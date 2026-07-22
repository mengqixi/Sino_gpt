from __future__ import annotations

import mimetypes
import base64
import hashlib
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
from threading import Lock
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
JD_LOGO_FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "LibreBodoni-VariableFont_wght.ttf"
JD_PHONE_REFERENCE_PATH = Path(__file__).resolve().parents[1] / "assets" / "iphone_reference.png"
_PREVIEW_LOCKS_GUARD = Lock()
_PREVIEW_LOCKS: dict[str, Lock] = {}
PREVIEW_RENDER_VERSION = 10
MAX_PREVIEW_CACHE_ENTRIES = 96
JD_PHONE_HEIGHT_MM = 163.0
JD_PHONE_LABEL = "iPhone 17 Pro Max"


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
JD_SLOT_DEFINITIONS = [
    ("0-无logo.jpg", "模特主图（无Logo）", "800×800", "model"),
    ("1.jpg", "模特主图（含Logo）", "800×800 + 750×1000", "model"),
    ("2.jpg", "半侧产品图（含Logo）", "800×800 + 750×1000", "product"),
    ("3.jpg", "ELLE Logo细节（含Logo）", "800×800 + 750×1000", "product"),
    ("4.jpg", "内里细节（含Logo）", "800×800 + 750×1000", "product"),
    ("5.jpg", "尺寸与手机对比（含Logo）", "800×800 + 750×1000", "generated"),
    ("透明.png", "正面透明底", "800×800", "product"),
]
ORGANIZER_PLATFORMS = {"vip", "jd"}
INFO_PRODUCT_BOX = (346, 258, 633, 458)
INFO_LENGTH_LINE_Y = 486

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


def _platform_slot_definitions(platform: str) -> list[tuple[str, str, str, str]]:
    if platform not in ORGANIZER_PLATFORMS:
        raise ValueError("不支持的输出平台")
    return JD_SLOT_DEFINITIONS if platform == "jd" else SLOT_DEFINITIONS


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


def _api_analysis_prompt() -> str:
    role_text = (
        "front正面主图, semi_side半侧面或三分之二角度, side完整侧面, back背面, "
        "top顶部或开口全景, bottom完整包底, transparent透明底正面, "
        "strap完整肩带或链条展示, detail局部细节"
    )
    tag_text = (
        "logo ELLE Logo, hardware五金, strap_chain肩带或链条, "
        "zipper_opening拉链或开口, interior内里, inner_pocket_label内袋或内标, "
        "material_texture材质或纹理, bottom_detail包底细节"
    )
    return (
        "你是ELLE女包电商素材分类员。请综合比较同批图片后逐张分类，"
        f"主类别只能从以下固定角色中选择：{role_text}。每张图片只能有一个主类别，"
        f"并可从以下细节标签中选择零个或多个：{tag_text}。"
        "必须按输入顺序返回全部图片，每个index只出现一次，不得遗漏。\n"
        "【同批必备视图约束】每一批商品原图必定至少包含一张front、一张semi_side、"
        "一张side、一张top和一张transparent。必须先在全批图片中比较并找出这五张，"
        "五个角色必须分配给五个不同index，不得把同一张图重复用于多个必备角色。"
        "透明文件名、透明通道或明确透明底是transparent的重要证据；完整开口俯视图即使"
        "中央可见Logo或五金也应归为top。其余图片再分类为back、bottom、strap或detail。\n"
        "完整视图规则：front是完整正面，包身正面、包口、Logo或主要五金朝向镜头；"
        "semi_side必须同时看到正面和一侧厚度，包体轮廓存在真实透视；"
        "side只看到狭窄侧廓或包体厚度；back是完整背面。"
        "不能因为肩带、链条、挂件横向铺开，或一侧配件较多，就把正面误判为semi_side。"
        "不能只因没有明显Logo就把图片判为back，应与同批相同包型的正面、半侧和背面互相比较。"
        "同款完整视图中，主体最窄且主要展示厚度的通常是side；"
        "能看到一侧厚度且仍保留大部分正面的通常是semi_side；"
        "中央Logo或主五金朝向镜头的通常是front；相同轮廓但背部结构朝向镜头的才是back。\n"
        "特殊完整视图规则：top是完整包口或开口俯视全景；bottom是完整包底平面或仰拍全景，"
        "不要仅因主体横向扁平就判断为bottom；transparent只用于确有透明通道或透明底素材；"
        "strap只用于整条肩带或链条本身是主要展示对象的图片。\n"
        "局部细节规则：只有包身被裁切、局部被明显放大时才使用detail。"
        "ELLE金属字标或铭牌近景添加logo和hardware；扣件、铆钉、链条连接件添加hardware；"
        "肩带或链条近景添加strap_chain；拉链、包口近景添加zipper_opening；"
        "出现包内空间或内衬添加interior，内袋或内标近景再添加inner_pocket_label；"
        "面料、压纹、缝线近景添加material_texture；包脚、底部缝线等局部添加bottom_detail。"
        "细节标签允许多选，例如ELLE金属Logo面料近景可同时使用logo、hardware、material_texture。"
        "完整产品图即使可见Logo、五金或链条，主类别仍应是对应完整视图，不得改为detail。\n"
        "本地初判已结合轻量图像特征和同批相对校正，只是参考。画面证据明确时可以纠正本地初判；"
        "无法可靠判断时降低confidence，并在reason中写清不确定点。"
        "仅返回JSON对象，不要Markdown，格式："
        '{"items":[{"index":1,"role":"front","tags":["logo","hardware"],'
        '"confidence":90,"reason":"简短但具体的中文理由"}]}。'
    )


def analyze_assets_with_api(session_id: str, product_image_ids: list[int], api_config_id: int) -> dict[str, Any]:
    _validate_session_assets(session_id, {"product": product_image_ids})
    rows = _uploaded_rows(product_image_ids)
    if not rows:
        raise ValueError("请至少上传一张商品原图")
    config = _analysis_config(api_config_id)
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": _api_analysis_prompt(),
    }]
    local_items: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        metrics = _image_metrics(row)
        local_role, local_tags, local_confidence, local_reason = _classify_product_metrics(metrics)
        local_items.append({
            **metrics,
            "id": index,
            "suggested_role": local_role,
            "suggested_tags": local_tags,
            "role_confidence": local_confidence,
            "role_reason": local_reason,
        })
    _refine_product_classifications(local_items)
    local_hints = {
        int(item["id"]): (
            str(item["suggested_role"]),
            list(item["suggested_tags"]),
            int(item["role_confidence"]),
            str(item["role_reason"]),
        )
        for item in local_items
    }
    for index, row in enumerate(rows, start=1):
        local_role, local_tags, local_confidence, local_reason = local_hints[index]
        local_tag_text = "、".join(local_tags) if local_tags else "无"
        content.append({
            "type": "text",
            "text": (
                f"图片 {index}，文件名：{row['file_name']}。"
                f"同批本地校正参考：{local_role}（{local_confidence}%），"
                f"细节标签：{local_tag_text}，理由：{local_reason}。请以画面证据作最终判断。"
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
    api_results: dict[int, dict[str, Any]] = {}
    for item in parsed.get("items", []):
        index = int(item.get("index", 0))
        role = str(item.get("role", "detail"))
        raw_tags = item.get("tags", [])
        tags = [str(tag) for tag in raw_tags if str(tag) in DETAIL_TAGS] if isinstance(raw_tags, list) else []
        if role == "logo":
            role, tags = "detail", list(dict.fromkeys([*tags, "logo", "hardware"]))
        elif role == "interior":
            role, tags = "detail", list(dict.fromkeys([*tags, "interior"]))
        if local_hints.get(index, ("", [], 0, ""))[0] == "transparent":
            role = "transparent"
        if 1 <= index <= len(rows) and role in API_ANALYSIS_ROLES:
            api_results[index] = {
                "role": role,
                "tags": tags,
                "confidence": max(0, min(100, int(item.get("confidence", 0)))),
                "reason": str(item.get("reason", ""))[:160],
            }
    if not api_results:
        raise ValueError("素材分析 API 没有返回有效分类")

    for local_item in local_items:
        index = int(local_item["id"])
        api_item = api_results.get(index)
        if not api_item:
            continue
        local_item.update({
            "suggested_role": api_item["role"],
            "suggested_tags": api_item["tags"],
            "role_confidence": api_item["confidence"],
            "role_reason": f"API：{api_item['reason']}",
        })
    _refine_product_classifications(local_items)

    results: list[dict[str, Any]] = []
    roles: dict[int, str] = {}
    tags_by_image: dict[int, list[str]] = {}
    for local_item in local_items:
        index = int(local_item["id"])
        if not 1 <= index <= len(rows):
            continue
        image_id = int(rows[index - 1]["id"])
        role = str(local_item["suggested_role"])
        tags = [str(tag) for tag in local_item.get("suggested_tags", []) if str(tag) in DETAIL_TAGS]
        roles[image_id] = role
        tags_by_image[image_id] = tags
        results.append({
            "image_id": image_id,
            "file_name": rows[index - 1]["file_name"],
            "role": role,
            "tags": tags,
            "confidence": max(0, min(100, int(local_item.get("role_confidence", 0)))),
            "reason": str(local_item.get("role_reason", ""))[:160],
        })
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


def _batch_view_candidates(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in products:
        role = str(item.get("suggested_role", ""))
        ratio = float(item.get("main_component_ratio", item.get("object_ratio", 1)))
        fill = float(item.get("main_component_fill_ratio", item.get("foreground_fill_ratio", 0)))
        foreground = float(item.get("foreground_ratio", 0))
        bbox = float(item.get("bbox_ratio", 0))
        if role in {"transparent", "strap", "bottom"}:
            continue
        if (
            0.22 <= ratio <= 1.70
            and fill >= 0.38
            and 0.025 <= foreground < 0.30
            and bbox < 0.48
        ):
            candidates.append(item)
    return candidates


def _assign_required_batch_views(products: list[dict[str, Any]]) -> None:
    """Assign the five views guaranteed to exist in every complete product batch."""
    if len(products) < 5:
        return

    transparent_candidates = [
        item for item in products
        if (
            float(item.get("alpha_ratio", 0)) > 0.02
            or "透明" in str(item.get("file_name", ""))
            or item.get("suggested_role") == "transparent"
        )
    ]
    complete = _batch_view_candidates(products)
    if not transparent_candidates or len(complete) < 4:
        return
    side_candidates = [
        item for item in complete
        if float(item.get("main_component_ratio", item.get("object_ratio", 1))) <= 0.58
        or item.get("suggested_role") == "side"
    ]
    if not side_candidates:
        return

    protected_back_ids = {
        int(item.get("id", id(item)))
        for item in complete
        if item.get("suggested_role") == "back"
    }
    if protected_back_ids and len([
        item for item in complete
        if int(item.get("id", id(item))) not in protected_back_ids
    ]) < 4:
        return

    selected: dict[str, dict[str, Any]] = {}
    selected_ids: set[int] = set(protected_back_ids)

    def item_id(item: dict[str, Any]) -> int:
        return int(item.get("id", id(item)))

    def available() -> list[dict[str, Any]]:
        return [item for item in complete if item_id(item) not in selected_ids]

    transparent = max(
        transparent_candidates,
        key=lambda item: (
            5.0 if "透明" in str(item.get("file_name", "")) else 0.0
        ) + float(item.get("alpha_ratio", 0)) * 20.0
        + (3.0 if item.get("suggested_role") == "transparent" else 0.0),
    )
    selected["transparent"] = transparent
    selected_ids.add(item_id(transparent))

    side = min(
        [item for item in side_candidates if item_id(item) not in selected_ids],
        key=lambda item: (
            float(item.get("main_component_ratio", item.get("object_ratio", 1)))
            - (0.18 if item.get("suggested_role") == "side" else 0.0)
        ),
    )
    selected["side"] = side
    selected_ids.add(item_id(side))

    def top_score(item: dict[str, Any]) -> float:
        ratio = float(item.get("main_component_ratio", item.get("object_ratio", 1)))
        symmetry = float(item.get("main_symmetry_error", 0))
        side_edge = min(4.0, float(item.get("main_body_side_edge_ratio", 4.0)))
        center_gold = min(0.08, float(item.get("center_gold_ratio", 0)))
        tags = set(item.get("suggested_tags", []))
        return (
            (7.0 if item.get("suggested_role") == "top" else 0.0)
            + (2.0 if {"zipper_opening", "interior"} & tags else 0.0)
            + symmetry * 5.0
            - abs(ratio - 0.82) * 3.5
            - side_edge * 1.2
            + center_gold * 18.0
        )

    top = max(available(), key=top_score)
    selected["top"] = top
    selected_ids.add(item_id(top))

    def semi_side_score(item: dict[str, Any]) -> float:
        ratio = float(item.get("main_component_ratio", item.get("object_ratio", 1)))
        symmetry = float(item.get("main_symmetry_error", 0))
        side_edge = min(6.0, float(item.get("main_body_side_edge_ratio", 6.0)))
        return (
            (7.0 if item.get("suggested_role") == "semi_side" else 0.0)
            + min(2.0, side_edge * 0.45)
            + symmetry * 2.0
            - abs(ratio - 1.08) * 2.5
        )

    semi_side = max(available(), key=semi_side_score)
    selected["semi_side"] = semi_side
    selected_ids.add(item_id(semi_side))

    def front_score(item: dict[str, Any]) -> float:
        ratio = float(item.get("main_component_ratio", item.get("object_ratio", 1)))
        fill = float(item.get("main_component_fill_ratio", item.get("foreground_fill_ratio", 0)))
        symmetry = float(item.get("main_symmetry_error", 0))
        strict_gold = min(0.012, float(item.get("strict_center_gold_ratio", 0)))
        return (
            (2.0 if item.get("suggested_role") == "front" else 0.0)
            + fill * 7.0
            - symmetry * 5.0
            - abs(ratio - 1.20) * 2.0
            + strict_gold * 220.0
        )

    front = max(available(), key=front_score)
    selected["front"] = front

    role_details = {
        "transparent": (99, [], "同批必备视图约束：检测到透明文件名、透明通道或透明底，确定为透明正面"),
        "side": (94, [], "同批必备视图约束：完整包体宽厚比最窄，确定为完整侧面"),
        "top": (92, ["zipper_opening", "interior"], "同批必备视图约束：俯拍开口、内部轮廓与透视特征最明显，确定为顶部开口全景"),
        "semi_side": (90, [], "同批必备视图约束：同时保留正面主体和一侧厚度，确定为半侧面"),
        "front": (88, [], "同批必备视图约束：完整正面轮廓、居中结构与正面Logo特征最匹配，确定为正面主图"),
    }
    for role, item in selected.items():
        confidence, default_tags, reason = role_details[role]
        tags = list(dict.fromkeys([*item.get("suggested_tags", []), *default_tags]))
        if role in {"side", "semi_side", "front"}:
            tags = [tag for tag in tags if tag not in {"interior", "inner_pocket_label"}]
        item.update({
            "suggested_role": role,
            "suggested_tags": tags,
            "role_confidence": max(confidence, int(item.get("role_confidence", 0))),
            "role_reason": reason,
        })


def _refine_product_classifications(products: list[dict[str, Any]]) -> None:
    """Use relative geometry within one product batch to correct obvious view mix-ups."""
    full_roles = {"front", "semi_side", "side", "back", "top"}
    candidates = [item for item in products if item.get("suggested_role") in full_roles]
    if len(candidates) < 3:
        _assign_required_batch_views(products)
        return

    regular = [
        item for item in candidates
        if float(item.get("main_component_fill_ratio", 0)) >= 0.45
        and float(item.get("bbox_ratio", 0)) < 0.48
    ]
    if len(regular) < 3:
        _assign_required_batch_views(products)
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
        _assign_required_batch_views(products)
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

    if not any(item.get("suggested_role") == "back" for item in face_candidates):
        fallback_back = min(
            face_candidates,
            key=lambda item: (
                int(item.get("role_confidence", 0)),
                float(item.get("strict_center_gold_ratio", 0)),
                -float(item.get("sharpness", 0)),
            ),
        )
        fallback_back.update({
            "suggested_role": "back",
            "suggested_tags": [
                tag for tag in fallback_back.get("suggested_tags", [])
                if tag not in {"logo", "hardware"}
            ],
            "role_confidence": int(fallback_back.get("role_confidence", 0)),
            "role_reason": "同批完整包体图中未识别到明确背面，暂将自动判断置信度最低的一张作为背面候选，建议人工确认",
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
    _assign_required_batch_views(products)


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
    with _PREVIEW_LOCKS_GUARD:
        _PREVIEW_LOCKS.pop(session_id, None)


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
    platform: str = "vip",
) -> dict[str, Any]:
    slot_definitions = _platform_slot_definitions(platform)
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

    def view_ratio(item: dict[str, Any]) -> float:
        return float(item.get("main_component_ratio", item.get("object_ratio", 1.0)))

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

    regular_views = [item for item in full_views if 0.9 <= view_ratio(item) <= 1.8]
    fixed_front_views = [item for item in usable_products if role_overrides.get(item["id"]) == "front"]
    labeled_front_views = fixed_front_views or [item for item in regular_views if effective_role(item) == "front"]
    front_view = max(
        labeled_front_views or regular_views or full_views,
        key=lambda item: (
            item["role_confidence"],
            item["foreground_fill_ratio"],
            -abs(view_ratio(item) - 1.30),
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
    side_candidates = [item for item in full_views if view_ratio(item) < 0.75 and item["foreground_ratio"] >= 0.04]
    side_candidates.sort(key=lambda item: (abs(view_ratio(item) - 0.45), -item["sharpness"]))
    side_view = assigned("side") or (side_candidates[0] if side_candidates else min(full_views, key=view_ratio))
    side_id = side_view["id"]
    semi_side_candidates = [
        item
        for item in full_views
        if item["id"] not in {front_view["id"], back_view["id"]}
        and 1.02 <= view_ratio(item) <= 1.26
        and item["foreground_fill_ratio"] >= 0.52
    ]
    semi_side_candidates.sort(key=lambda item: (
        abs(view_ratio(item) - 1.16),
        -item["foreground_fill_ratio"],
        -item["sharpness"],
    ))
    angle_view = assigned("semi_side") or (semi_side_candidates[0] if semi_side_candidates else side_view)
    angle_role = "semi_side" if effective_role(angle_view) == "semi_side" else "side"
    top_candidates = [
        item for item in full_views
        if item["id"] not in {front_view["id"], back_view["id"], side_id, angle_view["id"]}
        and 0.72 <= view_ratio(item) <= 0.95
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
        "604.jpg": ([interior_id], selection_confidence(interior_view, tag="interior"), "与15.jpg使用同一张已确认的内里或结构细节图，并套用详情模板"),
        "605.jpg": ([logo_id], selection_confidence(logo_view, tag="logo"), "复用ELLE Logo清晰近景候选"),
        "606.jpg": ([front_view["id"], angle_view["id"], back_id, top_id], min(
            selection_confidence(front_view, role="front"),
            selection_confidence(angle_view, role=angle_role),
            selection_confidence(back_view, role="back"),
            selection_confidence(top_view, role="top"),
        ), "按正面、半侧面或全侧、背面、开口顶视图的固定顺序生成四角度模板"),
        "801.jpg": (tag_ids, 95 if tags else 0, "使用独立上传的吊牌图片" if tags else "尚未上传吊牌图片"),
    }
    if platform == "jd":
        slot_values = {
            "0-无logo.jpg": (model_pick(0), 88 if models else 0, "京东800目录模特主图，不叠加ELLE角标"),
            "1.jpg": (model_pick(0), 88 if models else 0, "与0-无logo.jpg使用同一张模特图，叠加京东ELLE角标"),
            "2.jpg": ([angle_view["id"]], selection_confidence(angle_view, role=angle_role), "优先使用半侧面或三分之二角度产品图"),
            "3.jpg": ([logo_id], selection_confidence(logo_view, tag="logo"), "优先使用ELLE Logo清晰可见的局部细节"),
            "4.jpg": ([interior_id], selection_confidence(interior_view, tag="interior"), "优先使用内里、开口或内袋细节"),
            "5.jpg": ([front_view["id"]], selection_confidence(front_view, role="front"), "使用完整产品图生成尺寸与手机对比模板"),
            "透明.png": ([front_id], 98 if has_transparent else 35, "检测到透明通道" if has_transparent else "未检测到透明图，暂用正面候选图"),
        }
    slots = [
        _slot(file_name, title, size, kind, *slot_values[file_name])
        for file_name, title, size, kind in slot_definitions
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


@lru_cache(maxsize=64)
def _load_image_file(image_id: int, file_path: str, modified_ns: int) -> Image.Image:
    with Image.open(file_path) as source:
        source.draft("RGB", (2400, 2400))
        image = ImageOps.exif_transpose(source)
        image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
        loaded = image.copy()
    loaded.info["_organizer_image_id"] = image_id
    loaded.info["_organizer_modified_ns"] = modified_ns
    return loaded


def _load_image(image_id: int) -> Image.Image:
    rows = _uploaded_rows([image_id])
    if not rows:
        raise ValueError(f"图片记录不存在：{image_id}")
    file_path = Path(rows[0]["file_path"])
    if not file_path.exists():
        raise ValueError(f"图片文件不存在：{image_id}")
    return _load_image_file(image_id, str(file_path), file_path.stat().st_mtime_ns).copy()


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


def _normalize_adjustment(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value or {}

    def number(name: str, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value.get(name, default))
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    crop_x = number("crop_x", 0.0, 0.0, 0.98)
    crop_y = number("crop_y", 0.0, 0.0, 0.98)
    crop_width = number("crop_width", 1.0, 0.02, 1.0 - crop_x)
    crop_height = number("crop_height", 1.0, 0.02, 1.0 - crop_y)
    return {
        "zoom": number("zoom", 1.0, 0.5, 4.0),
        "offset_x": number("offset_x", 0.0, -1.5, 1.5),
        "offset_y": number("offset_y", 0.0, -1.5, 1.5),
        "crop_x": crop_x,
        "crop_y": crop_y,
        "crop_width": crop_width,
        "crop_height": crop_height,
        "phone_scale": number("phone_scale", 1.0, 0.5, 1.8),
        "phone_offset_x": number("phone_offset_x", 0.0, -1.5, 1.5),
        "phone_offset_y": number("phone_offset_y", 0.0, -1.5, 1.5),
        "phone_alignment": "bottom" if value.get("phone_alignment") == "bottom" else "center",
        "product_show_ruler": value.get("product_show_ruler") is not False,
        "phone_show_ruler": value.get("phone_show_ruler") is not False,
    }


def _crop_source(source: Image.Image, adjustment: dict[str, Any] | None) -> Image.Image:
    normalized = _normalize_adjustment(adjustment)
    width, height = source.size
    left = int(round(normalized["crop_x"] * width))
    top = int(round(normalized["crop_y"] * height))
    right = int(round((normalized["crop_x"] + normalized["crop_width"]) * width))
    bottom = int(round((normalized["crop_y"] + normalized["crop_height"]) * height))
    right = max(left + 1, min(width, right))
    bottom = max(top + 1, min(height, bottom))
    return source.crop((left, top, right, bottom))


def _has_manual_crop(adjustment: dict[str, Any] | None) -> bool:
    normalized = _normalize_adjustment(adjustment)
    return (
        normalized["crop_x"] > 0.0001
        or normalized["crop_y"] > 0.0001
        or normalized["crop_width"] < 0.9999
        or normalized["crop_height"] < 0.9999
    )


def _crop_aware_mode(adjustment: dict[str, Any] | None, default: str) -> str:
    return "contain" if _has_manual_crop(adjustment) else default


def _has_manual_layout_adjustment(adjustment: dict[str, Any] | None) -> bool:
    normalized = _normalize_adjustment(adjustment)
    return (
        _has_manual_crop(adjustment)
        or abs(normalized["zoom"] - 1.0) > 0.0001
        or abs(normalized["offset_x"]) > 0.0001
        or abs(normalized["offset_y"]) > 0.0001
    )


def _crop_cache_key(adjustment: dict[str, Any] | None) -> tuple[int, int, int, int]:
    normalized = _normalize_adjustment(adjustment)
    return tuple(
        int(round(normalized[name] * 1_000_000))
        for name in ("crop_x", "crop_y", "crop_width", "crop_height")
    )


@lru_cache(maxsize=96)
def _cached_product_cutout(
    image_id: int,
    modified_ns: int,
    crop_key: tuple[int, int, int, int],
) -> Image.Image:
    crop_x, crop_y, crop_width, crop_height = (value / 1_000_000 for value in crop_key)
    source = _load_image(image_id)
    cropped = _crop_source(source, {
        "crop_x": crop_x,
        "crop_y": crop_y,
        "crop_width": crop_width,
        "crop_height": crop_height,
    })
    return _product_cutout(cropped)


def _paste_layer(
    canvas: Image.Image,
    layer: Image.Image,
    box: tuple[int, int, int, int],
    adjustment: dict[str, Any] | None = None,
    *,
    mode: str = "contain",
    clip_box: tuple[int, int, int, int] | None = None,
) -> None:
    left, top, right, bottom = box
    box_width = max(1, right - left)
    box_height = max(1, bottom - top)
    clip_left, clip_top, clip_right, clip_bottom = clip_box or box
    clip_width = max(1, clip_right - clip_left)
    clip_height = max(1, clip_bottom - clip_top)
    normalized = _normalize_adjustment(adjustment)
    if mode == "cover":
        base_scale = max(box_width / layer.width, box_height / layer.height)
    else:
        base_scale = min(box_width / layer.width, box_height / layer.height)
    scale = base_scale * normalized["zoom"]
    rendered_size = (
        max(1, int(round(layer.width * scale))),
        max(1, int(round(layer.height * scale))),
    )
    rendered = layer.resize(rendered_size, Image.Resampling.LANCZOS)
    global_x = left + (box_width - rendered.width) // 2 + int(round(normalized["offset_x"] * box_width))
    global_y = top + (box_height - rendered.height) // 2 + int(round(normalized["offset_y"] * box_height))
    if rendered.width <= clip_width:
        global_x = max(clip_left, min(global_x, clip_right - rendered.width))
    if rendered.height <= clip_height:
        global_y = max(clip_top, min(global_y, clip_bottom - rendered.height))
    x = global_x - clip_left
    y = global_y - clip_top
    region_mode = "RGBA" if canvas.mode == "RGBA" or rendered.mode == "RGBA" else "RGB"
    region_background = (255, 255, 255, 0) if region_mode == "RGBA" else "white"
    region = Image.new(region_mode, (clip_width, clip_height), region_background)
    if region_mode == "RGBA":
        layer_rgba = rendered.convert("RGBA")
        region.alpha_composite(layer_rgba, (x, y))
    else:
        region.paste(rendered.convert("RGB"), (x, y))
    if canvas.mode == "RGBA":
        canvas.alpha_composite(region.convert("RGBA"), (clip_left, clip_top))
    elif region.mode == "RGBA":
        canvas.paste(region.convert("RGB"), (clip_left, clip_top), region.getchannel("A"))
    else:
        canvas.paste(region, (clip_left, clip_top))


def _expanded_safe_box(
    box: tuple[int, int, int, int],
    canvas_size: tuple[int, int],
    *,
    padding_ratio: float = 0.055,
) -> tuple[int, int, int, int]:
    width, height = canvas_size
    left, top, right, bottom = box
    pad_x = max(18, round(width * padding_ratio))
    pad_y = max(18, round(height * padding_ratio))
    margin_x = round(width * 0.04)
    margin_y = round(height * 0.04)
    return (
        max(margin_x, left - pad_x),
        max(margin_y, top - pad_y),
        min(width - margin_x, right + pad_x),
        min(height - margin_y, bottom + pad_y),
    )


def _has_light_studio_border(source: Image.Image) -> bool:
    preview = source.convert("RGB")
    preview.thumbnail((96, 96), Image.Resampling.BILINEAR)
    pixels = np.asarray(preview, dtype=np.uint8)
    border_size = max(2, min(pixels.shape[:2]) // 18)
    border = np.concatenate((
        pixels[:border_size].reshape(-1, 3),
        pixels[-border_size:].reshape(-1, 3),
        pixels[:, :border_size].reshape(-1, 3),
        pixels[:, -border_size:].reshape(-1, 3),
    ))
    bright = border.min(axis=1) >= 232
    neutral = border.max(axis=1) - border.min(axis=1) <= 22
    return float(np.mean(bright & neutral)) >= 0.78


def _paste_detail_layer(
    canvas: Image.Image,
    source: Image.Image,
    box: tuple[int, int, int, int],
    adjustment: dict[str, Any] | None,
    *,
    clip_box: tuple[int, int, int, int] | None = None,
    auto_zoom: float = 0.9,
    auto_offset_y: float = -0.045,
    default_mode: str = "cover",
) -> None:
    cropped = _crop_source(source, adjustment)
    if not _has_manual_layout_adjustment(adjustment) and _has_light_studio_border(cropped):
        cutout = _product_cutout(cropped)
        _paste_layer(
            canvas,
            cutout,
            box,
            {"zoom": auto_zoom, "offset_y": auto_offset_y},
            mode="contain",
            clip_box=clip_box,
        )
        return
    _paste_layer(
        canvas,
        cropped.convert("RGB"),
        box,
        adjustment,
        mode=_crop_aware_mode(adjustment, default_mode),
        clip_box=clip_box,
    )


def _paste_product(
    canvas: Image.Image,
    source: Image.Image,
    box: tuple[int, int, int, int],
    adjustment: dict[str, Any] | None = None,
    *,
    clip_box: tuple[int, int, int, int] | None = None,
) -> None:
    image_id = source.info.get("_organizer_image_id")
    modified_ns = source.info.get("_organizer_modified_ns")
    if isinstance(image_id, int) and isinstance(modified_ns, int):
        cutout = _cached_product_cutout(
            image_id,
            modified_ns,
            _crop_cache_key(adjustment),
        ).copy()
    else:
        cutout = _product_cutout(_crop_source(source, adjustment))
    _paste_layer(canvas, cutout, box, adjustment, clip_box=clip_box)


def _paste_product_floating(
    canvas: Image.Image,
    source: Image.Image,
    box: tuple[int, int, int, int],
    adjustment: dict[str, Any] | None = None,
) -> None:
    """Use the template box for scale/position without clipping manual movement to it."""
    left, top, right, bottom = box
    box_width = max(1, right - left)
    box_height = max(1, bottom - top)
    normalized = _normalize_adjustment(adjustment)
    cutout = _product_cutout(_crop_source(source, adjustment))
    scale = min(box_width / cutout.width, box_height / cutout.height) * normalized["zoom"]
    rendered = cutout.resize(
        (max(1, round(cutout.width * scale)), max(1, round(cutout.height * scale))),
        Image.Resampling.LANCZOS,
    )
    x = left + (box_width - rendered.width) // 2 + round(normalized["offset_x"] * box_width)
    y = top + (box_height - rendered.height) // 2 + round(normalized["offset_y"] * box_height)
    safe_left = round(canvas.width * 0.04)
    safe_top = round(canvas.height * 0.04)
    safe_right = round(canvas.width * 0.96)
    safe_bottom = round(canvas.height * 0.96)
    x = max(safe_left, min(x, safe_right - rendered.width))
    y = max(safe_top, min(y, safe_bottom - rendered.height))
    if canvas.mode == "RGBA":
        canvas.alpha_composite(rendered, (x, y))
    else:
        canvas.paste(rendered.convert("RGB"), (x, y), rendered.getchannel("A"))


def _normalized_product_page(
    source: Image.Image,
    size: tuple[int, int] = (800, 800),
    box: tuple[int, int, int, int] | None = None,
    transparent: bool = False,
    adjustment: dict[str, Any] | None = None,
) -> Image.Image:
    """Normalize non-model assets into a stable safe area regardless of source whitespace."""
    width, height = size
    safe_box = box or (
        round(width * 0.15),
        round(height * 0.2125),
        round(width * 0.85),
        round(height * 0.8875),
    )
    background = (255, 255, 255, 0) if transparent else "white"
    canvas = Image.new("RGBA" if transparent else "RGB", size, background)
    clip_box = _expanded_safe_box(safe_box, size) if _has_manual_layout_adjustment(adjustment) else None
    _paste_product(canvas, source, safe_box, adjustment, clip_box=clip_box)
    return canvas


def _catalog_product_page(source: Image.Image, adjustment: dict[str, Any] | None = None) -> Image.Image:
    """Match the catalog reference with a stable white border and a lower visual center."""
    return _normalized_product_page(source, adjustment=adjustment)


def _dimension_value_mm(value: str | None) -> float | None:
    normalized = str(value or "").strip().lower().replace("，", ".")
    if not normalized:
        return None
    match = re.search(r"\d+(?:\.\d+)?", normalized)
    if not match:
        return None
    number = float(match.group(0))
    if "mm" not in normalized:
        number *= 10
    return number


def _dimension_mm(value: str) -> str:
    number = _dimension_value_mm(value)
    if number is None:
        return value or "--mm"
    rendered = str(int(round(number))) if abs(number - round(number)) < 0.01 else f"{number:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}mm"


def _draw_rotated_text(canvas: Image.Image, text: str, xy: tuple[int, int], angle: float, font: ImageFont.ImageFont) -> None:
    box = font.getbbox(text)
    layer = Image.new("RGBA", (max(1, box[2] - box[0] + 12), max(1, box[3] - box[1] + 12)), (255, 255, 255, 0))
    ImageDraw.Draw(layer).text((6 - box[0], 6 - box[1]), text, font=font, fill="#555555")
    rotated = layer.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
    canvas.paste(rotated, xy, rotated)


def _info_page(
    info: dict[str, str],
    product_image: Image.Image | None = None,
    adjustment: dict[str, Any] | None = None,
) -> Image.Image:
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
        if _has_manual_layout_adjustment(adjustment):
            _paste_product_floating(image, product_image, INFO_PRODUCT_BOX, adjustment)
        else:
            _paste_product(image, product_image, INFO_PRODUCT_BOX, adjustment)

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


def _model_showcase_page(source: Image.Image, adjustment: dict[str, Any] | None = None) -> Image.Image:
    """Match the 601-603 reference: cropped model photo with a fixed white frame."""
    canvas = Image.new("RGB", (750, 750), "white")
    cropped = _crop_source(source.convert("RGB"), adjustment)
    box = (56, 65, 694, 699)
    clip_box = _expanded_safe_box(box, canvas.size) if _has_manual_layout_adjustment(adjustment) else None
    _paste_layer(canvas, cropped, box, adjustment, mode=_crop_aware_mode(adjustment, "cover"), clip_box=clip_box)
    return canvas


def _detail_showcase_page(source: Image.Image, adjustment: dict[str, Any] | None = None) -> Image.Image:
    """Match the 604-605 reference without changing the uploaded detail image."""
    canvas = Image.new("RGB", (750, 750), "white")
    draw = ImageDraw.Draw(canvas)
    title = "细节展示"
    title_font = _font(34, True)
    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((750 - (title_box[2] - title_box[0])) / 2, 70), title, font=title_font, fill="#c4c4c4")
    box = (52, 181, 695, 704)
    if _has_manual_layout_adjustment(adjustment):
        clip_box = _expanded_safe_box(box, canvas.size)
    elif _has_light_studio_border(source):
        clip_box = _expanded_safe_box(box, canvas.size)
    else:
        clip_box = None
    _paste_detail_layer(
        canvas,
        source,
        box,
        adjustment,
        clip_box=clip_box,
        auto_zoom=0.82,
        auto_offset_y=-0.11,
    )
    return canvas


def _multi_angle_page(
    image_ids: list[int],
    adjustments: list[dict[str, Any]] | None = None,
) -> Image.Image:
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
    adjustments = adjustments or []
    for index, (image_id, box) in enumerate(zip(image_ids[:4], boxes)):
        adjustment = adjustments[index] if index < len(adjustments) else None
        clip_box = _expanded_safe_box(box, canvas.size, padding_ratio=0.035) if _has_manual_layout_adjustment(adjustment) else None
        _paste_product(canvas, _load_image(image_id), box, adjustment, clip_box=clip_box)
    draw.line((346, 420, 404, 420), fill="#a8a8a8", width=2)
    draw.line((375, 391, 375, 449), fill="#a8a8a8", width=2)
    return canvas


def _save_png_30(image: Image.Image, path: Path) -> None:
    canvas = _normalized_product_page(image, transparent=True)
    canvas.save(path, optimize=True)
    size = path.stat().st_size
    if size < 100_000:
        canvas.save(path, compress_level=0)
    elif size > 600_000:
        canvas.quantize(colors=256).save(path, optimize=True)


@lru_cache(maxsize=8)
def _jd_logo_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [JD_LOGO_FONT_PATH, BUNDLED_FONT_PATH]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


@lru_cache(maxsize=1)
def _jd_elle_logo_layer() -> Image.Image:
    font = _jd_logo_font(160)
    text = "E L L E"
    bbox = font.getbbox(text)
    layer = Image.new("L", (bbox[2] - bbox[0] + 8, bbox[3] - bbox[1] + 8), 0)
    draw = ImageDraw.Draw(layer)
    draw.text((4 - bbox[0], 4 - bbox[1]), text, font=font, fill=255, stroke_width=0)
    glyph_bbox = layer.getbbox()
    if glyph_bbox:
        layer = layer.crop(glyph_bbox)
    return layer.resize((187, 60), Image.Resampling.LANCZOS)


def _draw_jd_elle_logo(
    canvas: Image.Image,
    size: tuple[int, int],
    color: str = "black",
) -> None:
    if size == (800, 800):
        position = (32, 38)
    elif size == (750, 1000):
        position = (56, 45)
    else:
        position = (
            int(round(32 * size[0] / 800)),
            int(round(38 * size[1] / 800)),
        )
    logo_mask = _jd_elle_logo_layer()
    ink = Image.new("RGB", logo_mask.size, "#ffffff" if color == "white" else "#111111")
    canvas.paste(ink, position, logo_mask)


def _jd_model_page(
    source: Image.Image,
    size: tuple[int, int],
    adjustment: dict[str, Any] | None,
    *,
    with_logo: bool,
    logo_color: str = "black",
) -> Image.Image:
    canvas = Image.new("RGB", size, "white")
    _paste_layer(
        canvas,
        _crop_source(source.convert("RGB"), adjustment),
        (0, 0, *size),
        adjustment,
        mode=_crop_aware_mode(adjustment, "cover"),
    )
    if with_logo:
        _draw_jd_elle_logo(canvas, size, logo_color)
    return canvas


def _jd_product_page(
    source: Image.Image,
    size: tuple[int, int],
    adjustment: dict[str, Any] | None,
    *,
    detail: bool = False,
    logo_color: str = "black",
) -> Image.Image:
    canvas = Image.new("RGB", size, "white")
    if detail:
        if not _has_manual_layout_adjustment(adjustment) and _has_light_studio_border(source):
            detail_box = (
                round(size[0] * 0.0875),
                round(size[1] * 0.145),
                round(size[0] * 0.9125),
                round(size[1] * 0.92),
            )
            _paste_detail_layer(canvas, source, detail_box, adjustment, auto_zoom=0.9, auto_offset_y=-0.055)
        else:
            _paste_detail_layer(canvas, source, (0, 0, *size), adjustment)
    else:
        if size == (800, 800):
            box = (100, 135, 700, 700)
        else:
            box = (100, 145, 650, 900)
        clip_box = _expanded_safe_box(box, size) if _has_manual_layout_adjustment(adjustment) else None
        _paste_product(canvas, source, box, adjustment, clip_box=clip_box)
    _draw_jd_elle_logo(canvas, size, logo_color)
    return canvas


def _mask_longest_run(values: np.ndarray) -> tuple[int, int] | None:
    indices = np.flatnonzero(values)
    if not len(indices):
        return None
    best_start = current_start = int(indices[0])
    best_end = current_end = int(indices[0])
    for raw_index in indices[1:]:
        index = int(raw_index)
        if index == current_end + 1:
            current_end = index
        else:
            if current_end - current_start > best_end - best_start:
                best_start, best_end = current_start, current_end
            current_start = current_end = index
    if current_end - current_start > best_end - best_start:
        best_start, best_end = current_start, current_end
    return best_start, best_end + 1


def _jd_product_body_bbox(cutout: Image.Image) -> tuple[int, int, int, int]:
    """Estimate the solid bag body while excluding sparse handles and chains."""
    alpha = np.asarray(cutout.getchannel("A"))
    mask = alpha > 28
    ys, xs = np.where(mask)
    if not len(xs):
        return 0, 0, cutout.width, cutout.height

    full_left = int(xs.min())
    full_top = int(ys.min())
    full_right = int(xs.max()) + 1
    full_bottom = int(ys.max()) + 1
    row_counts = mask.sum(axis=1)
    max_row_width = int(row_counts.max())
    broad_rows = row_counts >= max(10, int(round(max_row_width * 0.38)))
    row_run = _mask_longest_run(broad_rows)
    if row_run is None:
        return full_left, full_top, full_right, full_bottom

    body_top, body_bottom = row_run
    padding_y = max(1, int(round((body_bottom - body_top) * 0.04)))
    body_top = max(full_top, body_top - padding_y)
    body_bottom = min(full_bottom, body_bottom + padding_y)

    body_mask = mask[body_top:body_bottom]
    column_counts = body_mask.sum(axis=0)
    solid_columns = column_counts >= max(2, int(round((body_bottom - body_top) * 0.14)))
    column_run = _mask_longest_run(solid_columns)
    if column_run is None:
        body_xs = np.where(body_mask)[1]
        body_left = int(body_xs.min())
        body_right = int(body_xs.max()) + 1
    else:
        body_left, body_right = column_run

    padding_x = max(1, int(round((body_right - body_left) * 0.025)))
    body_left = max(full_left, body_left - padding_x)
    body_right = min(full_right, body_right + padding_x)
    if body_right - body_left < cutout.width * 0.18 or body_bottom - body_top < cutout.height * 0.12:
        return full_left, full_top, full_right, full_bottom
    return body_left, body_top, body_right, body_bottom


def _draw_jd_dimension_bar(
    canvas: Image.Image,
    start: tuple[int, int],
    end: tuple[int, int],
    label: str,
    *,
    vertical: bool = False,
    vertical_label_side: str = "left",
) -> None:
    draw = ImageDraw.Draw(canvas)
    color = "#777777"
    stroke = max(2, round(min(canvas.size) / 400))
    cap = max(8, round(min(canvas.size) * 0.014))
    font = _font(max(14, round(min(canvas.size) * 0.022)))
    draw.line((start, end), fill=color, width=stroke)
    if vertical:
        draw.line((start[0] - cap, start[1], start[0] + cap, start[1]), fill=color, width=stroke)
        draw.line((end[0] - cap, end[1], end[0] + cap, end[1]), fill=color, width=stroke)
        text_box = font.getbbox(label)
        text_height = text_box[3] - text_box[1]
        text_x = start[0] + cap + 9 if vertical_label_side == "right" else start[0] - cap - text_height - 16
        _draw_rotated_text(
            canvas,
            label,
            (text_x, round((start[1] + end[1]) / 2 - 30)),
            90,
            font,
        )
    else:
        draw.line((start[0], start[1] - cap, start[0], start[1] + cap), fill=color, width=stroke)
        draw.line((end[0], end[1] - cap, end[0], end[1] + cap), fill=color, width=stroke)
        text_box = draw.textbbox((0, 0), label, font=font)
        text_width = text_box[2] - text_box[0]
        draw.text(
            (round((start[0] + end[0] - text_width) / 2), start[1] + cap + 7),
            label,
            font=font,
            fill=color,
        )


@lru_cache(maxsize=1)
def _jd_phone_reference_layer() -> Image.Image | None:
    if not JD_PHONE_REFERENCE_PATH.is_file():
        return None
    with Image.open(JD_PHONE_REFERENCE_PATH) as source:
        return source.convert("RGBA")


def _draw_jd_phone_reference(
    canvas: Image.Image,
    center_x: int,
    top: int,
    height: int,
) -> tuple[int, int, int, int]:
    """Draw the supplied iPhone reference as one movable, scalable layer."""
    reference = _jd_phone_reference_layer()
    if reference is not None:
        phone_height = max(90, height)
        phone_width = max(42, round(phone_height * reference.width / reference.height))
        left = round(center_x - phone_width / 2)
        rendered = reference.resize((phone_width, phone_height), Image.Resampling.LANCZOS)
        canvas.paste(rendered, (left, top), rendered)
        return left, top, left + phone_width, top + phone_height

    draw = ImageDraw.Draw(canvas)
    phone_height = max(90, height)
    phone_width = max(42, round(phone_height * 0.48))
    overlap = max(12, round(phone_height * 0.13))
    pair_width = phone_width * 2 - overlap
    left = round(center_x - pair_width / 2)
    right = left + phone_width - overlap
    radius = max(8, round(phone_width * 0.13))
    outline = "#888888"

    draw.rounded_rectangle(
        (left, top, left + phone_width, top + phone_height),
        radius=radius,
        fill="#f2f2f0",
        outline=outline,
        width=2,
    )
    camera_panel = (
        left + round(phone_width * 0.08),
        top + round(phone_width * 0.08),
        left + round(phone_width * 0.66),
        top + round(phone_width * 0.66),
    )
    draw.rounded_rectangle(camera_panel, radius=max(5, radius // 2), fill="#dededc")
    camera_r = max(4, round(phone_width * 0.09))
    camera_centers = [
        (left + round(phone_width * 0.24), top + round(phone_width * 0.24)),
        (left + round(phone_width * 0.49), top + round(phone_width * 0.24)),
        (left + round(phone_width * 0.24), top + round(phone_width * 0.49)),
    ]
    for cx, cy in camera_centers:
        draw.ellipse((cx - camera_r, cy - camera_r, cx + camera_r, cy + camera_r), fill="#171717", outline="#777777")
        highlight = max(1, camera_r // 3)
        draw.ellipse((cx - highlight, cy - highlight, cx, cy), fill="#5f6872")
    apple_center = (left + phone_width // 2, top + round(phone_height * 0.55))
    apple_r = max(3, round(phone_width * 0.055))
    draw.ellipse(
        (apple_center[0] - apple_r, apple_center[1] - apple_r, apple_center[0] + apple_r, apple_center[1] + apple_r),
        fill="#ddddda",
    )
    draw.rounded_rectangle(
        (right, top, right + phone_width, top + phone_height),
        radius=radius,
        fill="#11171c",
        outline=outline,
        width=2,
    )
    screen_left = right + 3
    screen_top = top + 3
    screen_right = right + phone_width - 3
    screen_bottom = top + phone_height - 3
    for index in range(max(1, screen_bottom - screen_top)):
        ratio = index / max(1, screen_bottom - screen_top - 1)
        red = round(15 + 6 * ratio)
        green = round(24 + 20 * ratio)
        blue = round(31 + 28 * ratio)
        draw.line((screen_left, screen_top + index, screen_right, screen_top + index), fill=(red, green, blue))
    arc_width = max(1, round(phone_width * 0.025))
    draw.arc(
        (right - round(phone_width * 0.34), top + round(phone_height * 0.05), right + round(phone_width * 1.28), top + round(phone_height * 0.75)),
        15,
        145,
        fill="#8aa6ac",
        width=arc_width,
    )
    draw.arc(
        (right - round(phone_width * 0.1), top + round(phone_height * 0.4), right + round(phone_width * 1.2), top + round(phone_height * 1.05)),
        195,
        330,
        fill="#4b8e9b",
        width=arc_width,
    )
    island_width = round(phone_width * 0.34)
    draw.rounded_rectangle(
        (
            right + round((phone_width - island_width) / 2),
            top + max(5, round(phone_width * 0.08)),
            right + round((phone_width + island_width) / 2),
            top + max(9, round(phone_width * 0.16)),
        ),
        radius=4,
        fill="#050505",
    )
    return left, top, right + phone_width, top + phone_height


def _jd_size_comparison_page(
    source: Image.Image,
    size: tuple[int, int],
    product_info: dict[str, str],
    adjustment: dict[str, Any] | None,
    logo_color: str = "black",
) -> Image.Image:
    canvas = Image.new("RGB", size, "#f3f3f3")
    _draw_jd_elle_logo(canvas, size, logo_color)
    width, height = size
    normalized = _normalize_adjustment(adjustment)
    cutout = _product_cutout(_crop_source(source, adjustment))
    body_left, body_top, body_right, body_bottom = _jd_product_body_bbox(cutout)
    body_width = max(1, body_right - body_left)
    body_height = max(1, body_bottom - body_top)

    length_mm = _dimension_value_mm(product_info.get("product_length", "")) or 200.0
    height_mm = _dimension_value_mm(product_info.get("product_height", ""))
    if height_mm is None:
        height_mm = max(60.0, length_mm * body_height / body_width)

    portrait = height > width
    product_max_width = width * (0.39 if portrait else 0.37)
    product_max_height = height * (0.36 if portrait else 0.37)
    preferred_product_width = width * 0.35 * max(0.82, min(1.08, length_mm / 205.0))
    target_body_width = min(product_max_width, preferred_product_width)
    product_scale = min(target_body_width / body_width, product_max_height / body_height)
    if _has_manual_crop(adjustment):
        # A designer crop defines the complete visible region. Fit that region as
        # a whole so sparse handles or straps are not clipped by body-only sizing.
        product_scale = min(
            product_scale,
            width * 0.43 / max(1, cutout.width),
            height * 0.58 / max(1, cutout.height),
        )
    product_scale *= normalized["zoom"]
    resized_width = max(1, round(cutout.width * product_scale))
    resized_height = max(1, round(cutout.height * product_scale))
    cutout = cutout.resize((resized_width, resized_height), Image.Resampling.LANCZOS)
    scaled_body = (
        round(body_left * product_scale),
        round(body_top * product_scale),
        round(body_right * product_scale),
        round(body_bottom * product_scale),
    )

    desired_body_center_x = width * (0.34 if portrait else 0.34)
    desired_body_bottom = height * (0.70 if portrait else 0.73)
    desired_body_center_x += normalized["offset_x"] * width * 0.18
    desired_body_bottom += normalized["offset_y"] * height * 0.18
    paste_x = round(desired_body_center_x - (scaled_body[0] + scaled_body[2]) / 2)
    paste_y = round(desired_body_bottom - scaled_body[3])
    safe_left = round(width * 0.04)
    safe_top = round(height * 0.04)
    safe_right = round(width * 0.96)
    safe_bottom = round(height * 0.96)
    paste_x = min(max(safe_left, paste_x), max(safe_left, safe_right - resized_width))
    product_top = max(safe_top, round(height * 0.16))
    paste_y = min(max(product_top, paste_y), max(product_top, safe_bottom - resized_height - 35))
    canvas.paste(cutout, (paste_x, paste_y), cutout)

    rendered_body = (
        paste_x + scaled_body[0],
        paste_y + scaled_body[1],
        paste_x + scaled_body[2],
        paste_y + scaled_body[3],
    )
    draw = ImageDraw.Draw(canvas)
    rendered_body_width = max(1, rendered_body[2] - rendered_body[0])
    rendered_body_height = max(1, rendered_body[3] - rendered_body[1])
    rendered_pixels_per_mm = rendered_body_height / max(1.0, height_mm)
    phone_height = round(JD_PHONE_HEIGHT_MM * rendered_pixels_per_mm * normalized["phone_scale"])
    phone_height = max(round(height * 0.095), min(round(height * 0.46), phone_height))
    phone_center_x = width * 0.75 + normalized["phone_offset_x"] * width * 0.18
    if normalized["phone_alignment"] == "bottom":
        phone_top = rendered_body[3] - phone_height
    else:
        phone_top = round((rendered_body[1] + rendered_body[3] - phone_height) / 2)
    phone_top += round(normalized["phone_offset_y"] * height * 0.18)
    reference = _jd_phone_reference_layer()
    phone_width = max(
        42,
        round(phone_height * reference.width / reference.height) if reference is not None else round(phone_height * 0.83),
    )
    phone_left = round(phone_center_x - phone_width / 2)
    phone_ruler_gap = max(38, round(width * 0.075))
    phone_right_allowance = phone_ruler_gap + max(18, round(width * 0.025)) if normalized["phone_show_ruler"] else 0
    phone_left = min(max(safe_left, phone_left), max(safe_left, safe_right - phone_width - phone_right_allowance))
    phone_center_x = phone_left + phone_width / 2
    phone_bottom_allowance = 0 if not normalized["phone_show_ruler"] else max(28, round(height * 0.055))
    phone_top = min(max(safe_top, phone_top), max(safe_top, safe_bottom - phone_height - phone_bottom_allowance))
    phone_box = _draw_jd_phone_reference(
        canvas,
        round(phone_center_x),
        phone_top,
        phone_height,
    )

    ruler_gap = max(28, round(width * 0.045))
    if normalized["product_show_ruler"]:
        horizontal_y = min(height - 70, rendered_body[3] + ruler_gap)
        _draw_jd_dimension_bar(
            canvas,
            (rendered_body[0], horizontal_y),
            (rendered_body[2], horizontal_y),
            _dimension_mm(product_info.get("product_length", "")),
        )
        vertical_x = max(30, rendered_body[0] - ruler_gap)
        _draw_jd_dimension_bar(
            canvas,
            (vertical_x, rendered_body[1]),
            (vertical_x, rendered_body[3]),
            _dimension_mm(product_info.get("product_height", "")),
            vertical=True,
        )

    if normalized["phone_show_ruler"]:
        phone_ruler_x = min(safe_right - max(12, round(width * 0.02)), phone_box[2] + phone_ruler_gap)
        _draw_jd_dimension_bar(
            canvas,
            (phone_ruler_x, phone_box[1]),
            (phone_ruler_x, phone_box[3]),
            "163mm",
            vertical=True,
            vertical_label_side="right",
        )
        label_font = _font(max(13, round(min(size) * 0.02)))
        phone_label = JD_PHONE_LABEL
        label_box = draw.textbbox((0, 0), phone_label, font=label_font)
        draw.text(
            (round((phone_box[0] + phone_box[2] - (label_box[2] - label_box[0])) / 2), phone_box[3] + 12),
            phone_label,
            font=label_font,
            fill="#555555",
        )
    return canvas


def _render_jd_slot_image(
    file_name: str,
    image_ids: list[int],
    product_info: dict[str, str],
    adjustments: list[dict[str, Any]],
    target_folder: str = "800",
    logo_color: str = "black",
) -> Image.Image | None:
    if not image_ids:
        return None
    size = (800, 800) if target_folder == "800" else (750, 1000)
    source = _load_image(image_ids[0])
    adjustment = adjustments[0] if adjustments else None
    if file_name == "0-无logo.jpg":
        return _jd_model_page(source, size, adjustment, with_logo=False)
    if file_name == "1.jpg":
        return _jd_model_page(source, size, adjustment, with_logo=True, logo_color=logo_color)
    if file_name == "2.jpg":
        return _jd_product_page(source, size, adjustment, logo_color=logo_color)
    if file_name in {"3.jpg", "4.jpg"}:
        return _jd_product_page(source, size, adjustment, detail=True, logo_color=logo_color)
    if file_name == "5.jpg":
        return _jd_size_comparison_page(source, size, product_info, adjustment, logo_color)
    if file_name == "透明.png":
        return _normalized_product_page(source, transparent=True, adjustment=adjustment)
    return None


def _slot_map(slots: list[dict[str, Any]], platform: str = "vip") -> dict[str, dict[str, Any]]:
    slot_map = {
        item["file_name"]: {
            "image_ids": [int(value) for value in item.get("image_ids", [])],
            "adjustments": [
                _normalize_adjustment(value if isinstance(value, dict) else None)
                for value in item.get("adjustments", [])
            ],
            "logo_color": "white" if item.get("logo_color") == "white" else "black",
        }
        for item in slots
    }
    # Linked model layouts always use one source image.
    if platform == "jd" and "0-无logo.jpg" in slot_map:
        slot_map.setdefault("1.jpg", {"image_ids": [], "adjustments": [], "logo_color": "black"})
        slot_map["1.jpg"]["image_ids"] = list(slot_map["0-无logo.jpg"]["image_ids"])
    elif platform != "jd" and "1.jpg" in slot_map:
        slot_map.setdefault("50.jpg", {"image_ids": [], "adjustments": [], "logo_color": "black"})
        slot_map["50.jpg"]["image_ids"] = list(slot_map["1.jpg"]["image_ids"])
    return slot_map


def _validate_slot_map(session_id: str, slot_map: dict[str, dict[str, Any]], platform: str = "vip") -> None:
    model_slots = {"0-无logo.jpg", "1.jpg"} if platform == "jd" else {"1.jpg", "50.jpg", "601.jpg", "602.jpg", "603.jpg"}
    tag_name = None if platform == "jd" else "801.jpg"
    _validate_session_assets(session_id, {
        "model": [
            image_id
            for name in model_slots
            for image_id in slot_map.get(name, {}).get("image_ids", [])
        ],
        "tag": slot_map.get(tag_name, {}).get("image_ids", []) if tag_name else [],
        "product": [
            image_id
            for name, slot in slot_map.items()
            if name not in model_slots and name != tag_name
            for image_id in slot.get("image_ids", [])
        ],
    })


def _render_slot_image(
    file_name: str,
    image_ids: list[int],
    product_info: dict[str, str],
    adjustments: list[dict[str, Any]] | None = None,
    platform: str = "vip",
    target_folder: str = "800",
    logo_color: str = "black",
) -> Image.Image | None:
    adjustments = adjustments or []
    if platform == "jd":
        return _render_jd_slot_image(
            file_name,
            image_ids,
            product_info,
            adjustments,
            target_folder,
            logo_color,
        )
    adjustment = adjustments[0] if adjustments else None
    if file_name == "401.jpg":
        source = _load_image(image_ids[0]) if image_ids else None
        return _info_page(product_info, source, adjustment)
    if file_name == "606.jpg":
        return _multi_angle_page(image_ids, adjustments) if len(image_ids) >= 4 else None
    if not image_ids:
        return None

    source = _load_image(image_ids[0])
    if file_name == "1.jpg":
        canvas = Image.new("RGB", (800, 800), "white")
        _paste_layer(canvas, _crop_source(source.convert("RGB"), adjustment), (0, 0, 800, 800), adjustment, mode=_crop_aware_mode(adjustment, "cover"))
        return canvas
    if file_name == "30.png":
        return _normalized_product_page(source, transparent=True, adjustment=adjustment)
    if file_name == "50.jpg":
        canvas = Image.new("RGB", (950, 1200), "white")
        _paste_layer(canvas, _crop_source(source.convert("RGB"), adjustment), (0, 0, 950, 1200), adjustment, mode=_crop_aware_mode(adjustment, "cover"))
        return canvas
    if file_name == "4.jpg":
        canvas = Image.new("RGB", (800, 800), "white")
        _paste_detail_layer(
            canvas,
            source,
            (0, 0, 800, 800),
            adjustment,
            auto_zoom=0.82,
            auto_offset_y=-0.08,
            default_mode="contain",
        )
        return canvas
    if file_name == "15.jpg":
        canvas = Image.new("RGB", (800, 800), "white")
        _paste_layer(canvas, _crop_source(source.convert("RGB"), adjustment), (0, 0, 800, 800), adjustment)
        return canvas
    if file_name in {"601.jpg", "602.jpg", "603.jpg"}:
        return _model_showcase_page(source, adjustment)
    if file_name in {"604.jpg", "605.jpg"}:
        return _detail_showcase_page(source, adjustment)
    if file_name == "801.jpg":
        return _normalized_product_page(source, size=(750, 750), box=(90, 105, 660, 665), adjustment=adjustment)
    return _catalog_product_page(source, adjustment)


def _save_slot_image(image: Image.Image, file_name: str, output: Path) -> None:
    if file_name.endswith(".png"):
        image.save(output, optimize=True)
        size = output.stat().st_size
        if size < 100_000:
            image.save(output, compress_level=0)
        elif size > 600_000:
            image.quantize(colors=256).save(output, optimize=True)
        return
    if file_name in {"4.jpg", "15.jpg", "604.jpg", "605.jpg"}:
        quality = 100
    elif file_name in {"1.jpg", "50.jpg", "601.jpg", "602.jpg", "603.jpg", "801.jpg"}:
        quality = 94
    else:
        quality = 98
    image.convert("RGB").save(output, quality=quality, subsampling=0)


def _save_preview_image(image: Image.Image, file_name: str, output: Path) -> None:
    """Encode temporary previews quickly without changing layout or color."""
    if file_name.endswith(".png"):
        image.save(output, compress_level=1)
        return
    image.convert("RGB").save(output, quality=95, subsampling=0, optimize=False)


def _preview_lock(session_id: str) -> Lock:
    with _PREVIEW_LOCKS_GUARD:
        return _PREVIEW_LOCKS.setdefault(session_id, Lock())


def _preview_product_info(
    file_name: str,
    product_info: dict[str, str],
    platform: str,
) -> dict[str, str]:
    if (platform == "vip" and file_name == "401.jpg") or (platform == "jd" and file_name == "5.jpg"):
        return product_info
    return {}


def _preview_cache_id(
    file_name: str,
    slot: dict[str, Any],
    product_info: dict[str, str],
    platform: str,
    target_folder: str = "800",
) -> str:
    payload = {
        "version": PREVIEW_RENDER_VERSION,
        "platform": platform,
        "target_folder": target_folder,
        "file_name": file_name,
        "image_ids": slot["image_ids"],
        "adjustments": slot["adjustments"],
        "logo_color": slot["logo_color"],
        "product_info": _preview_product_info(file_name, product_info, platform),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _render_cached_slot_preview(
    session_id: str,
    file_name: str,
    slot: dict[str, Any],
    product_info: dict[str, str],
    platform: str,
    target_folder: str = "800",
) -> str | None:
    preview_id = _preview_cache_id(file_name, slot, product_info, platform, target_folder)
    folder = _session_result_dir(session_id) / "previews" / preview_id
    output = folder / file_name
    if output.is_file():
        os.utime(folder, None)
        return f"/api/vip-organizer/previews/{session_id}/{preview_id}/{file_name}"

    image = _render_slot_image(
        file_name,
        slot["image_ids"],
        product_info,
        slot["adjustments"],
        platform,
        target_folder=target_folder,
        logo_color=slot["logo_color"],
    )
    if image is None:
        return None

    folder.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}-{uuid.uuid4().hex[:8]}{output.suffix}")
    try:
        _save_preview_image(image, file_name, temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return f"/api/vip-organizer/previews/{session_id}/{preview_id}/{file_name}"


def _prune_preview_cache(session_id: str) -> None:
    preview_root = _session_result_dir(session_id) / "previews"
    if not preview_root.is_dir():
        return
    entries = sorted(
        (path for path in preview_root.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in entries[MAX_PREVIEW_CACHE_ENTRIES:]:
        shutil.rmtree(stale, ignore_errors=True)


def render_previews(
    session_id: str,
    slots: list[dict[str, Any]],
    product_info: dict[str, str],
    platform: str = "vip",
    target_folder: str = "800",
) -> dict[str, Any]:
    with _preview_lock(session_id):
        return _render_previews(session_id, slots, product_info, platform, target_folder)


def render_slot_preview(
    session_id: str,
    slots: list[dict[str, Any]],
    product_info: dict[str, str],
    file_name: str,
    platform: str = "vip",
    target_folder: str = "800",
) -> dict[str, str]:
    slot_definitions = _platform_slot_definitions(platform)
    valid_names = {name for name, _, _, _ in slot_definitions}
    if file_name not in valid_names:
        raise ValueError("输出位置不存在")
    # A slot preview writes to its own UUID folder, so it can render independently
    # without waiting for the slower full-set preview lock.
    slot_map = _slot_map(slots, platform)
    _validate_slot_map(session_id, slot_map, platform)
    slot = slot_map.get(file_name, {"image_ids": [], "adjustments": [], "logo_color": "black"})
    if platform == "jd" and target_folder not in {"800", "750"}:
        raise ValueError("京东预览目录必须是 800 或 750")
    preview_url = _render_cached_slot_preview(
        session_id,
        file_name,
        slot,
        product_info,
        platform,
        target_folder,
    )
    if preview_url is None:
        raise ValueError("当前输出位置缺少素材")
    _prune_preview_cache(session_id)
    return {
        "file_name": file_name,
        "preview_url": preview_url,
    }


def _render_previews(
    session_id: str,
    slots: list[dict[str, Any]],
    product_info: dict[str, str],
    platform: str = "vip",
    target_folder: str = "800",
) -> dict[str, Any]:
    if platform == "jd" and target_folder not in {"800", "750"}:
        raise ValueError("京东预览目录必须是 800 或 750")
    slot_definitions = _platform_slot_definitions(platform)
    slot_map = _slot_map(slots, platform)
    _validate_slot_map(session_id, slot_map, platform)
    previews: dict[str, str] = {}
    missing: list[str] = []

    for file_name, _, _, _ in slot_definitions:
        slot = slot_map.get(file_name, {"image_ids": [], "adjustments": [], "logo_color": "black"})
        preview_url = _render_cached_slot_preview(
            session_id,
            file_name,
            slot,
            product_info,
            platform,
            target_folder,
        )
        if preview_url is None:
            missing.append(file_name)
            continue
        previews[file_name] = preview_url

    _prune_preview_cache(session_id)
    return {"previews": previews, "missing": missing}


def export_package(
    session_id: str,
    slots: list[dict[str, Any]],
    product_info: dict[str, str],
    platform: str = "vip",
) -> dict[str, Any]:
    slot_definitions = _platform_slot_definitions(platform)
    slot_map = _slot_map(slots, platform)
    _validate_slot_map(session_id, slot_map, platform)
    export_id = uuid.uuid4().hex[:12]
    session_result_dir = _session_result_dir(session_id)
    folder = session_result_dir / export_id
    folder.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []

    if platform == "jd":
        output_folders = {"800": folder / "800", "750": folder / "750"}
        for output_folder in output_folders.values():
            output_folder.mkdir(parents=True, exist_ok=True)
        for file_name, _, _, _ in slot_definitions:
            targets = ["800"] if file_name in {"0-无logo.jpg", "透明.png"} else ["800", "750"]
            slot = slot_map.get(file_name, {"image_ids": [], "adjustments": [], "logo_color": "black"})
            for target in targets:
                image = _render_slot_image(
                    file_name,
                    slot["image_ids"],
                    product_info,
                    slot["adjustments"],
                    platform,
                    target,
                    slot["logo_color"],
                )
                if image is None:
                    missing.append(f"{target}/{file_name}")
                    continue
                _save_slot_image(image, file_name, output_folders[target] / file_name)
    else:
        for file_name, _, _, _ in slot_definitions:
            output = folder / file_name
            slot = slot_map.get(file_name, {"image_ids": [], "adjustments": [], "logo_color": "black"})
            image = _render_slot_image(file_name, slot["image_ids"], product_info, slot["adjustments"])
            if image is None:
                missing.append(file_name)
                continue
            _save_slot_image(image, file_name, output)

    platform_label = "京东" if platform == "jd" else "唯品会"
    zip_path = session_result_dir / f"{platform_label}套图_{export_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=path.relative_to(folder).as_posix())
    preview_paths = [path for path in sorted(folder.rglob("*")) if path.suffix.lower() in {".jpg", ".png"}]
    previews = []
    for path in preview_paths:
        relative = path.relative_to(folder)
        if len(relative.parts) == 2:
            previews.append(f"/api/vip-organizer/exports/{session_id}/{export_id}/files/{relative.parts[0]}/{relative.parts[1]}")
        else:
            previews.append(f"/api/vip-organizer/exports/{session_id}/{export_id}/files/{path.name}")
    return {
        "download_url": f"/api/vip-organizer/exports/{session_id}/{export_id}/download",
        "previews": previews,
        "generated_count": len(previews),
        "missing": missing,
    }


def _valid_output_file_name(file_name: str) -> bool:
    allowed = {
        name
        for definitions in (SLOT_DEFINITIONS, JD_SLOT_DEFINITIONS)
        for name, _, _, _ in definitions
    }
    return file_name in allowed


def export_file(session_id: str, export_id: str, file_name: str, folder_name: str | None = None) -> Path:
    if (
        not _valid_session_id(session_id)
        or not re.fullmatch(r"[0-9a-f]{12}", export_id)
        or not _valid_output_file_name(file_name)
        or (folder_name is not None and folder_name not in {"800", "750"})
    ):
        raise ValueError("导出文件不存在")
    path = _session_result_dir(session_id) / export_id
    if folder_name:
        path /= folder_name
    path /= file_name
    if not path.is_file():
        raise ValueError("导出文件不存在")
    return path


def preview_file(session_id: str, preview_id: str, file_name: str) -> Path:
    if not _valid_session_id(session_id) or not re.fullmatch(r"[0-9a-f]{12}", preview_id) or not _valid_output_file_name(file_name):
        raise ValueError("预览文件不存在")
    path = _session_result_dir(session_id) / "previews" / preview_id / file_name
    if not path.is_file():
        raise ValueError("预览文件不存在")
    return path


def export_zip(session_id: str, export_id: str) -> Path:
    if not _valid_session_id(session_id) or not re.fullmatch(r"[0-9a-f]{12}", export_id):
        raise ValueError("导出文件不存在")
    matches = list(_session_result_dir(session_id).glob(f"*套图_{export_id}.zip"))
    if len(matches) != 1 or not matches[0].is_file():
        raise ValueError("导出文件不存在")
    return matches[0]
