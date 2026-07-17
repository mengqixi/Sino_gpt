from __future__ import annotations

import mimetypes
import base64
import io
import json
import os
import re
import shutil
import textwrap
import uuid
import zipfile
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests
from fastapi import UploadFile
from PIL import Image, ImageDraw, ImageFont, ImageOps

from ..config import ALLOWED_IMAGE_EXTENSIONS, DATA_DIR
from ..database import db_session, now_iso
from .api_config_service import TEXT_API_TYPE, get_config, get_default_config, mask_api_key, require_config_type
from .json_path_service import json_path_get


ORGANIZER_DATA_DIR = DATA_DIR / "vip_organizer"
ORGANIZER_UPLOAD_DIR = ORGANIZER_DATA_DIR / "uploads"
ORGANIZER_RESULT_DIR = ORGANIZER_DATA_DIR / "results"
UPLOAD_COPY_BUFFER_SIZE = 1024 * 1024
ORGANIZER_SESSION_TTL_HOURS = 24
BUNDLED_FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NotoSansSC-VF-GB2312.ttf"


SLOT_DEFINITIONS = [
    ("1.jpg", "模特主图", "800×800", "model"),
    ("2.jpg", "半侧或全侧", "800×800", "product"),
    ("3.jpg", "背面", "800×800", "product"),
    ("4.jpg", "ELLE Logo细节", "800×800", "product"),
    ("15.jpg", "内里细节", "800×800", "product"),
    ("30.png", "正面透明底", "800×800", "product"),
    ("50.jpg", "模特竖图", "950×1200", "model"),
    ("401.jpg", "产品信息", "750×665", "generated"),
    ("601.jpg", "模特展示一", "750×750", "model"),
    ("602.jpg", "模特展示二", "750×750", "model"),
    ("603.jpg", "模特展示三", "750×750", "model"),
    ("604.jpg", "内里/结构细节", "750×750", "product"),
    ("605.jpg", "ELLE Logo/五金细节", "750×750", "product"),
    ("606.jpg", "正面、半侧面或全侧、背面、开口顶视图", "750×750", "composite"),
    ("801.jpg", "吊牌信息", "750×750", "tag"),
]

PRODUCT_ROLES = {
    "auto",
    "front",
    "semi_side",
    "side",
    "back",
    "top",
    "bottom",
    "transparent",
    "strap",
    "detail",
    "ignore",
    # Keep accepting labels returned by older clients and API responses.
    "logo",
    "interior",
}
CANONICAL_PRODUCT_ROLES = PRODUCT_ROLES - {"auto", "ignore", "logo", "interior"}
DETAIL_TAGS = {
    "logo",
    "hardware",
    "strap_chain",
    "zipper_opening",
    "interior",
    "inner_pocket_label",
    "material_texture",
    "bottom_detail",
}
API_ANALYSIS_ROLES = CANONICAL_PRODUCT_ROLES


def _analysis_config(config_id: int | None = None) -> dict[str, Any]:
    config = get_config(config_id, include_secret=True) if config_id else get_default_config(TEXT_API_TYPE, include_secret=True)
    if not config:
        raise ValueError("尚未配置可用的图文分析 API，请先在 API 设置中新增")
    if not config.get("enabled"):
        raise ValueError("所选图文分析 API 未启用")
    require_config_type(config, TEXT_API_TYPE)
    return config


def save_analysis_config(api_base_url: str, api_key: str, model_name: str) -> dict[str, Any]:
    base_url = api_base_url.strip().rstrip("/")
    model = model_name.strip()
    key = api_key.strip()
    if not base_url or not key or not model:
        raise ValueError("API Base URL、API Key 和模型名称不能为空")
    with db_session() as conn:
        row = conn.execute(
            "SELECT id FROM api_configs WHERE api_type = ? ORDER BY is_default DESC, id ASC LIMIT 1",
            (TEXT_API_TYPE,),
        ).fetchone()
        ts = now_iso()
        if row:
            conn.execute(
                """
                UPDATE api_configs
                SET api_base_url = ?, api_key = ?, model_name = ?, endpoint_path = ?,
                    request_content_type = ?, response_text_path = ?, enabled = 1, updated_at = ?
                WHERE id = ?
                """,
                (base_url, key, model, "/chat/completions", "application/json", "choices.0.message.content", ts, row["id"]),
            )
            config_id = row["id"]
        else:
            cursor = conn.execute(
                """
                INSERT INTO api_configs (
                    config_name, api_type, api_base_url, api_key, model_name,
                    endpoint_path, request_content_type, response_text_path,
                    timeout_seconds, enabled, is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "素材分析（图文）", TEXT_API_TYPE, base_url, key, model,
                    "/chat/completions", "application/json", "choices.0.message.content",
                    350, 1, 1, ts, ts,
                ),
            )
            config_id = cursor.lastrowid
    return {"configured": True, "config_id": config_id, "api_base_url": base_url, "model_name": model}


def analysis_config_status() -> dict[str, Any]:
    try:
        config = _analysis_config()
    except ValueError:
        return {"configured": False}
    return {
        "configured": True,
        "config_id": config["id"],
        "api_base_url": config["api_base_url"],
        "model_name": config["model_name"],
        "api_key_masked": mask_api_key(config.get("api_key")),
    }


def _analysis_data_url(path: Path) -> str:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((900, 900), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=78, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def analyze_assets_with_api(session_id: str, product_image_ids: list[int], api_config_id: int) -> dict[str, Any]:
    _validate_session_assets(session_id, {"product": product_image_ids})
    rows = _uploaded_rows(product_image_ids)
    if not rows:
        raise ValueError("请至少上传一张商品原图")
    config = _analysis_config(api_config_id)
    role_text = (
        "front正面主图, semi_side半侧面或三分之二角度, side完整侧面, back背面, top顶部或开口全景, bottom底部, "
        "transparent透明底正面, strap肩带完整展示, detail局部细节"
    )
    tag_text = (
        "logo ELLE Logo, hardware五金, strap_chain肩带或链条, "
        "zipper_opening拉链或开口, interior内里, inner_pocket_label内袋或内标, "
        "material_texture材质或纹理, bottom_detail包底细节"
    )
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            "你是ELLE女包电商素材分类员。请逐张判断图片用途，只能从固定角色中选择："
            f"{role_text}。每张图只能有一个主类别；相似图片允许同类别。"
            f"另外可从以下细节标签中选择零个或多个：{tag_text}。"
            "完整产品图不能因为画面里有Logo或链条就标成局部细节。只有局部放大的照片才标为detail。"
            "半侧面能同时看到包身正面和一侧厚度，标为semi_side；只看到窄侧廓时才标为side。"
            "front必须是完整正面且包口、Logo或主要五金朝向镜头；back是完整背面，不要仅因没有明显Logo就草率判断。"
            "不要把肩带横向铺开的正面图误判为半侧面；判断视角时以包身主体透视和侧边厚度为准。"
            "底部平放或仰拍标为bottom；整条肩带或链条长度为主要展示内容时标为strap；"
            "出现内衬、内袋或包内空间时添加interior，内袋或内标近景再添加inner_pocket_label；"
            "包底完整视图主类别标为bottom；只有包脚、底部车线等局部放大图才标detail并添加bottom_detail；"
            "俯拍完整包口但重点不是局部拉链时标为top。无法可靠判断正反面时降低confidence。"
            "仅返回JSON对象，不要Markdown，格式："
            '{"items":[{"index":1,"role":"front","tags":["logo","hardware"],"confidence":90,"reason":"简短中文理由"}]}。'
        ),
    }]
    local_hints: dict[int, tuple[str, int]] = {}
    for index, row in enumerate(rows, start=1):
        metrics = _image_metrics(row)
        local_role, _, local_confidence, _ = _classify_product_metrics(metrics)
        local_hints[index] = (local_role, local_confidence)
        content.append({
            "type": "text",
            "text": (
                f"图片 {index}，文件名：{row['file_name']}。"
                f"轻量本地初判：{local_role}（{local_confidence}%），仅供参考，请以画面为准。"
            ),
        })
        content.append({"type": "image_url", "image_url": {"url": _analysis_data_url(Path(row["file_path"])), "detail": "low"}})
    base_url = (config.get("api_base_url") or "").strip()
    endpoint_path = (config.get("endpoint_path") or "").strip()
    if not base_url or not endpoint_path:
        raise ValueError("图文分析 API 的 Base URL 或接口路径为空")
    if (config.get("method") or "POST").upper() != "POST":
        raise ValueError("图文分析 API 当前仅支持 POST 请求")
    if (config.get("request_content_type") or "application/json").lower() != "application/json":
        raise ValueError("图文分析 API 必须使用 application/json 请求格式")
    headers = {"Content-Type": "application/json"}
    auth_type = (config.get("auth_type") or "bearer").lower()
    api_key = config.get("api_key") or ""
    if auth_type != "none":
        if not api_key:
            raise ValueError("所选图文分析 API 尚未配置 API Key")
        header_name = config.get("auth_header_name") or "Authorization"
        if auth_type == "bearer":
            prefix = config.get("auth_header_prefix") or "Bearer"
            headers[header_name] = f"{prefix} {api_key}".strip()
        elif auth_type == "raw":
            headers[header_name] = api_key
        else:
            raise ValueError("图文分析 API 的认证方式无效")
    try:
        request_payload = json.loads(config.get("extra_params_json") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("图文分析 API 的额外参数 JSON 格式错误") from exc
    if not isinstance(request_payload, dict):
        raise ValueError("图文分析 API 的额外参数 JSON 必须是对象")
    request_payload.update({
        config.get("model_field_name") or "model": config.get("model_name") or "",
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
    })
    url = base_url.rstrip("/") + "/" + endpoint_path.lstrip("/")
    response = requests.post(
        url,
        headers=headers,
        json=request_payload,
        timeout=max(10, int(config.get("timeout_seconds") or 350)),
    )
    if not response.ok:
        raise ValueError(f"素材分析 API 返回 HTTP {response.status_code}：{response.text[:300]}")
    try:
        response_json = response.json()
        response_values = json_path_get(
            response_json,
            config.get("response_text_path") or "choices.0.message.content",
        )
        raw = response_values[0]
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        match = re.search(r"\{.*\}", raw, re.S)
        parsed = json.loads(match.group(0) if match else raw)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("素材分析 API 未返回可读取的固定标签 JSON") from exc
    results: list[dict[str, Any]] = []
    roles: dict[int, str] = {}
    tags_by_image: dict[int, list[str]] = {}
    for item in parsed.get("items", []):
        index = int(item.get("index", 0))
        role = str(item.get("role", "detail"))
        raw_tags = item.get("tags", [])
        tags = [str(tag) for tag in raw_tags if str(tag) in DETAIL_TAGS] if isinstance(raw_tags, list) else []
        if role == "logo":
            role, tags = "detail", list(dict.fromkeys([*tags, "logo", "hardware"]))
        elif role == "interior":
            role, tags = "detail", list(dict.fromkeys([*tags, "interior"]))
        if local_hints.get(index, ("", 0))[0] == "transparent":
            role = "transparent"
        if 1 <= index <= len(rows) and role in API_ANALYSIS_ROLES:
            image_id = int(rows[index - 1]["id"])
            roles[image_id] = role
            tags_by_image[image_id] = tags
            results.append({
                "image_id": image_id,
                "file_name": rows[index - 1]["file_name"],
                "role": role,
                "tags": tags,
                "confidence": max(0, min(100, int(item.get("confidence", 0)))),
                "reason": str(item.get("reason", ""))[:160],
            })
    if not results:
        raise ValueError("素材分析 API 没有返回有效分类")
    return {"asset_roles": roles, "asset_tags": tags_by_image, "items": results}


def _uploaded_rows(image_ids: list[int]) -> list[dict[str, Any]]:
    ids = list(dict.fromkeys(int(item) for item in image_ids))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with db_session() as conn:
        rows = conn.execute(
            f"SELECT id, file_name, file_path, width, height, mime_type FROM vip_organizer_assets WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    lookup = {int(row["id"]): dict(row) for row in rows}
    return [lookup[item] for item in ids if item in lookup]


def _validate_session_assets(session_id: str, assets_by_type: dict[str, list[int]]) -> None:
    normalized = {
        asset_type: list(dict.fromkeys(int(image_id) for image_id in image_ids))
        for asset_type, image_ids in assets_by_type.items()
        if image_ids
    }
    with db_session() as conn:
        session = conn.execute("SELECT id FROM vip_organizer_sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            raise ValueError("整理会话已失效，请重新开始")
        conn.execute(
            "UPDATE vip_organizer_sessions SET updated_at = ? WHERE id = ?",
            (now_iso(), session_id),
        )
        for asset_type, image_ids in normalized.items():
            placeholders = ",".join("?" for _ in image_ids)
            rows = conn.execute(
                f"SELECT id, asset_type FROM vip_organizer_assets WHERE session_id = ? AND id IN ({placeholders})",
                [session_id, *image_ids],
            ).fetchall()
            lookup = {int(row["id"]): row["asset_type"] for row in rows}
            invalid = [image_id for image_id in image_ids if lookup.get(image_id) != asset_type]
            if invalid:
                raise ValueError(f"存在不属于当前整理会话的{asset_type}素材，请重新上传并整理")


def _image_metrics(row: dict[str, Any]) -> dict[str, Any]:
    path = Path(row["file_path"])
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"无法读取图片：{row['file_name']}")
    alpha_ratio = 0.0
    if image.ndim == 3 and image.shape[2] == 4:
        alpha_ratio = float(np.mean(image[:, :, 3] < 250))
        bgr = image[:, :, :3]
    elif image.ndim == 2:
        bgr = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    else:
        bgr = image[:, :, :3]
    height, width = bgr.shape[:2]
    scale = min(1.0, 900 / max(width, height))
    if scale < 1:
        bgr = cv2.resize(bgr, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    sh, sw = rgb.shape[:2]
    edge = max(2, min(sh, sw) // 30)
    border = np.vstack(
        [
            rgb[:edge, :].reshape(-1, 3),
            rgb[-edge:, :].reshape(-1, 3),
            rgb[:, :edge].reshape(-1, 3),
            rgb[:, -edge:].reshape(-1, 3),
        ]
    )
    background = np.median(border, axis=0)
    distance = np.linalg.norm(rgb.astype(np.float32) - background, axis=2)
    mask = (distance > 30).astype(np.uint8)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    keep_ids = [index for index in range(1, count) if stats[index, cv2.CC_STAT_AREA] > sh * sw * 0.00035]
    foreground = np.isin(labels, keep_ids)
    ys, xs = np.where(foreground)
    if len(xs):
        box_width = int(xs.max() - xs.min() + 1)
        box_height = int(ys.max() - ys.min() + 1)
        bbox_ratio = box_width * box_height / (sw * sh)
        object_ratio = box_width / max(1, box_height)
    else:
        bbox_ratio = 0.0
        object_ratio = 1.0
    main_component_ratio = object_ratio
    main_component_fill_ratio = foreground_fill_ratio = 0.0
    main_symmetry_error = 0.0
    main_angle_degrees = 0.0
    main_center_x = 0.5
    main_center_y = 0.5
    main_top_fill_ratio = 0.0
    main_bottom_fill_ratio = 0.0
    main_body_side_edge_ratio = 999.0
    if keep_ids:
        main_id = max(keep_ids, key=lambda index: int(stats[index, cv2.CC_STAT_AREA]))
        left = int(stats[main_id, cv2.CC_STAT_LEFT])
        top = int(stats[main_id, cv2.CC_STAT_TOP])
        main_width = int(stats[main_id, cv2.CC_STAT_WIDTH])
        main_height = int(stats[main_id, cv2.CC_STAT_HEIGHT])
        main_area = int(stats[main_id, cv2.CC_STAT_AREA])
        main_component_ratio = main_width / max(1, main_height)
        main_component_fill_ratio = main_area / max(1, main_width * main_height)
        main_center_x = float(centroids[main_id, 0] / sw)
        main_center_y = float(centroids[main_id, 1] / sh)
        component_mask = (labels == main_id).astype(np.uint8)
        component_crop = component_mask[top:top + main_height, left:left + main_width]
        mirrored = cv2.flip(component_crop, 1)
        main_symmetry_error = float(np.mean(component_crop != mirrored))
        split = max(1, main_height // 2)
        main_top_fill_ratio = float(component_crop[:split].mean())
        main_bottom_fill_ratio = float(component_crop[split:].mean())
        component_points = np.column_stack(np.where(component_mask > 0)[::-1]).astype(np.float32)
        if len(component_points) >= 5:
            _, eigenvectors, _ = cv2.PCACompute2(component_points, mean=None)
            angle = abs(float(np.degrees(np.arctan2(eigenvectors[0, 1], eigenvectors[0, 0]))))
            main_angle_degrees = min(angle, 180.0 - angle)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    edge_map = cv2.Canny(gray, 80, 180) > 0
    edge_ratio = float(np.mean(edge_map))
    if keep_ids and main_width >= 12 and main_height >= 12:
        body_top = min(sh, top + int(main_height * 0.45))
        body_bottom = min(sh, top + int(main_height * 0.95))
        left_end = min(sw, left + int(main_width * 0.20))
        center_start = left_end
        center_end = min(sw, left + int(main_width * 0.80))
        right_start = center_end
        right_end = min(sw, left + main_width)
        left_edges = edge_map[body_top:body_bottom, left:left_end]
        center_edges = edge_map[body_top:body_bottom, center_start:center_end]
        right_edges = edge_map[body_top:body_bottom, right_start:right_end]
        if left_edges.size and center_edges.size and right_edges.size:
            side_density = min(float(left_edges.mean()), float(right_edges.mean()))
            center_density = float(center_edges.mean())
            main_body_side_edge_ratio = side_density / max(0.0001, center_density)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    gold = (
        ((hue >= 8) & (hue <= 35) & (saturation >= 55) & (value >= 95))
        | ((hue >= 15) & (hue <= 38) & (saturation >= 25) & (value >= 155))
    )
    strict_gold = (hue >= 8) & (hue <= 35) & (saturation >= 65) & (value >= 110)
    center_gold = gold[int(sh * 0.25):int(sh * 0.78), int(sw * 0.25):int(sw * 0.75)]
    if keep_ids:
        logo_left = max(0, int(left + main_width * 0.28))
        logo_right = min(sw, int(left + main_width * 0.72))
        logo_top = max(0, int(top + main_height * 0.45))
        logo_bottom = min(sh, int(top + main_height * 0.92))
        strict_center_gold = strict_gold[logo_top:logo_bottom, logo_left:logo_right]
    else:
        strict_center_gold = strict_gold[0:0, 0:0]
    foreground_ratio = float(foreground.mean())
    foreground_fill_ratio = foreground_ratio / bbox_ratio if bbox_ratio else 0.0
    return {
        **row,
        "preview_url": f"/api/vip-organizer/assets/{row['id']}/thumbnail",
        "original_url": f"/api/vip-organizer/assets/{row['id']}/original",
        "alpha_ratio": round(alpha_ratio, 4),
        "foreground_ratio": round(foreground_ratio, 4),
        "foreground_fill_ratio": round(float(foreground_fill_ratio), 4),
        "bbox_ratio": round(float(bbox_ratio), 4),
        "object_ratio": round(float(object_ratio), 4),
        "sharpness": round(sharpness, 2),
        "edge_ratio": round(edge_ratio, 4),
        "center_gold_ratio": round(float(center_gold.mean()) if center_gold.size else 0.0, 4),
        "strict_center_gold_ratio": round(float(strict_center_gold.mean()) if strict_center_gold.size else 0.0, 4),
        "component_count": len(keep_ids),
        "main_component_ratio": round(float(main_component_ratio), 4),
        "main_component_fill_ratio": round(float(main_component_fill_ratio), 4),
        "main_symmetry_error": round(float(main_symmetry_error), 4),
        "main_angle_degrees": round(float(main_angle_degrees), 2),
        "main_center_x": round(float(main_center_x), 4),
        "main_center_y": round(float(main_center_y), 4),
        "main_top_fill_ratio": round(float(main_top_fill_ratio), 4),
        "main_bottom_fill_ratio": round(float(main_bottom_fill_ratio), 4),
        "main_body_side_edge_ratio": round(float(main_body_side_edge_ratio), 4),
    }


def _classify_product_metrics(item: dict[str, Any]) -> tuple[str, list[str], int, str]:
    """Return a lightweight primary role plus optional multi-label detail tags."""
    alpha = float(item.get("alpha_ratio", 0))
    foreground = float(item.get("foreground_ratio", 0))
    fill = float(item.get("foreground_fill_ratio", 0))
    bbox = float(item.get("bbox_ratio", 0))
    ratio = float(item.get("object_ratio", 1))
    sharpness = float(item.get("sharpness", 0))
    edge_ratio = float(item.get("edge_ratio", 0))
    center_gold = float(item.get("center_gold_ratio", 0))
    strict_center_gold = float(item.get("strict_center_gold_ratio", center_gold))
    main_ratio = float(item.get("main_component_ratio", ratio))
    main_fill = float(item.get("main_component_fill_ratio", fill))
    main_symmetry = float(item.get("main_symmetry_error", 0))
    main_angle = float(item.get("main_angle_degrees", 0))
    main_center_y = float(item.get("main_center_y", 0.5))
    main_top_fill = float(item.get("main_top_fill_ratio", 0))
    main_bottom_fill = float(item.get("main_bottom_fill_ratio", 0))
    component_count = int(item.get("component_count", 1))
    has_shape_metrics = "main_component_ratio" in item

    if alpha > 0.02:
        return "transparent", [], 99, "检测到透明通道，适合作为透明正面素材"
    if 1.65 <= main_ratio <= 3.0 and bbox < 0.24 and foreground < 0.18 and main_fill >= 0.68:
        return "bottom", [], 94, "主体呈横向扁平轮廓，判断为包底视图"
    if main_ratio < 0.30 and foreground < 0.10:
        return "strap", ["strap_chain"], 92, "主体纵向跨度很长且包体占比较小，判断为完整肩带展示"
    if main_ratio < 0.47 and foreground < 0.14 and bbox < 0.32:
        return "side", [], 90, "包体轮廓较窄，判断为侧面视图"
    looks_like_open_top = (
        main_symmetry >= 0.22
        and main_fill >= 0.48
        and main_center_y <= 0.57
        and main_top_fill >= main_bottom_fill * 0.65
        and bbox < 0.45
        and foreground < 0.28
    )
    legacy_open_top = (
        not has_shape_metrics
        and 0.68 <= ratio <= 0.98
        and bbox < 0.32
        and foreground < 0.18
    )
    if looks_like_open_top or legacy_open_top:
        return "top", ["zipper_opening", "interior"], 86, "俯拍轮廓和开口区域明显，判断为顶部或开口全景"

    is_closeup = (
        bbox >= 0.48
        or foreground >= 0.28
        or (bbox >= 0.38 and fill < 0.35)
        or (main_ratio > 3.0 and bbox >= 0.20)
    )
    if is_closeup:
        tags: list[str] = []
        opening_interior = (
            component_count >= 6
            and main_symmetry >= 0.30
            and main_top_fill > main_bottom_fill
        ) or (
            not has_shape_metrics
            and fill >= 0.60
            and sharpness < 2000
        )
        if opening_interior:
            tags.append("interior")
            if sharpness < 2000:
                tags.append("inner_pocket_label")
        if not opening_interior and (
            main_ratio > 3.0 or strict_center_gold > 0.008 or center_gold > 0.04
        ):
            tags.extend(["logo", "hardware"])
        if main_angle >= 60 or (foreground >= 0.20 and fill >= 0.32):
            tags.append("zipper_opening")
        if sharpness > 1800 or edge_ratio > 0.12:
            tags.append("material_texture")
        if 1.5 <= main_ratio <= 3.0 and main_fill >= 0.75 and main_angle <= 15:
            tags.append("bottom_detail")
        if not tags:
            tags.append("hardware" if strict_center_gold > 0.002 else "material_texture")
        tags = list(dict.fromkeys(tags))
        confidence = 74 if tags else 62
        return "detail", tags, confidence, "检测到局部放大画面，并按可见结构添加细节标签"

    full_view_tags = ["logo", "hardware"] if strict_center_gold > 0.008 else []
    if center_gold > 0.012 and 1.05 <= ratio <= 1.24 and fill >= 0.60 and bbox <= 0.27:
        return "semi_side", full_view_tags, 84, "完整包体同时露出正面和一侧厚度，判断为半侧面视图"
    if strict_center_gold > 0.008:
        return "front", full_view_tags, 76, "检测到完整包体及中央五金/Logo候选，判断为正面主图"
    return "front", [], 58, "检测到完整包体但缺少明确正反面标志，暂作正面候选，建议人工确认"


def _refine_product_classifications(products: list[dict[str, Any]]) -> None:
    """Use relative geometry within one product batch to correct obvious view mix-ups."""
    full_roles = {"front", "semi_side", "side", "back", "top"}
    candidates = [item for item in products if item.get("suggested_role") in full_roles]
    if len(candidates) < 3:
        return

    regular = [
        item for item in candidates
        if float(item.get("main_component_fill_ratio", 0)) >= 0.45
        and float(item.get("bbox_ratio", 0)) < 0.48
    ]
    if len(regular) < 3:
        return

    ratios = np.array([float(item.get("main_component_ratio", item.get("object_ratio", 1))) for item in regular])
    median_ratio = float(np.median(ratios))
    narrowest = min(regular, key=lambda item: float(item.get("main_component_ratio", 1)))
    narrow_ratio = float(narrowest.get("main_component_ratio", 1))
    if narrow_ratio < 0.48 and narrow_ratio <= median_ratio * 0.68:
        narrowest.update({
            "suggested_role": "side",
            "suggested_tags": [],
            "role_confidence": max(90, int(narrowest.get("role_confidence", 0))),
            "role_reason": "同批完整视图中包体宽厚比最窄，校正为完整侧面",
        })

    face_candidates = [
        item for item in regular
        if item.get("suggested_role") not in {"top", "side"}
        and float(item.get("main_component_fill_ratio", 0)) >= 0.52
    ]
    if len(face_candidates) < 2:
        return

    gold_values = [float(item.get("strict_center_gold_ratio", 0)) for item in face_candidates]
    if max(gold_values, default=0) >= 0.008:
        back = min(face_candidates, key=lambda item: float(item.get("strict_center_gold_ratio", 0)))
        if float(back.get("strict_center_gold_ratio", 0)) <= max(gold_values) * 0.20:
            back.update({
                "suggested_role": "back",
                "suggested_tags": [],
                "role_confidence": 82,
                "role_reason": "同批完整视图中未检测到正面中央Logo/五金，校正为背面",
            })

    remaining = [item for item in face_candidates if item.get("suggested_role") != "back"]
    selected_semi_side: dict[str, Any] | None = None
    if len(remaining) >= 2:
        edge_candidates = [
            item for item in remaining
            if float(item.get("main_body_side_edge_ratio", 999)) < 999
        ]
        if edge_candidates:
            face_median_ratio = float(np.median([
                float(item.get("main_component_ratio", item.get("object_ratio", 1)))
                for item in edge_candidates
            ]))

            def semi_side_score(item: dict[str, Any]) -> float:
                edge_value = max(0.0001, float(item.get("main_body_side_edge_ratio", 999)))
                item_ratio = float(item.get("main_component_ratio", item.get("object_ratio", 1)))
                relative_narrowing = max(0.0, face_median_ratio - item_ratio)
                return float(np.log(edge_value)) - relative_narrowing * 15.0

            selected_semi_side = min(
                edge_candidates,
                key=semi_side_score,
            )
            if semi_side_score(selected_semi_side) > float(np.log(1.65)):
                selected_semi_side = None
        else:
            symmetry_values = [float(item.get("main_symmetry_error", 0)) for item in remaining]
            most_asymmetric = max(remaining, key=lambda item: float(item.get("main_symmetry_error", 0)))
            if float(most_asymmetric.get("main_symmetry_error", 0)) >= min(symmetry_values) + 0.015:
                selected_semi_side = most_asymmetric

        if selected_semi_side is not None:
            selected_semi_side.update({
                "suggested_role": "semi_side",
                "role_confidence": max(82, int(selected_semi_side.get("role_confidence", 0))),
                "role_reason": "同批完整视图中包体左右边缘与中央结构的透视差异最明显，校正为半侧面",
            })

    for item in face_candidates:
        if item.get("suggested_role") == "back":
            continue
        strict_gold = float(item.get("strict_center_gold_ratio", 0))
        if item is not selected_semi_side and strict_gold >= 0.008:
            item.update({
                "suggested_role": "front",
                "suggested_tags": ["logo", "hardware"],
                "role_confidence": 78,
                "role_reason": "同批视图校正后检测到正面中央Logo/五金，判断为正面主图",
            })


def _valid_session_id(session_id: str | None) -> bool:
    return bool(session_id and re.fullmatch(r"[0-9a-f]{32}", session_id))


def _session_upload_dir(session_id: str) -> Path:
    if not _valid_session_id(session_id):
        raise ValueError("Invalid organizer session")
    return ORGANIZER_UPLOAD_DIR / session_id


def _session_result_dir(session_id: str) -> Path:
    if not _valid_session_id(session_id):
        raise ValueError("Invalid organizer session")
    return ORGANIZER_RESULT_DIR / session_id


def delete_session(session_id: str) -> None:
    """Delete one organizer session without touching other users."""
    if not _valid_session_id(session_id):
        return
    with db_session() as conn:
        rows = conn.execute(
            "SELECT id, file_path FROM vip_organizer_assets WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        conn.execute("DELETE FROM vip_organizer_sessions WHERE id = ?", (session_id,))

    organizer_root = ORGANIZER_DATA_DIR.resolve()
    for row in rows:
        path = Path(row["file_path"])
        try:
            if os.path.commonpath((str(path.resolve()), str(organizer_root))) == str(organizer_root):
                path.unlink(missing_ok=True)
                (path.parent / f"thumb_{row['id']}.jpg").unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    shutil.rmtree(_session_upload_dir(session_id), ignore_errors=True)
    shutil.rmtree(_session_result_dir(session_id), ignore_errors=True)


def _cleanup_expired_sessions() -> None:
    cutoff = (datetime.now() - timedelta(hours=ORGANIZER_SESSION_TTL_HOURS)).isoformat(timespec="seconds")
    with db_session() as conn:
        rows = conn.execute(
            "SELECT id FROM vip_organizer_sessions WHERE COALESCE(updated_at, created_at) < ?",
            (cutoff,),
        ).fetchall()
    for row in rows:
        delete_session(row["id"])


def start_session(previous_session_id: str | None = None) -> dict[str, str]:
    """Replace only the caller's previous organizer session."""
    ORGANIZER_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ORGANIZER_RESULT_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_expired_sessions()
    if _valid_session_id(previous_session_id):
        delete_session(previous_session_id)

    session_id = uuid.uuid4().hex
    ts = now_iso()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO vip_organizer_sessions (id, created_at, updated_at) VALUES (?, ?, ?)",
            (session_id, ts, ts),
        )
    _session_upload_dir(session_id).mkdir(parents=True, exist_ok=True)
    _session_result_dir(session_id).mkdir(parents=True, exist_ok=True)
    return {"session_id": session_id}


def save_assets(session_id: str, asset_type: str, files: list[UploadFile]) -> list[dict[str, Any]]:
    if asset_type not in {"product", "model", "tag"}:
        raise ValueError("素材类型不正确")
    if not files:
        raise ValueError("请选择要上传的图片")
    with db_session() as conn:
        exists = conn.execute("SELECT id FROM vip_organizer_sessions WHERE id = ?", (session_id,)).fetchone()
    if not exists:
        raise ValueError("整理会话已失效，请重新开始")
    session_upload_dir = _session_upload_dir(session_id)
    session_upload_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[dict[str, Any]] = []
    for file in files:
        original_name = file.filename or "image.png"
        suffix = Path(original_name).suffix.lower()
        if suffix not in ALLOWED_IMAGE_EXTENSIONS:
            continue
        path = session_upload_dir / f"{uuid.uuid4().hex}{suffix}"
        try:
            with path.open("wb") as output:
                shutil.copyfileobj(file.file, output, length=UPLOAD_COPY_BUFFER_SIZE)
            with Image.open(path) as image:
                width, height = image.size
                image.verify()
            prepared.append({
                "file_name": original_name,
                "path": path,
                "mime_type": file.content_type or mimetypes.guess_type(original_name)[0] or "image/jpeg",
                "width": width,
                "height": height,
            })
        except Exception:
            path.unlink(missing_ok=True)

    saved: list[dict[str, Any]] = []
    try:
        with db_session() as conn:
            still_exists = conn.execute("SELECT id FROM vip_organizer_sessions WHERE id = ?", (session_id,)).fetchone()
            if not still_exists:
                raise ValueError("整理会话已失效，请重新开始")
            for item in prepared:
                cursor = conn.execute(
                    """
                    INSERT INTO vip_organizer_assets
                        (session_id, asset_type, file_name, file_path, file_size, mime_type, width, height, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        asset_type,
                        item["file_name"],
                        str(item["path"]),
                        item["path"].stat().st_size,
                        item["mime_type"],
                        item["width"],
                        item["height"],
                        now_iso(),
                    ),
                )
                saved.append({
                    "image_id": int(cursor.lastrowid),
                    "file_name": item["file_name"],
                    "preview_url": f"/api/vip-organizer/assets/{cursor.lastrowid}/thumbnail",
                    "original_url": f"/api/vip-organizer/assets/{cursor.lastrowid}/original",
                    "width": item["width"],
                    "height": item["height"],
                })
            conn.execute(
                "UPDATE vip_organizer_sessions SET updated_at = ? WHERE id = ?",
                (now_iso(), session_id),
            )
    except Exception:
        for item in prepared:
            item["path"].unlink(missing_ok=True)
        raise
    return saved


def asset_thumbnail(image_id: int) -> Path:
    rows = _uploaded_rows([image_id])
    if not rows:
        raise ValueError("图片记录不存在")
    source_path = Path(rows[0]["file_path"])
    thumbnail_path = source_path.parent / f"thumb_{image_id}.jpg"
    if thumbnail_path.exists() and thumbnail_path.stat().st_mtime >= source_path.stat().st_mtime:
        return thumbnail_path

    temp_path = source_path.parent / f".{thumbnail_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with Image.open(source_path) as source:
            source.draft("RGB", (420, 420))
            image = ImageOps.exif_transpose(source).convert("RGBA")
            image.thumbnail((420, 420), Image.Resampling.BILINEAR)
            canvas = Image.new("RGB", image.size, "white")
            canvas.paste(image.convert("RGB"), mask=image.getchannel("A"))
            canvas.save(temp_path, format="JPEG", quality=80, subsampling=2)
        os.replace(temp_path, thumbnail_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return thumbnail_path


def asset_original(image_id: int) -> Path:
    rows = _uploaded_rows([image_id])
    if not rows:
        raise ValueError("图片记录不存在")
    path = Path(rows[0]["file_path"])
    try:
        inside_uploads = os.path.commonpath(
            (str(path.resolve()), str(ORGANIZER_UPLOAD_DIR.resolve()))
        ) == str(ORGANIZER_UPLOAD_DIR.resolve())
    except ValueError:
        inside_uploads = False
    if not path.exists() or not inside_uploads:
        raise ValueError("图片文件不存在")
    return path


def _slot(file_name: str, title: str, size: str, kind: str, ids: list[int], confidence: int, reason: str) -> dict[str, Any]:
    return {
        "file_name": file_name,
        "title": title,
        "size": size,
        "kind": kind,
        "image_ids": ids,
        "confidence": confidence,
        "reason": reason,
    }


def analyze_assets(
    session_id: str,
    product_image_ids: list[int],
    model_image_ids: list[int],
    tag_image_ids: list[int],
    asset_roles: dict[int, str] | None = None,
    asset_tags: dict[int, list[str]] | None = None,
) -> dict[str, Any]:
    _validate_session_assets(session_id, {
        "product": product_image_ids,
        "model": model_image_ids,
        "tag": tag_image_ids,
    })
    products = [_image_metrics(row) for row in _uploaded_rows(product_image_ids)]
    models = [_image_metrics(row) for row in _uploaded_rows(model_image_ids)]
    tags = [_image_metrics(row) for row in _uploaded_rows(tag_image_ids)]
    if not products:
        raise ValueError("请至少上传一张商品原图")

    for item in products:
        role, suggested_tags, confidence, reason = _classify_product_metrics(item)
        item.update({
            "suggested_role": role,
            "suggested_tags": suggested_tags,
            "role_confidence": confidence,
            "role_reason": reason,
        })
    _refine_product_classifications(products)

    role_overrides: dict[int, str] = {}
    tag_overrides = {
        int(image_id): list(dict.fromkeys(tag for tag in image_tags if tag in DETAIL_TAGS))
        for image_id, image_tags in (asset_tags or {}).items()
        if isinstance(image_tags, list)
    }
    for image_id, raw_role in (asset_roles or {}).items():
        if raw_role not in PRODUCT_ROLES or raw_role == "auto":
            continue
        image_id = int(image_id)
        if raw_role == "logo":
            role_overrides[image_id] = "detail"
            tag_overrides[image_id] = list(dict.fromkeys([*tag_overrides.get(image_id, []), "logo", "hardware"]))
        elif raw_role == "interior":
            role_overrides[image_id] = "detail"
            tag_overrides[image_id] = list(dict.fromkeys([*tag_overrides.get(image_id, []), "interior"]))
        else:
            role_overrides[image_id] = raw_role

    def effective_role(item: dict[str, Any]) -> str:
        return role_overrides.get(item["id"], item["suggested_role"])

    def effective_tags(item: dict[str, Any]) -> list[str]:
        return tag_overrides.get(item["id"], item["suggested_tags"])

    usable_products = [item for item in products if effective_role(item) != "ignore"]
    if not usable_products:
        raise ValueError("所有商品图都被标记为忽略，请至少保留一张")

    def assigned(role: str) -> dict[str, Any] | None:
        fixed = [item for item in usable_products if role_overrides.get(item["id"]) == role]
        candidates = fixed or [item for item in usable_products if effective_role(item) == role]
        return max(candidates, key=lambda item: (item["role_confidence"], item["sharpness"])) if candidates else None

    def tagged(tag: str) -> list[dict[str, Any]]:
        return [item for item in usable_products if tag in effective_tags(item)]

    def selection_confidence(item: dict[str, Any], role: str | None = None, tag: str | None = None) -> int:
        if role and role_overrides.get(item["id"]) == role:
            return 100
        if tag and item["id"] in tag_overrides and tag in tag_overrides[item["id"]]:
            return 100
        if (role and effective_role(item) == role) or (tag and tag in effective_tags(item)):
            return int(item["role_confidence"])
        return 45

    transparent = assigned("transparent") or max(usable_products, key=lambda item: item["alpha_ratio"])
    has_transparent = effective_role(transparent) == "transparent" or transparent["alpha_ratio"] > 0.02
    non_transparent = [item for item in usable_products if item["id"] != transparent["id"]] if has_transparent else usable_products[:]
    if not non_transparent:
        non_transparent = [transparent]
    full_views = [
        item
        for item in non_transparent
        if effective_role(item) in {"front", "semi_side", "side", "back", "top"}
        or (0.04 <= item["foreground_ratio"] <= 0.26 and item["bbox_ratio"] <= 0.36)
    ]
    if not full_views:
        full_views = sorted(
            non_transparent,
            key=lambda item: (abs(item["foreground_ratio"] - 0.14), abs(item["bbox_ratio"] - 0.22)),
        )[: max(1, min(6, len(non_transparent)))]

    regular_views = [item for item in full_views if 0.9 <= item["object_ratio"] <= 1.8]
    fixed_front_views = [item for item in usable_products if role_overrides.get(item["id"]) == "front"]
    labeled_front_views = fixed_front_views or [item for item in regular_views if effective_role(item) == "front"]
    front_view = max(
        labeled_front_views or regular_views or full_views,
        key=lambda item: (
            item["role_confidence"],
            item["foreground_fill_ratio"],
            -abs(item["object_ratio"] - 1.30),
            item["center_gold_ratio"],
            item["sharpness"],
        ),
    )
    back_candidates = [item for item in regular_views if item["id"] != front_view["id"]]
    back_view = assigned("back") or (min(
        back_candidates,
        key=lambda item: (item["center_gold_ratio"], -item["sharpness"]),
    ) if back_candidates else front_view)
    front_id = transparent["id"] if has_transparent else front_view["id"]
    side_candidates = [item for item in full_views if item["object_ratio"] < 0.75 and item["foreground_ratio"] >= 0.04]
    side_candidates.sort(key=lambda item: (abs(item["object_ratio"] - 0.45), -item["sharpness"]))
    side_view = assigned("side") or (side_candidates[0] if side_candidates else min(full_views, key=lambda item: item["object_ratio"]))
    side_id = side_view["id"]
    semi_side_candidates = [
        item
        for item in full_views
        if item["id"] not in {front_view["id"], back_view["id"]}
        and 1.02 <= item["object_ratio"] <= 1.26
        and item["foreground_fill_ratio"] >= 0.52
    ]
    semi_side_candidates.sort(key=lambda item: (
        abs(item["object_ratio"] - 1.16),
        -item["foreground_fill_ratio"],
        -item["sharpness"],
    ))
    angle_view = assigned("semi_side") or (semi_side_candidates[0] if semi_side_candidates else side_view)
    angle_role = "semi_side" if effective_role(angle_view) == "semi_side" else "side"
    top_candidates = [
        item for item in full_views
        if item["id"] not in {front_view["id"], back_view["id"], side_id, angle_view["id"]}
        and 0.72 <= item["object_ratio"] <= 0.95
    ]
    top_view = assigned("top") or (min(top_candidates, key=lambda item: item["sharpness"]) if top_candidates else next(
        (item for item in full_views if item["id"] not in {front_view["id"], back_view["id"], side_id, angle_view["id"]}),
        back_view,
    ))
    back_id = back_view["id"]
    top_id = top_view["id"]

    detail_views = [item for item in non_transparent if effective_role(item) in {"detail", "bottom", "strap"}]
    detail_views = detail_views or sorted(
        non_transparent,
        key=lambda item: (item["bbox_ratio"], item["foreground_ratio"], item["sharpness"]),
        reverse=True,
    )
    logo_candidates = tagged("logo")
    logo_view = max(
        logo_candidates or detail_views,
        key=lambda item: (effective_role(item) == "detail", item["bbox_ratio"], item["sharpness"]),
    )
    interior_candidates = [
        item for item in usable_products
        if "interior" in effective_tags(item) or "inner_pocket_label" in effective_tags(item)
    ]
    interior_view = max(
        interior_candidates or [item for item in detail_views if item["id"] != logo_view["id"]] or [logo_view],
        key=lambda item: (effective_role(item) == "detail", item["foreground_fill_ratio"], item["bbox_ratio"]),
    )
    secondary_candidates = [
        item for item in detail_views
        if item["id"] not in {logo_view["id"], interior_view["id"]}
        and any(tag in effective_tags(item) for tag in {"inner_pocket_label", "zipper_opening", "material_texture"})
    ]
    secondary_detail = max(
        secondary_candidates or [item for item in detail_views if item["id"] not in {logo_view["id"], interior_view["id"]}] or [interior_view],
        key=lambda item: ("inner_pocket_label" in effective_tags(item), item["bbox_ratio"], item["sharpness"]),
    )
    logo_id = logo_view["id"]
    interior_id = interior_view["id"]

    model_ids = [item["id"] for item in sorted(models, key=lambda item: item["sharpness"], reverse=True)]
    model_pick = lambda index: [model_ids[index % len(model_ids)]] if model_ids else []
    tag_ids = [tags[0]["id"]] if tags else []

    slot_values = {
        "1.jpg": (model_pick(0), 88 if models else 0, "来自独立模特图区，优先选择清晰度较高的照片"),
        "2.jpg": ([angle_view["id"]], selection_confidence(angle_view, role=angle_role), "优先使用半侧面（三分之二角度），缺少时才回退到完整侧面"),
        "3.jpg": ([back_id], selection_confidence(back_view, role="back"), "优先使用背面标签，缺少明确背面图时会回退到完整产品图"),
        "4.jpg": ([logo_id], selection_confidence(logo_view, tag="logo"), "优先使用局部细节中的ELLE Logo标签"),
        "15.jpg": ([interior_id], selection_confidence(interior_view, tag="interior"), "优先使用带内里或内袋标签的局部细节"),
        "30.png": ([front_id], 98 if has_transparent else 35, "检测到透明通道" if has_transparent else "未检测到透明图，暂用正面候选图"),
        "50.jpg": (model_pick(0), 88 if models else 0, "与1.jpg使用同一张模特图，仅按竖版规格重新排版"),
        "401.jpg": ([angle_view["id"]], selection_confidence(angle_view, role=angle_role), "按示例优先使用半侧面产品图和商品资料套用信息页模板"),
        "601.jpg": (model_pick(0), 90 if models else 0, "模特图自动留白排版"),
        "602.jpg": (model_pick(1), 90 if models else 0, "模特图自动留白排版"),
        "603.jpg": (model_pick(2), 90 if models else 0, "模特图自动留白排版"),
        "604.jpg": ([secondary_detail["id"]], selection_confidence(secondary_detail, tag="inner_pocket_label"), "优先使用另一张内里、拉链开口或结构细节"),
        "605.jpg": ([logo_id], selection_confidence(logo_view, tag="logo"), "复用ELLE Logo清晰近景候选"),
        "606.jpg": ([front_view["id"], angle_view["id"], back_id, top_id], min(
            selection_confidence(front_view, role="front"),
            selection_confidence(angle_view, role=angle_role),
            selection_confidence(back_view, role="back"),
            selection_confidence(top_view, role="top"),
        ), "按正面、半侧面或全侧、背面、开口顶视图的固定顺序生成四角度模板"),
        "801.jpg": (tag_ids, 95 if tags else 0, "使用独立上传的吊牌图片" if tags else "尚未上传吊牌图片"),
    }
    slots = [
        _slot(file_name, title, size, kind, *slot_values[file_name])
        for file_name, title, size, kind in SLOT_DEFINITIONS
    ]
    for item in products:
        item["selected_role"] = role_overrides.get(item["id"], "auto")
        item["selected_tags"] = tag_overrides.get(item["id"], [])
    for item in models:
        item["selected_role"] = "model"
        item["suggested_role"] = "model"
        item["suggested_tags"] = []
        item["role_confidence"] = 100
    for item in tags:
        item["selected_role"] = "tag"
        item["suggested_role"] = "tag"
        item["suggested_tags"] = []
        item["role_confidence"] = 100
    return {"assets": {"product": products, "model": models, "tag": tags}, "slots": slots}


def _load_image(image_id: int) -> Image.Image:
    rows = _uploaded_rows([image_id])
    if not rows:
        raise ValueError(f"图片记录不存在：{image_id}")
    with Image.open(rows[0]["file_path"]) as source:
        source.draft("RGB", (2400, 2400))
        image = ImageOps.exif_transpose(source)
        image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
        return image.copy()


def _fit(image: Image.Image, size: tuple[int, int], margin: int = 0, contain: bool = False) -> Image.Image:
    target = Image.new("RGB", size, "white")
    inner = (max(1, size[0] - margin * 2), max(1, size[1] - margin * 2))
    source = image.convert("RGB")
    rendered = ImageOps.contain(source, inner, Image.Resampling.LANCZOS) if contain else ImageOps.fit(source, inner, Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    target.paste(rendered, ((size[0] - rendered.width) // 2, (size[1] - rendered.height) // 2))
    return target


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        BUNDLED_FONT_PATH,
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            font = ImageFont.truetype(str(path), size=size)
            if path == BUNDLED_FONT_PATH:
                try:
                    font.set_variation_by_name("Bold" if bold else "Regular")
                except (AttributeError, OSError):
                    pass
            return font
    return ImageFont.load_default()


def _product_cutout(source: Image.Image) -> Image.Image:
    """Remove catalog-page whitespace while retaining a controlled soft edge."""
    image = ImageOps.exif_transpose(source).convert("RGBA")
    rgba = np.asarray(image)
    rgb = rgba[:, :, :3]
    source_alpha = rgba[:, :, 3]
    height, width = rgb.shape[:2]

    if float(np.mean(source_alpha < 250)) > 0.01:
        mask = (source_alpha > 12).astype(np.uint8)
    else:
        edge = max(2, min(height, width) // 50)
        border = np.vstack([
            rgb[:edge, :].reshape(-1, 3),
            rgb[-edge:, :].reshape(-1, 3),
            rgb[:, :edge].reshape(-1, 3),
            rgb[:, -edge:].reshape(-1, 3),
        ])
        background = np.median(border, axis=0)
        distance = np.linalg.norm(rgb.astype(np.float32) - background, axis=2)
        raw_mask = (distance > 18).astype(np.uint8)
        raw_mask = cv2.morphologyEx(raw_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(raw_mask, 8)
        if count <= 1:
            mask = raw_mask
        else:
            main_id = max(range(1, count), key=lambda index: int(stats[index, cv2.CC_STAT_AREA]))
            main_x, main_y, main_w, main_h, main_area = [int(value) for value in stats[main_id]]
            expand_x = max(8, int(main_w * 0.12))
            expand_y = max(8, int(main_h * 0.18))
            region = (
                max(0, main_x - expand_x),
                max(0, main_y - expand_y),
                min(width, main_x + main_w + expand_x),
                min(height, main_y + main_h + expand_y),
            )
            keep = [main_id]
            for index in range(1, count):
                if index == main_id:
                    continue
                x, y, component_width, component_height, area = [int(value) for value in stats[index]]
                intersects = x < region[2] and x + component_width > region[0] and y < region[3] and y + component_height > region[1]
                if intersects and area >= max(24, int(main_area * 0.004)):
                    keep.append(index)
            mask = np.isin(labels, keep).astype(np.uint8)

    ys, xs = np.where(mask > 0)
    if not len(xs):
        return image
    object_width = int(xs.max() - xs.min() + 1)
    object_height = int(ys.max() - ys.min() + 1)
    padding = max(3, int(max(object_width, object_height) * 0.015))
    left = max(0, int(xs.min()) - padding)
    top = max(0, int(ys.min()) - padding)
    right = min(width, int(xs.max()) + padding + 1)
    bottom = min(height, int(ys.max()) + padding + 1)
    cropped_rgb = rgb[top:bottom, left:right]
    cropped_mask = (mask[top:bottom, left:right] * 255).astype(np.uint8)
    cropped_mask = cv2.GaussianBlur(cropped_mask, (0, 0), 0.7)
    result = Image.fromarray(cropped_rgb, "RGB").convert("RGBA")
    result.putalpha(Image.fromarray(cropped_mask, "L"))
    return result


def _paste_product(canvas: Image.Image, source: Image.Image, box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = box
    cutout = _product_cutout(source)
    box_width = max(1, right - left)
    box_height = max(1, bottom - top)
    scale = min(box_width / cutout.width, box_height / cutout.height)
    rendered_size = (
        max(1, int(round(cutout.width * scale))),
        max(1, int(round(cutout.height * scale))),
    )
    rendered = cutout.resize(rendered_size, Image.Resampling.LANCZOS)
    x = left + (box_width - rendered.width) // 2
    y = top + (box_height - rendered.height) // 2
    canvas.paste(rendered.convert("RGB"), (x, y), rendered.getchannel("A"))


def _catalog_product_page(source: Image.Image) -> Image.Image:
    """Match the catalog reference with a stable white border and a lower visual center."""
    canvas = Image.new("RGB", (800, 800), "white")
    _paste_product(canvas, source, (120, 170, 680, 710))
    return canvas


def _dimension_mm(value: str) -> str:
    normalized = value.strip().lower().replace("，", ".")
    if not normalized:
        return "--mm"
    match = re.search(r"\d+(?:\.\d+)?", normalized)
    if not match:
        return value
    number = float(match.group(0))
    if "mm" not in normalized:
        number *= 10
    rendered = str(int(round(number))) if abs(number - round(number)) < 0.01 else f"{number:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}mm"


def _draw_rotated_text(canvas: Image.Image, text: str, xy: tuple[int, int], angle: float, font: ImageFont.ImageFont) -> None:
    box = font.getbbox(text)
    layer = Image.new("RGBA", (max(1, box[2] - box[0] + 12), max(1, box[3] - box[1] + 12)), (255, 255, 255, 0))
    ImageDraw.Draw(layer).text((6 - box[0], 6 - box[1]), text, font=font, fill="#555555")
    rotated = layer.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
    canvas.paste(rotated, xy, rotated)


def _info_page(info: dict[str, str], product_image: Image.Image | None = None) -> Image.Image:
    image = Image.new("RGB", (750, 665), "white")
    draw = ImageDraw.Draw(image)
    title = "产品信息"
    title_font = _font(33, True)
    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((750 - (title_box[2] - title_box[0])) / 2, 42), title, font=title_font, fill="#101010")

    rows = [
        ("材质", info.get("main_material") or "待填写"),
        ("里料", info.get("lining_material") or "待填写"),
        ("背法", info.get("wearing_method") or "待填写"),
    ]
    y = 216
    for label, value in rows:
        draw.text((45, y), label, font=_font(20, True), fill="#111111")
        draw.text((45, y + 34), value[:18], font=_font(19), fill="#555555")
        y += 96

    if product_image is not None:
        _paste_product(image, product_image, (346, 288, 633, 488))

    line_color = "#8a8a8a"
    draw.line((390, 486, 590, 486), fill=line_color, width=2)
    draw.line((390, 477, 390, 495), fill=line_color, width=2)
    draw.line((590, 477, 590, 495), fill=line_color, width=2)
    length_text = _dimension_mm(info.get("product_length") or "")
    length_box = draw.textbbox((0, 0), length_text, font=_font(18))
    draw.text((490 - (length_box[2] - length_box[0]) / 2, 500), length_text, font=_font(18), fill="#555555")

    draw.line((349, 285, 349, 454), fill=line_color, width=2)
    draw.line((340, 285, 358, 285), fill=line_color, width=2)
    draw.line((340, 454, 358, 454), fill=line_color, width=2)
    _draw_rotated_text(image, _dimension_mm(info.get("product_height") or ""), (299, 333), 90, _font(18))

    draw.line((634, 486, 690, 457), fill=line_color, width=2)
    draw.line((628, 478, 639, 493), fill=line_color, width=2)
    draw.line((685, 450, 696, 465), fill=line_color, width=2)
    _draw_rotated_text(image, _dimension_mm(info.get("product_width") or ""), (640, 493), 26, _font(18))

    disclaimer = info.get("disclaimer") or "包身长宽高测量均为最长部分\n误差在1-2cm之间因手工测量均属正常"
    notes = [line.strip() for line in disclaimer.splitlines() if line.strip()]
    if len(notes) < 2:
        notes = textwrap.wrap(disclaimer.replace("\n", " "), width=31)[:2]
    for index, line in enumerate(notes):
        draw.text((329, 580 + index * 27), f"* {line[:34]}", font=_font(15), fill="#222222")
    return image


def _model_showcase_page(source: Image.Image) -> Image.Image:
    """Match the 601-603 reference: cropped model photo with a fixed white frame."""
    canvas = Image.new("RGB", (750, 750), "white")
    rendered = ImageOps.fit(source.convert("RGB"), (638, 634), Image.Resampling.LANCZOS, centering=(0.5, 0.46))
    canvas.paste(rendered, (56, 65))
    return canvas


def _detail_showcase_page(source: Image.Image) -> Image.Image:
    """Match the 604-605 reference: title, white border and a large detail crop."""
    canvas = Image.new("RGB", (750, 750), "white")
    draw = ImageDraw.Draw(canvas)
    title = "细节展示"
    title_font = _font(34, True)
    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((750 - (title_box[2] - title_box[0])) / 2, 70), title, font=title_font, fill="#aaaaaa")
    rendered = ImageOps.fit(source.convert("RGB"), (640, 522), Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    canvas.paste(rendered, (55, 181))
    return canvas


def _multi_angle_page(image_ids: list[int]) -> Image.Image:
    canvas = Image.new("RGB", (750, 750), "white")
    draw = ImageDraw.Draw(canvas)
    title = "多角度展示"
    title_font = _font(35, True)
    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((750 - (title_box[2] - title_box[0])) / 2, 62), title, font=title_font, fill="#111111")
    boxes = [
        (78, 195, 323, 365),
        (427, 195, 672, 365),
        (78, 500, 323, 680),
        (427, 500, 672, 680),
    ]
    for image_id, box in zip(image_ids[:4], boxes):
        _paste_product(canvas, _load_image(image_id), box)
    draw.line((346, 420, 404, 420), fill="#a8a8a8", width=2)
    draw.line((375, 391, 375, 449), fill="#a8a8a8", width=2)
    return canvas


def _save_png_30(image: Image.Image, path: Path) -> None:
    rgba = ImageOps.contain(image.convert("RGBA"), (800, 800), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
    canvas.alpha_composite(rgba, ((800 - rgba.width) // 2, (800 - rgba.height) // 2))
    canvas.save(path, optimize=True)
    size = path.stat().st_size
    if size < 100_000:
        canvas.save(path, compress_level=0)
    elif size > 600_000:
        canvas.quantize(colors=256).save(path, optimize=True)


def export_package(session_id: str, slots: list[dict[str, Any]], product_info: dict[str, str]) -> dict[str, Any]:
    slot_map = {item["file_name"]: [int(value) for value in item.get("image_ids", [])] for item in slots}
    # 1.jpg and 50.jpg are two layouts of the same model photo.
    slot_map["50.jpg"] = list(slot_map.get("1.jpg", []))
    model_slots = {"1.jpg", "50.jpg", "601.jpg", "602.jpg", "603.jpg"}
    _validate_session_assets(session_id, {
        "model": [image_id for name in model_slots for image_id in slot_map.get(name, [])],
        "tag": slot_map.get("801.jpg", []),
        "product": [
            image_id
            for name, image_ids in slot_map.items()
            if name not in model_slots and name != "801.jpg"
            for image_id in image_ids
        ],
    })
    export_id = uuid.uuid4().hex[:12]
    session_result_dir = _session_result_dir(session_id)
    folder = session_result_dir / export_id
    folder.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []

    for file_name, _, _, kind in SLOT_DEFINITIONS:
        ids = slot_map.get(file_name, [])
        output = folder / file_name
        if file_name == "401.jpg":
            source = _load_image(ids[0]) if ids else None
            _info_page(product_info, source).save(output, quality=98, subsampling=0)
        elif file_name == "606.jpg":
            if len(ids) < 4:
                missing.append(file_name)
                continue
            _multi_angle_page(ids).save(output, quality=98, subsampling=0)
        elif not ids:
            missing.append(file_name)
        elif file_name == "30.png":
            _save_png_30(_load_image(ids[0]), output)
        elif file_name == "50.jpg":
            _fit(_load_image(ids[0]), (950, 1200)).save(output, quality=94)
        elif file_name in {"601.jpg", "602.jpg", "603.jpg"}:
            _model_showcase_page(_load_image(ids[0])).save(output, quality=94)
        elif file_name in {"604.jpg", "605.jpg"}:
            _detail_showcase_page(_load_image(ids[0])).save(output, quality=94)
        elif file_name == "801.jpg":
            _fit(_load_image(ids[0]), (750, 750), margin=40, contain=True).save(output, quality=94)
        elif file_name in {"2.jpg", "3.jpg"}:
            _catalog_product_page(_load_image(ids[0])).save(output, quality=98, subsampling=0)
        else:
            _fit(_load_image(ids[0]), (800, 800)).save(output, quality=94)

    zip_path = session_result_dir / f"唯品会套图_{export_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.iterdir()):
            archive.write(path, arcname=path.name)
    previews = [
        f"/api/vip-organizer/exports/{session_id}/{export_id}/files/{path.name}"
        for path in sorted(folder.iterdir())
        if path.suffix.lower() in {".jpg", ".png"}
    ]
    return {
        "download_url": f"/api/vip-organizer/exports/{session_id}/{export_id}/download",
        "previews": previews,
        "generated_count": len(previews),
        "missing": missing,
    }


def export_file(session_id: str, export_id: str, file_name: str) -> Path:
    if not _valid_session_id(session_id) or not re.fullmatch(r"[0-9a-f]{12}", export_id) or not re.fullmatch(r"[0-9]+\.(?:jpg|png)", file_name):
        raise ValueError("导出文件不存在")
    path = _session_result_dir(session_id) / export_id / file_name
    if not path.is_file():
        raise ValueError("导出文件不存在")
    return path


def export_zip(session_id: str, export_id: str) -> Path:
    if not _valid_session_id(session_id) or not re.fullmatch(r"[0-9a-f]{12}", export_id):
        raise ValueError("导出文件不存在")
    path = _session_result_dir(session_id) / f"唯品会套图_{export_id}.zip"
    if not path.is_file():
        raise ValueError("导出文件不存在")
    return path
