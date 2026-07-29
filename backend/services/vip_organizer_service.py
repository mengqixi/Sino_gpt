from __future__ import annotations

import mimetypes
import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
from PIL import Image, ImageDraw, ImageFont, ImageOps, PngImagePlugin

from ..config import ALLOWED_IMAGE_EXTENSIONS, DATA_DIR
from ..database import db_session, now_iso
from .api_config_service import TEXT_API_TYPE, get_config, get_default_config, mask_api_key, require_config_type
from .heavy_task_service import run_heavy_task
from .json_path_service import json_path_get


ORGANIZER_DATA_DIR = DATA_DIR / "vip_organizer"
ORGANIZER_UPLOAD_DIR = ORGANIZER_DATA_DIR / "uploads"
ORGANIZER_RESULT_DIR = ORGANIZER_DATA_DIR / "results"
UPLOAD_COPY_BUFFER_SIZE = 1024 * 1024
ORGANIZER_SESSION_TTL_HOURS = 24
BUNDLED_FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NotoSansSC-VF-GB2312.ttf"
JD_LOGO_FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "LibreBodoni-VariableFont_wght.ttf"
JD_PHONE_REFERENCE_PATH = Path(__file__).resolve().parents[1] / "assets" / "iphone_reference.png"
JD_LOGO_BLACK_PATH = Path(__file__).resolve().parents[1] / "assets" / "elle_logo_black.png"
JD_LOGO_WHITE_PATH = Path(__file__).resolve().parents[1] / "assets" / "elle_logo_white.png"
U2NETP_MODEL_PATH = Path(__file__).resolve().parents[1] / "assets" / "models" / "u2netp.onnx"
CUTOUT_WORKER_PATH = Path(__file__).resolve().with_name("cutout_model_worker.py")
_PREVIEW_LOCKS_GUARD = Lock()
_PREVIEW_LOCKS: dict[str, Lock] = {}
PREVIEW_RENDER_VERSION = 20
MAX_PREVIEW_CACHE_ENTRIES = 48
JD_PHONE_HEIGHT_MM = 163.0
JD_PHONE_LABEL = "iPhone 17 Pro Max"
JD_MEASURE_COLOR = "#707070"


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
INFO_PRODUCT_BOX = (294, 238, 687, 511)
INFO_LENGTH_LINE_Y = 528

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


def delete_asset(session_id: str, image_id: int) -> None:
    """Delete one uploaded organizer asset owned by the requested session."""
    if not _valid_session_id(session_id):
        raise ValueError("整理会话已失效，请重新开始")
    with db_session() as conn:
        row = conn.execute(
            """
            SELECT id, file_path
            FROM vip_organizer_assets
            WHERE id = ? AND session_id = ?
            """,
            (image_id, session_id),
        ).fetchone()
        if not row:
            raise ValueError("素材不存在或已被删除")
        conn.execute(
            "DELETE FROM vip_organizer_assets WHERE id = ? AND session_id = ?",
            (image_id, session_id),
        )
        conn.execute(
            "UPDATE vip_organizer_sessions SET updated_at = ? WHERE id = ?",
            (now_iso(), session_id),
        )

    organizer_root = ORGANIZER_DATA_DIR.resolve()
    path = Path(row["file_path"])
    try:
        if os.path.commonpath((str(path.resolve()), str(organizer_root))) == str(organizer_root):
            path.unlink(missing_ok=True)
            (path.parent / f"thumb_{row['id']}.jpg").unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


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


def prepare_product_cutout(session_id: str, file: UploadFile) -> dict[str, str]:
    """Create an optional transparent product image without adding it to the organizer inputs."""
    with db_session() as conn:
        exists = conn.execute("SELECT id FROM vip_organizer_sessions WHERE id = ?", (session_id,)).fetchone()
    if not exists:
        raise ValueError("整理会话已失效，请重新开始")
    original_name = file.filename or "front.jpg"
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("请上传 JPG、PNG 或 WebP 图片")

    prepared_id = uuid.uuid4().hex
    prepared_dir = _session_result_dir(session_id) / "prepared" / prepared_id
    prepared_dir.mkdir(parents=True, exist_ok=True)
    source_path = prepared_dir / f"source{suffix}"
    transparent_path = prepared_dir / "transparent.png"
    gray_path = prepared_dir / "gray-preview.png"
    try:
        with source_path.open("wb") as output:
            shutil.copyfileobj(file.file, output, length=UPLOAD_COPY_BUFFER_SIZE)
        with Image.open(source_path) as source_image:
            source_image.verify()
        run_heavy_task(
            "backend.services.cutout_pipeline_worker",
            {
                "source_path": str(source_path),
                "transparent_path": str(transparent_path),
                "gray_path": str(gray_path),
            },
            timeout=600,
        )
        if not transparent_path.is_file() or not gray_path.is_file():
            raise ValueError("抠图结果生成失败")
        source_path.unlink(missing_ok=True)
        with db_session() as conn:
            conn.execute(
                "UPDATE vip_organizer_sessions SET updated_at = ? WHERE id = ?",
                (now_iso(), session_id),
            )
    except Exception:
        shutil.rmtree(prepared_dir, ignore_errors=True)
        raise

    base_url = f"/api/vip-organizer/prepared/{session_id}/{prepared_id}"
    return {
        "prepared_id": prepared_id,
        "transparent_url": f"{base_url}/transparent",
        "gray_preview_url": f"{base_url}/gray",
        "download_url": f"{base_url}/download",
        "file_name": f"{Path(original_name).stem}-透明.png",
    }


def prepared_cutout_file(session_id: str, prepared_id: str, variant: str) -> Path:
    if not _valid_session_id(session_id) or not re.fullmatch(r"[0-9a-f]{32}", prepared_id):
        raise ValueError("抠图结果不存在")
    if variant not in {"transparent", "gray", "download"}:
        raise ValueError("抠图结果不存在")
    file_name = "gray-preview.png" if variant == "gray" else "transparent.png"
    path = _session_result_dir(session_id) / "prepared" / prepared_id / file_name
    if not path.is_file():
        raise ValueError("抠图结果不存在")
    return path


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
    # A front-facing phone bag can be much narrower than a regular handbag.
    # Do not reject an explicit front label based on the product aspect ratio.
    labeled_front_views = fixed_front_views or [item for item in full_views if effective_role(item) == "front"]
    front_view = max(
        labeled_front_views or full_views,
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
        "401.jpg": ([front_id], 98 if has_transparent else selection_confidence(front_view, role="front"), "优先使用透明正面图生成产品信息页，缺少时回退正面主图"),
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
            "5.jpg": ([front_id], 98 if has_transparent else selection_confidence(front_view, role="front"), "优先使用透明正面图生成尺寸与手机对比模板，缺少时回退正面主图"),
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


@lru_cache(maxsize=12)
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
        channel_spread = rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2).astype(np.int16)
        bright_neutral = (rgb.min(axis=2) >= 232) & (channel_spread <= 24)
        background_candidate = (distance <= 34) | bright_neutral
        candidate_count, candidate_labels = cv2.connectedComponents(background_candidate.astype(np.uint8), 8)
        border_labels = np.unique(np.concatenate((
            candidate_labels[0, :],
            candidate_labels[-1, :],
            candidate_labels[:, 0],
            candidate_labels[:, -1],
        )))
        border_labels = border_labels[border_labels > 0]
        connected_background = np.isin(candidate_labels, border_labels) if candidate_count > 1 else background_candidate
        raw_mask = (~connected_background).astype(np.uint8)
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


def _predict_product_matte(source: Image.Image) -> np.ndarray | None:
    if not U2NETP_MODEL_PATH.exists() or not CUTOUT_WORKER_PATH.exists():
        return None
    configured_python = os.environ.get("SINO_CUTOUT_PYTHON", "").strip()
    worker_python = Path(configured_python) if configured_python else Path(sys.executable)
    if not worker_python.exists():
        worker_python = Path(sys.executable)
    with tempfile.TemporaryDirectory(prefix="sino-cutout-") as directory:
        input_path = Path(directory) / "input.png"
        output_path = Path(directory) / "matte.png"
        source.convert("RGB").save(input_path, format="PNG", optimize=False)
        environment = os.environ.copy()
        environment.update({
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        })
        try:
            subprocess.run(
                [
                    str(worker_python),
                    str(CUTOUT_WORKER_PATH),
                    str(U2NETP_MODEL_PATH),
                    str(input_path),
                    str(output_path),
                ],
                check=True,
                timeout=45,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
            )
            with Image.open(output_path) as matte_image:
                return np.asarray(matte_image.convert("L"), dtype=np.float32) / 255.0
        except (OSError, subprocess.SubprocessError, ValueError):
            return None


def _clean_bottom_against_body_contour(
    alpha: np.ndarray,
    rgb: np.ndarray,
    model_matte: np.ndarray,
    protected_detail: np.ndarray,
    *,
    confidence_floor: float = 0.92,
    contour_mode: str = "smooth",
) -> np.ndarray:
    """Clear floor residue below a fitted, high-confidence bag-body edge.

    The caller supplies an already refined alpha.  This pass is deliberately
    one-way and local: it may clear pixels below the main body's lower contour,
    but it never changes a pixel above that contour.  Handles, openings and
    upper-body rules therefore remain byte-for-byte unchanged.  Hardware and
    chain pixels protected by the regional detector are excluded as well.

    ``contour_mode`` exists so the three inexpensive fits can be compared on
    real white-studio samples.  Production uses the selected default only.
    """
    if (
        alpha.shape != model_matte.shape
        or alpha.shape != protected_detail.shape
        or rgb.shape[:2] != alpha.shape
    ):
        return alpha
    height, width = alpha.shape
    if height < 40 or width < 40:
        return alpha

    yy, xx = np.indices(alpha.shape)
    central_columns = (xx >= round(width * 0.08)) & (xx < round(width * 0.92))
    high_confidence = (
        (model_matte >= confidence_floor)
        & (alpha > 8)
        & central_columns
        & ~protected_detail
    )
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        high_confidence.astype(np.uint8),
        8,
    )
    if component_count <= 1:
        return alpha
    largest_component = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    structure = labels == largest_component

    # Only the broad lower component can define the body edge.  Thin handles
    # and hanging side chains cannot accumulate enough width in these rows.
    scan_start = round(height * 0.52)
    row_widths = np.count_nonzero(structure & central_columns, axis=1)
    lower_widths = row_widths[scan_start:]
    widest_lower_row = int(np.max(lower_widths)) if lower_widths.size else 0
    minimum_body_width = max(20, round(width * 0.18))
    if widest_lower_row < minimum_body_width:
        return alpha
    broad_rows = np.flatnonzero(
        lower_widths >= max(minimum_body_width, round(widest_lower_row * 0.34))
    )
    if not broad_rows.size:
        return alpha
    body_scan_top = scan_start + int(broad_rows[0])
    body_scan_bottom = scan_start + int(broad_rows[-1])

    raw_contour = np.full(width, np.nan, dtype=np.float32)
    for column in range(round(width * 0.08), round(width * 0.92)):
        rows = np.flatnonzero(
            structure[body_scan_top:body_scan_bottom + 1, column]
        )
        if rows.size:
            raw_contour[column] = body_scan_top + float(rows[-1])
    supported_columns = np.flatnonzero(np.isfinite(raw_contour))
    if supported_columns.size < max(24, round(width * 0.24)):
        return alpha
    body_left = int(supported_columns.min())
    body_right = int(supported_columns.max())
    body_columns = np.arange(body_left, body_right + 1)
    interpolated = np.interp(
        body_columns,
        supported_columns,
        raw_contour[supported_columns],
    ).astype(np.float32)

    # Bag piping normally continues from both side edges around the bottom in
    # the same colour.  Use those trusted side-edge colours to extend the raw
    # contour through a darker/lighter real trim that a high confidence
    # threshold may miss.  Low-model shadow cannot extend it.  This only moves
    # the deletion boundary; it never paints or synthesizes edge pixels.
    structure_inside = cv2.distanceTransform(
        structure.astype(np.uint8),
        cv2.DIST_L2,
        3,
    )
    structure_boundary = structure & (structure_inside <= 3.0)
    side_band = max(3, round((body_right - body_left + 1) * 0.10))
    side_sample_bottom = body_scan_top + round(
        (body_scan_bottom - body_scan_top) * 0.76
    )
    edge_hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    edge_gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edge_gradient = cv2.magnitude(
        cv2.Sobel(edge_gray, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(edge_gray, cv2.CV_32F, 0, 1, ksize=3),
    )
    bright_gold_metal = (
        (edge_hsv[:, :, 0] >= 5)
        & (edge_hsv[:, :, 0] <= 40)
        & (edge_hsv[:, :, 1] >= 25)
        & (edge_hsv[:, :, 2] >= 155)
        & (edge_gradient >= 30)
        & (
            rgb[:, :, 0].astype(np.int16)
            >= rgb[:, :, 2].astype(np.int16) + 10
        )
    )
    neutral_metal_highlight = (
        (edge_hsv[:, :, 1] <= 42)
        & (edge_hsv[:, :, 2] >= 105)
        & (edge_hsv[:, :, 2] <= 245)
        & (edge_gradient >= 38)
        & (model_matte >= 0.78)
    )
    side_metal = bright_gold_metal | neutral_metal_highlight
    side_edge_zone = (
        structure_boundary
        & (yy >= body_scan_top)
        & (yy <= side_sample_bottom)
        & (
            (xx <= body_left + side_band)
            | (xx >= body_right - side_band)
        )
        & ~protected_detail
        & ~side_metal
    )
    edge_lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    side_colours = edge_lab[side_edge_zone]
    if side_colours.size:
        if len(side_colours) > 128:
            sample_indices = np.linspace(
                0,
                len(side_colours) - 1,
                128,
                dtype=np.int32,
            )
            side_colours = side_colours[sample_indices]
        maximum_extension = max(8, round(height * 0.12))
        upward_probe = max(6, round(height * 0.025))
        body_value_sample = edge_hsv[
            body_scan_top:side_sample_bottom + 1,
            body_left:body_right + 1,
            2,
        ]
        body_value = float(np.median(body_value_sample))
        for offset, column in enumerate(body_columns):
            initial_bottom = int(round(interpolated[offset]))
            extension_top = max(body_scan_top, initial_bottom - upward_probe)
            extension_bottom = min(
                height - 1,
                initial_bottom + maximum_extension,
            )
            candidate_rows = np.arange(
                extension_top,
                extension_bottom + 1,
            )
            candidate_colours = edge_lab[candidate_rows, column]
            edge_distance = np.min(
                np.linalg.norm(
                    candidate_colours[:, None, :] - side_colours[None, :, :],
                    axis=2,
                ),
                axis=1,
            )
            # Establish colour continuity while still safely inside the bag.
            # The model's last high-confidence row often includes the first
            # black contact-shadow pixel, so it must not be trusted directly.
            seed_bottom = min(
                initial_bottom,
                extension_top + max(3, upward_probe // 2),
            )
            seed_rows = np.arange(extension_top, seed_bottom + 1)
            seed_valid = (
                (model_matte[seed_rows, column] >= 0.72)
                & (alpha[seed_rows, column] > 8)
                & ~protected_detail[seed_rows, column]
            )
            if not np.any(seed_valid):
                continue
            rolling_colours = [
                colour.copy()
                for colour in edge_lab[seed_rows[seed_valid], column]
            ]
            rolling_values = [
                float(value_item)
                for value_item in edge_hsv[seed_rows[seed_valid], column, 2]
            ]
            last_trusted = int(seed_rows[seed_valid][-1])
            gap = 0
            for row_index, row in enumerate(candidate_rows):
                if row <= last_trusted:
                    continue
                local_reference = np.median(
                    np.asarray(rolling_colours[-5:]),
                    axis=0,
                )
                local_distance = float(np.linalg.norm(
                    edge_lab[row, column] - local_reference
                ))
                locally_non_shadow = (
                    edge_hsv[row, column, 2]
                    >= max(58.0, body_value * 0.58)
                )
                rolling_value = float(np.median(rolling_values[-5:]))
                abrupt_dark_drop = (
                    float(edge_hsv[row, column, 2])
                    < max(42.0, rolling_value * 0.57)
                )
                continuous_edge = (
                    model_matte[row, column] >= 0.72
                    and alpha[row, column] > 8
                    and not protected_detail[row, column]
                    and not abrupt_dark_drop
                    and (
                        edge_distance[row_index] <= 23.0
                        or (
                            local_distance <= 16.0
                            and locally_non_shadow
                        )
                    )
                )
                if continuous_edge:
                    last_trusted = int(row)
                    rolling_colours.append(edge_lab[row, column].copy())
                    rolling_values.append(float(edge_hsv[row, column, 2]))
                    gap = 0
                else:
                    gap += 1
                    if gap >= 2:
                        break
            interpolated[offset] = float(last_trusted)

    # The observations above only locate the real edge; they are not pixels
    # to preserve independently. A manufactured bag bottom is one coherent
    # edge, so fit either a horizontal baseline or a low-curvature quadratic.
    sample_count = len(interpolated)
    window = max(7, round(sample_count * 0.045) | 1)
    padded = np.pad(interpolated, window // 2, mode="edge")
    observations = np.median(
        np.lib.stride_tricks.sliding_window_view(padded, window),
        axis=1,
    ).astype(np.float32)
    normalized_x = np.linspace(-1.0, 1.0, sample_count)
    interior = np.abs(normalized_x) <= 0.84
    fit_x = normalized_x[interior]
    fit_y = observations[interior]
    for _ in range(3):
        coefficients = np.polyfit(fit_x, fit_y, 2)
        residual = fit_y - np.polyval(coefficients, fit_x)
        median_residual = float(np.median(residual))
        mad = float(np.median(np.abs(residual - median_residual)))
        keep = np.abs(residual - median_residual) <= max(2.0, 3.5 * mad)
        if np.count_nonzero(keep) < max(20, round(sample_count * 0.35)):
            break
        fit_x = fit_x[keep]
        fit_y = fit_y[keep]

    spread = float(np.quantile(fit_y, 0.90) - np.quantile(fit_y, 0.10))
    flat_limit = max(3.0, height * 0.009)
    if contour_mode == "horizontal" or spread <= flat_limit:
        level = float(np.quantile(fit_y, 0.58))
        fitted = np.full(sample_count, level, dtype=np.float32)
    else:
        coefficients = np.polyfit(fit_x, fit_y, 2)
        fitted = np.polyval(coefficients, normalized_x).astype(np.float32)
        lower = float(np.quantile(fit_y, 0.03) - max(2.0, spread * 0.30))
        upper = float(np.quantile(fit_y, 0.97) + max(2.0, spread * 0.30))
        fitted = np.clip(fitted, lower, upper)

    safety_margin = 0.0

    fitted_contour = np.full(width, height, dtype=np.float32)
    fitted_contour[body_left:body_right + 1] = fitted
    safe_contour = fitted_contour + safety_margin
    below_body = yy > safe_contour[None, :]
    removable = (
        below_body
        & (alpha > 0)
        & ~protected_detail
    )

    # Contact shadow can touch the last real row and therefore sit inside the
    # small antialias safety margin.  On non-dark products it is still a sharp
    # value/chroma departure from the broad bag body.  Clear only low-model
    # dark or grey contact pixels at/under the fitted edge; high-confidence
    # coloured trim remains untouched.  This is deletion only—no generated
    # edge colour or model alpha is written back.
    if not np.any(removable):
        return alpha
    cleaned = alpha.copy()
    cleaned[removable] = 0
    return cleaned


def _prepared_product_cutout(
    source: Image.Image,
    model_matte: np.ndarray | None = None,
    *,
    _bottom_contour_mode: str = "adaptive",
    _bottom_contour_confidence: float = 0.96,
) -> Image.Image:
    """Create a cleaner export cutout for white/light studio product photos.

    This stricter path is intentionally limited to the optional preparation
    tool. Existing organizer templates keep their established cutout behavior.
    """
    image = ImageOps.exif_transpose(source).convert("RGBA")
    if max(image.size) > 1600:
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    rgba = np.asarray(image).copy()
    rgb = rgba[:, :, :3]
    source_alpha = rgba[:, :, 3]
    height, width = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue = hsv[:, :, 0]
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    dark_product = False
    colourful_product = False
    pale_product = False
    elle_hardware_protection = np.zeros((height, width), dtype=bool)
    silver_hardware_protection = np.zeros((height, width), dtype=bool)
    strict_silver_hardware = np.zeros((height, width), dtype=bool)
    strict_hardware_protection = np.zeros((height, width), dtype=bool)
    hardware_protection = np.zeros((height, width), dtype=bool)
    pale_body_protection = np.zeros((height, width), dtype=bool)
    pale_opening_background = np.zeros((height, width), dtype=bool)

    if float(np.mean(source_alpha < 250)) > 0.01:
        mask = source_alpha > 12
        background_color = np.array([255.0, 255.0, 255.0], dtype=np.float32)
    else:
        edge = max(3, min(height, width) // 45)
        border = np.vstack([
            rgb[:edge, :].reshape(-1, 3),
            rgb[-edge:, :].reshape(-1, 3),
            rgb[:, :edge].reshape(-1, 3),
            rgb[:, -edge:].reshape(-1, 3),
        ])
        background_color = np.median(border, axis=0).astype(np.float32)
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        background_lab = cv2.cvtColor(
            np.uint8([[np.clip(background_color, 0, 255)]]),
            cv2.COLOR_RGB2LAB,
        )[0, 0].astype(np.float32)
        lab_distance = np.linalg.norm(lab - background_lab, axis=2)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        gradient_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gradient_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        gradient = cv2.magnitude(gradient_x, gradient_y)
        if model_matte is not None:
            model_matte = cv2.resize(
                model_matte,
                (width, height),
                interpolation=cv2.INTER_CUBIC,
            )
            model_matte = np.clip(model_matte, 0.0, 1.0)
            model_subject = model_matte >= 0.82
            model_subject_saturation = saturation[model_subject]
            model_subject_value = value[model_subject]
            model_subject_distance = lab_distance[model_subject]
            pale_product = bool(
                model_subject_saturation.size
                and model_subject_value.size
                and model_subject_distance.size
                and float(np.median(model_subject_saturation)) <= 30
                and float(np.median(model_subject_value)) >= 170
                and float(np.median(model_subject_distance)) <= 55
            )
            colourful_product = bool(
                model_subject_saturation.size
                and not pale_product
                and (
                    float(np.median(model_subject_saturation)) >= 15
                    or float(np.median(model_subject_distance)) >= 28
                )
            )

        # Flood only light/neutral pixels connected to an outside edge. This
        # removes the studio backdrop without opening holes inside the bag.
        background_candidate = (
            (lab_distance <= 20)
            | ((lab_distance <= 48) & (saturation <= 30) & (value >= 185))
        ).astype(np.uint8)
        _, background_labels = cv2.connectedComponents(background_candidate, 8)
        border_labels = np.unique(np.concatenate((
            background_labels[0, :],
            background_labels[-1, :],
            background_labels[:, 0],
            background_labels[:, -1],
        )))
        border_labels = border_labels[border_labels > 0]
        connected_background = np.isin(background_labels, border_labels)
        # Input to this preparation tool is guaranteed to be a white studio
        # image. Lock the near-white area that is already connected to the
        # canvas edge before consulting the segmentation model. The model is
        # useful for the product silhouette, but it must never pull white gaps
        # around chains, zips or fittings back into the foreground.
        locked_white_background = connected_background & (
            (
                (lab_distance <= 13)
                & (saturation <= 20)
                & (value >= 232)
            )
            | (
                (lab_distance <= 7)
                & (saturation <= 28)
                & (value >= 220)
            )
        )
        if pale_product and model_matte is not None:
            # A pale leather body can be close enough to studio white that the
            # outside flood reaches through its broad, low-texture panels.  Use
            # the model only inside the bag-body run; the handle/opening stays
            # on the stricter white-background path so enclosed white gaps are
            # still removed.
            model_geometry = model_matte >= 0.82
            model_row_widths = np.count_nonzero(model_geometry, axis=1)
            widest_model_row = int(np.max(model_row_widths))
            broad_model_rows = np.flatnonzero(
                model_row_widths >= max(18, round(widest_model_row * 0.42))
            )
            if broad_model_rows.size:
                row_runs = np.split(
                    broad_model_rows,
                    np.flatnonzero(np.diff(broad_model_rows) > 1) + 1,
                )
                body_run = max(row_runs, key=len)
                body_top = int(body_run[0])
                body_bottom = int(body_run[-1])
                row_grid = np.arange(height)[:, None]
                pale_body_protection = (
                    (model_matte >= 0.95)
                    & (row_grid >= body_top)
                    & (row_grid <= body_bottom)
                )
                body_rows, body_columns = np.where(model_geometry[body_run])
                if body_columns.size:
                    body_left = int(body_columns.min())
                    body_right = int(body_columns.max())
                    body_span_width = max(1, body_right - body_left + 1)
                    body_span_height = max(1, body_bottom - body_top + 1)
                    column_grid = np.arange(width)[None, :]
                    opening_zone = (
                        (row_grid >= body_top - round(body_span_height * 0.10))
                        & (row_grid <= body_top + round(body_span_height * 0.20))
                        & (column_grid >= body_left + round(body_span_width * 0.22))
                        & (column_grid <= body_right - round(body_span_width * 0.22))
                    )
                    source_min_full = rgb.min(axis=2)
                    source_spread_full = (
                        rgb.max(axis=2).astype(np.int16)
                        - source_min_full.astype(np.int16)
                    )
                    pale_opening_background = (
                        opening_zone
                        & (source_min_full >= 245)
                        & (source_spread_full <= 12)
                    )
                    pale_body_protection &= ~pale_opening_background
                locked_white_background &= ~pale_body_protection
                locked_white_background |= pale_opening_background
        gold_seed = np.zeros((height, width), dtype=bool)
        strict_gold_hardware = np.zeros((height, width), dtype=bool)
        initial_foreground = (
            (~connected_background) | pale_body_protection
        ).astype(np.uint8)

        # Detect ELLE's champagne-gold hardware before any foreground cleanup.
        # Every protected component must contain a clearly gold seed; muted
        # floor shadows cannot become protected merely because they are warm.
        # The one-pixel highlight recovery keeps bright metal edges without
        # filling the white holes between links, letters or clasps.
        if colourful_product:
            red = rgb[:, :, 0].astype(np.int16)
            green = rgb[:, :, 1].astype(np.int16)
            blue = rgb[:, :, 2].astype(np.int16)
            gold_seed = (
                (hue >= 8)
                & (hue <= 38)
                & (saturation >= 30)
                & (value >= 110)
                & (lab_distance >= 7)
                & (red >= blue + 10)
                & (green >= blue + 5)
                & (gradient >= 8)
            )
            gold_region_candidate = (
                (hue >= 7)
                & (hue <= 42)
                & (saturation >= 10)
                & (value >= 58)
                & (lab_distance >= 3)
                & (red >= blue + 5)
                & (green >= blue + 2)
                & ((gradient >= 4) | (saturation >= 18))
            )
            seeded_gold_neighbourhood = cv2.dilate(
                gold_seed.astype(np.uint8),
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
                iterations=1,
            ).astype(bool)
            gold_component_count, gold_component_labels = cv2.connectedComponents(
                gold_region_candidate.astype(np.uint8),
                8,
            )
            seeded_component_region = np.zeros((height, width), dtype=bool)
            if gold_component_count > 1:
                seeded_gold_labels = np.unique(gold_component_labels[gold_seed])
                seeded_gold_labels = seeded_gold_labels[seeded_gold_labels > 0]
                if seeded_gold_labels.size:
                    seeded_component_region = np.isin(
                        gold_component_labels,
                        seeded_gold_labels,
                    )
            upper_hardware_region = (
                seeded_component_region
                & (np.arange(height)[:, None] < round(height * 0.72))
            )
            elle_hardware_protection = (
                gold_seed
                | (seeded_gold_neighbourhood & gold_region_candidate)
                | upper_hardware_region
            )
            metal_highlight_neighbour = (
                cv2.dilate(
                    elle_hardware_protection.astype(np.uint8),
                    cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
                    iterations=1,
                ).astype(bool)
                & (lab_distance >= 2)
                & (value <= 252)
                & (gradient >= 14)
            )
            elle_hardware_protection |= metal_highlight_neighbour
            strict_gold_hardware = (
                gold_seed
                | (
                    seeded_gold_neighbourhood
                    & gold_region_candidate
                    & (gradient >= 10)
                )
            )

        # White-background ELLE photos also use silver chains and fittings.
        # Protect only high-contrast neutral metal immediately beside the
        # connected studio background. This avoids treating broad neutral
        # shadows or pale bag material as hardware, while retaining the thin
        # dark rim and bright highlight of individual silver links.
        if model_matte is not None:
            exterior_neighbour = cv2.dilate(
                connected_background.astype(np.uint8),
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
                iterations=1,
            ).astype(bool)
            local_value_range = (
                cv2.dilate(value, np.ones((3, 3), dtype=np.uint8))
                - cv2.erode(value, np.ones((3, 3), dtype=np.uint8))
            )
            silver_seed = (
                exterior_neighbour
                & (model_matte >= 0.55)
                & (saturation <= 40)
                & (value >= 45)
                & (value <= 225)
                & (lab_distance >= 7)
                & (gradient >= 26)
                & (local_value_range >= 20)
            )
            strict_silver_hardware = silver_seed
            silver_neighbourhood = cv2.dilate(
                silver_seed.astype(np.uint8),
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
                iterations=1,
            ).astype(bool)
            silver_hardware_protection = (
                silver_seed
                | (
                    silver_neighbourhood
                    & exterior_neighbour
                    & (model_matte >= 0.40)
                    & (saturation <= 48)
                    & (value <= 242)
                    & (lab_distance >= 5)
                    & (gradient >= 14)
                )
            )

        # The broad hardware mask helps GrabCut retain an entire thin fitting,
        # while the strict mask contains only pixels supported by a real metal
        # seed.  Final white-fringe removal must use the strict version: a
        # dilated metal neighbourhood otherwise protects the white studio
        # pixels beside a clasp or chain link and creates a visible halo.
        strict_hardware_protection = (
            strict_gold_hardware | strict_silver_hardware
        )
        strict_hardware_protection &= ~locked_white_background

        hardware_protection = (
            elle_hardware_protection | silver_hardware_protection
        )
        hardware_protection &= ~locked_white_background

        # Refine uncertain light edges with GrabCut. Strongly coloured and dark
        # pixels are protected so chains, hardware and pale leather survive.
        grabcut = np.full((height, width), cv2.GC_PR_BGD, dtype=np.uint8)
        probable_foreground = initial_foreground > 0
        if model_matte is not None:
            probable_foreground |= model_matte >= 0.48
        probable_foreground &= ~locked_white_background
        grabcut[probable_foreground] = cv2.GC_PR_FGD
        sure_foreground = probable_foreground & (
            (lab_distance >= 66)
            | (saturation >= 48)
            | (value <= 145)
        )
        sure_foreground |= hardware_protection
        if model_matte is not None:
            sure_foreground |= model_matte >= 0.985
        grabcut[sure_foreground] = cv2.GC_FGD
        reliable_connected_background = connected_background & (
            (lab_distance <= 12)
            | ((saturation <= 12) & (value >= 247))
        )
        exact_studio_background = (
            (lab_distance <= 5)
            & (saturation <= 8)
            & (value >= 248)
        )
        grabcut[
            (
                reliable_connected_background
                | exact_studio_background
                | locked_white_background
            )
            & ~pale_body_protection
        ] = cv2.GC_BGD
        try:
            cv2.grabCut(
                cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                grabcut,
                None,
                np.zeros((1, 65), np.float64),
                np.zeros((1, 65), np.float64),
                3,
                cv2.GC_INIT_WITH_MASK,
            )
            mask = np.isin(grabcut, (cv2.GC_FGD, cv2.GC_PR_FGD))
        except cv2.error:
            mask = probable_foreground

        if model_matte is not None:
            textured_model_foreground = (
                (model_matte >= 0.72)
                & ((gradient >= 8) | (lab_distance >= 7) | (saturation >= 10))
            )
            textured_model_foreground |= pale_body_protection
            mask |= pale_body_protection
            mask[connected_background & ~textured_model_foreground] = False
            mask[exact_studio_background & ~pale_body_protection] = False
            mask[locked_white_background] = False

        # GrabCut can shave off pale product edges that are close to a white
        # backdrop. Protect narrow, high-gradient parts already found by the
        # established organizer cutout while leaving broad white holes clear.
        legacy = _product_cutout(image)
        legacy_alpha = np.asarray(legacy.getchannel("A"))
        if legacy_alpha.size:
            legacy_full = np.zeros((height, width), dtype=np.uint8)
            legacy_rgb = np.asarray(legacy.convert("RGB"))
            match = cv2.matchTemplate(rgb, legacy_rgb, cv2.TM_SQDIFF_NORMED)
            legacy_left, legacy_top = cv2.minMaxLoc(match)[2]
            legacy_bottom = min(height, legacy_top + legacy_alpha.shape[0])
            legacy_right = min(width, legacy_left + legacy_alpha.shape[1])
            legacy_full[legacy_top:legacy_bottom, legacy_left:legacy_right] = legacy_alpha[
                :legacy_bottom - legacy_top,
                :legacy_right - legacy_left,
            ]
            distance_from_reliable_foreground = cv2.distanceTransform(
                (~mask).astype(np.uint8),
                cv2.DIST_L2,
                3,
            )
            edge_protection = (
                (legacy_full > 80)
                & (distance_from_reliable_foreground <= max(4, min(height, width) * 0.007))
                & ((gradient >= 10) | (lab_distance >= 7) | (value <= 247))
                & ~locked_white_background
            )
            mask |= edge_protection

        # ELLE plaques, letter logos, charms, links, zips and clasps have the
        # highest preservation priority throughout all later shadow cleanup.
        mask |= hardware_protection
        mask[locked_white_background] = False

        mask_u8 = mask.astype(np.uint8)
        mask_u8 = cv2.morphologyEx(
            mask_u8,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        )
        # Closing is useful for tiny breaks in straps and chain links, but it
        # must not paint already-confirmed studio white back into those gaps.
        mask_u8[locked_white_background] = 0

        # A studio floor shadow is normally a sparse neutral tail below the
        # final dense product row. Remove that tail while keeping coloured
        # straps and metal pieces which may extend below the bag body.
        structural_foreground = mask_u8.astype(bool) & (
            (saturation >= 35)
            | (value <= 185)
        )
        structural_rows = structural_foreground.sum(axis=1)
        broad_rows = structural_rows >= max(10, int(structural_rows.max() * 0.55))
        broad_labels_count, broad_labels = cv2.connectedComponents(
            broad_rows.astype(np.uint8)[:, None],
            8,
        )
        broad_runs = [
            np.flatnonzero(broad_labels[:, 0] == label)
            for label in range(1, broad_labels_count)
        ]
        broad_runs = [run for run in broad_runs if run.size >= 3]
        product_bottom_rows = max(broad_runs, key=lambda run: int(run[-1]), default=np.array([], dtype=int))
        if product_bottom_rows.size:
            sample_top = height * 2 // 5
            sample_bottom = height * 4 // 5
            sample_left = width // 4
            sample_right = width * 3 // 4
            sample_mask = mask_u8[
                sample_top:sample_bottom,
                sample_left:sample_right,
            ].astype(bool)
            sample_value = value[
                sample_top:sample_bottom,
                sample_left:sample_right,
            ][sample_mask]
            dark_product = bool(
                sample_value.size
                and float(np.median(sample_value)) <= 120
            )
            dense_bottom = int(product_bottom_rows[-1])
            central_material = mask_u8.astype(bool) & (
                (saturation >= 24)
                | (dark_product & (value <= 130))
                | ((lab_distance >= 16) & (gradient >= 4))
            )
            material_column_bottoms = []
            for column in range(width // 5, width * 4 // 5):
                material_rows = np.flatnonzero(central_material[:, column])
                if material_rows.size:
                    material_column_bottoms.append(int(material_rows[-1]))
            if len(material_column_bottoms) >= max(12, width // 10):
                material_bottom = int(np.percentile(material_column_bottoms, 60))
                dense_bottom = min(
                    dense_bottom,
                    material_bottom + max(2, height // 350),
                )
            if model_matte is not None:
                model_column_bottoms = []
                for column in range(width // 4, width * 3 // 4):
                    model_rows = np.flatnonzero(model_matte[:, column] >= 0.95)
                    if model_rows.size:
                        model_column_bottoms.append(int(model_rows[-1]))
                if model_column_bottoms:
                    dense_bottom = min(
                        dense_bottom,
                        int(np.percentile(model_column_bottoms, 90)),
                    )
            yy = np.arange(height)[:, None]
            nearby_rows = mask_u8[
                max(0, dense_bottom - max(8, height // 80)):dense_bottom + 1
            ]
            nearby_xs = np.flatnonzero(nearby_rows.any(axis=0))
            body_left = int(nearby_xs.min()) if nearby_xs.size else width // 4
            body_right = int(nearby_xs.max()) if nearby_xs.size else width * 3 // 4
            xx = np.arange(width)[None, :]
            inside_body_width = (xx >= body_left) & (xx <= body_right)
            gold_detail = (
                (gradient >= 18)
                & (saturation >= 25)
                & (hue >= 5)
                & (hue <= 40)
                & (value >= 80)
                & (rgb[:, :, 0].astype(np.int16) >= rgb[:, :, 2].astype(np.int16) + 7)
                & (rgb[:, :, 1].astype(np.int16) >= rgb[:, :, 2].astype(np.int16) + 10)
            )
            dark_detail = dark_product & (gradient >= 18) & (value <= 125)
            outside_colored_detail = (
                ~inside_body_width
                & (gradient >= 16)
                & (saturation >= 28)
            )
            central_colored_detail = (
                inside_body_width
                & mask_u8.astype(bool)
                & (saturation >= 28)
                & (lab_distance >= 14)
            )
            model_detail = np.zeros((height, width), dtype=bool)
            if model_matte is not None:
                model_detail = (
                    (model_matte >= 0.82)
                    & (gradient >= 18)
                    & (saturation >= 18)
                    & (value >= 140)
                )
            detail_below_body = (
                gold_detail
                | hardware_protection
                | dark_detail
                | outside_colored_detail
                | central_colored_detail
                | model_detail
            )
            shadow_tail = (yy > dense_bottom) & ~detail_below_body
            mask_u8[shadow_tail] = 0

            remaining_rows = np.flatnonzero(mask_u8.any(axis=1))
            if remaining_rows.size:
                floor_start = int(remaining_rows[-1]) - max(10, height // 18)
                connected_floor_shadow = (
                    (yy >= floor_start)
                    & connected_background
                    & (saturation <= 30)
                    & (value >= 115)
                    & (gradient <= 95)
                    & ~(
                        gold_detail
                        | hardware_protection
                        | dark_detail
                        | outside_colored_detail
                        | central_colored_detail
                    )
                )
                mask_u8[connected_floor_shadow] = 0

            # Shadows form broad, shallow islands under the product. Remove
            # those islands while retaining narrow chain/hardware components.
            tail = mask_u8.copy()
            tail[:dense_bottom + 1, :] = 0
            component_count, component_labels, component_stats, _ = cv2.connectedComponentsWithStats(tail, 8)
            for component in range(1, component_count):
                component_width = int(component_stats[component, cv2.CC_STAT_WIDTH])
                component_height = int(component_stats[component, cv2.CC_STAT_HEIGHT])
                component_area = int(component_stats[component, cv2.CC_STAT_AREA])
                component_pixels = component_labels == component
                protected_detail_area = int(np.count_nonzero(
                    detail_below_body[component_pixels]
                ))
                if (
                    component_width >= max(12, width // 16)
                    and component_height <= max(18, height // 45)
                    and component_area >= max(30, width // 8)
                    and protected_detail_area < max(6, round(component_area * 0.08))
                ):
                    mask_u8[component_pixels] = 0

        mask = mask_u8 > 0

    ys, xs = np.where(mask)
    if not len(xs):
        return _product_cutout(image)
    object_width = int(xs.max() - xs.min() + 1)
    object_height = int(ys.max() - ys.min() + 1)
    padding = max(12, int(max(object_width, object_height) * 0.012))
    left = max(0, int(xs.min()) - padding)
    top = max(0, int(ys.min()) - padding)
    right = min(width, int(xs.max()) + padding + 1)
    bottom = min(height, int(ys.max()) + padding + 1)

    cropped_mask = mask[top:bottom, left:right].astype(np.uint8)
    cropped_hue = hue[top:bottom, left:right]
    cropped_saturation = saturation[top:bottom, left:right]
    cropped_value = value[top:bottom, left:right]
    inside = cv2.distanceTransform(cropped_mask, cv2.DIST_L2, 3)
    outside = cv2.distanceTransform(1 - cropped_mask, cv2.DIST_L2, 3)
    alpha = np.clip((inside - outside) * 100.0 + 128.0, 0, 255).astype(np.uint8)
    alpha = cv2.GaussianBlur(alpha, (0, 0), 0.45)

    cropped_rgb = rgb[top:bottom, left:right].astype(np.float32)
    if float(np.mean(source_alpha < 250)) <= 0.01:
        cropped_lab_distance = lab_distance[top:bottom, left:right]

    # A few dark floor pixels can become detached only after edge decontamination.
    # Remove small lower islands after the final alpha refinement, while keeping
    # the connected product and gold hardware regardless of image occupancy.
    cropped_rgb_u8 = rgb[top:bottom, left:right]
    cropped_gradient = gradient[top:bottom, left:right]
    cropped_gold_detail = (
        (cropped_gradient >= 18)
        & (cropped_saturation >= 25)
        & (cropped_hue >= 5)
        & (cropped_hue <= 40)
        & (cropped_value >= 80)
        & (cropped_rgb_u8[:, :, 0].astype(np.int16) >= cropped_rgb_u8[:, :, 2].astype(np.int16) + 7)
        & (cropped_rgb_u8[:, :, 1].astype(np.int16) >= cropped_rgb_u8[:, :, 2].astype(np.int16) + 10)
    )
    cropped_gold_only_protection = cv2.dilate(
        cropped_gold_detail.astype(np.uint8),
        np.ones((3, 3), dtype=np.uint8),
        iterations=2,
    ).astype(bool)
    cropped_elle_hardware = elle_hardware_protection[top:bottom, left:right]
    cropped_silver_hardware = silver_hardware_protection[top:bottom, left:right]
    cropped_strict_silver_hardware = strict_silver_hardware[
        top:bottom,
        left:right,
    ]
    cropped_strict_hardware = strict_hardware_protection[
        top:bottom,
        left:right,
    ]
    cropped_locked_white_background = locked_white_background[
        top:bottom,
        left:right,
    ]
    cropped_pale_body_protection = pale_body_protection[
        top:bottom,
        left:right,
    ]
    cropped_hardware = cropped_elle_hardware | cropped_silver_hardware
    cropped_gold_protection = cropped_gold_only_protection | cropped_hardware
    cropped_tight_hardware = cropped_strict_hardware.copy()
    if model_matte is not None and not dark_product:
        cropped_model_matte = model_matte[top:bottom, left:right]
        tight_gold_core = (
            cropped_gold_detail
            & (cropped_model_matte >= 0.72)
        )
        tight_silver_core = (
            cropped_strict_silver_hardware
            & (cropped_model_matte >= 0.78)
            & (cropped_gradient >= 22)
        )
        tight_elle_core = (
            cropped_elle_hardware
            & (cropped_model_matte >= 0.72)
            & (
                (cropped_saturation >= 22)
                | (cropped_gradient >= 24)
            )
        )
        metal_core_seed = tight_gold_core | tight_silver_core | tight_elle_core
        cropped_tight_hardware = metal_core_seed | (
            cv2.dilate(
                metal_core_seed.astype(np.uint8),
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
                iterations=1,
            ).astype(bool)
            & (cropped_model_matte >= 0.72)
            & (cropped_lab_distance >= 8)
        )
        cropped_colored_material = (
            (cropped_saturation >= 28)
            & (cropped_lab_distance >= 14)
            & (cropped_model_matte >= 0.82)
            & ~cropped_locked_white_background
        )
        verified_floor_hardware = cropped_gold_only_protection & (
            (cropped_model_matte >= 0.80)
            | (
                (cropped_value >= 170)
                & (cropped_gradient >= 40)
                & (
                    cropped_rgb_u8[:, :, 1].astype(np.int16)
                    >= cropped_rgb_u8[:, :, 2].astype(np.int16) + 15
                )
            )
        )
        # ELLE gold plaques and charms retain the existing unconditional
        # protection. Neutral silver links are stricter near the floor so a
        # thin grey cast shadow cannot masquerade as metal.
        verified_floor_hardware |= cropped_elle_hardware
        verified_floor_hardware |= (
            cropped_silver_hardware
            & (cropped_model_matte >= 0.78)
            & (cropped_saturation <= 48)
            & (cropped_value >= 65)
            & (cropped_value <= 245)
            & (cropped_gradient >= 22)
        )
        cropped_height, cropped_width = alpha.shape
        yy, xx = np.indices(alpha.shape)
        material_sample = (
            (yy >= cropped_height * 2 // 5)
            & (yy < cropped_height * 4 // 5)
            & (xx >= cropped_width // 4)
            & (xx < cropped_width * 3 // 4)
            & (alpha >= 200)
        )
        material_saturation = cropped_saturation[material_sample]
        strongly_coloured_product = bool(
            material_saturation.size
            and float(np.median(material_saturation)) >= 30
        )
        refined_value = cv2.cvtColor(
            np.clip(cropped_rgb, 0, 255).astype(np.uint8),
            cv2.COLOR_RGB2HSV,
        )[:, :, 2]
        # Preparation photos are supplied on a white studio background. Clear
        # genuinely white pixels directly instead of asking the segmentation
        # model to distinguish every tiny gap around chains and zip hardware.
        # The supplied pale bag is ivory rather than pure white, so this narrow
        # RGB rule preserves its material while removing white pockets.
        source_min = cropped_rgb_u8.min(axis=2)
        source_spread = (
            cropped_rgb_u8.max(axis=2).astype(np.int16)
            - source_min.astype(np.int16)
        )
        white_background = (
            (alpha > 0)
            & (source_min >= 235)
            & (source_spread <= 18)
            & ~cropped_pale_body_protection
            & ~cropped_tight_hardware
        )
        alpha[white_background] = 0

        # Different parts of a handbag need different cleanup rules.  The
        # handle/opening and the side-chain lanes often contain disconnected
        # white pockets which cannot be reached by an outside flood.  The body
        # itself is not subjected to this broader rule, and pale/white bags
        # remain on the conservative model path.
        geometry_structure = (
            (cropped_model_matte >= 0.82)
            & (alpha > 0)
            & ~cropped_locked_white_background
        )
        structure_rows, structure_columns = np.where(geometry_structure)
        if structure_rows.size and structure_columns.size and not pale_product:
            object_top = int(structure_rows.min())
            object_bottom = int(structure_rows.max())
            object_left = int(structure_columns.min())
            object_right = int(structure_columns.max())
            object_span_height = max(1, object_bottom - object_top + 1)
            object_span_width = max(1, object_right - object_left + 1)
            row_widths = np.count_nonzero(geometry_structure, axis=1)
            widest_row = int(np.max(row_widths))
            body_rows = np.flatnonzero(
                row_widths >= max(18, round(widest_row * 0.42))
            )
            body_top = (
                int(body_rows[0])
                if body_rows.size
                else object_top + round(object_span_height * 0.35)
            )
            body_bottom = (
                int(body_rows[-1])
                if body_rows.size
                else object_bottom
            )

            inside_distance = cv2.distanceTransform(
                (alpha > 0).astype(np.uint8),
                cv2.DIST_L2,
                3,
            )
            boundary_zone = (alpha > 0) & (inside_distance <= 14.0)
            handle_zone = (
                (yy <= body_top + round(object_span_height * 0.04))
                & (xx >= object_left)
                & (xx <= object_right)
            )
            opening_zone = (
                (yy >= body_top - round(object_span_height * 0.10))
                & (yy <= body_top + round(object_span_height * 0.14))
                & (xx >= object_left)
                & (xx <= object_right)
            )
            side_lane = max(8, round(object_span_width * 0.26))
            chain_zone = (
                (yy >= object_top)
                & (yy <= object_bottom)
                & (
                    (xx <= object_left + side_lane)
                    | (xx >= object_right - side_lane)
                )
            )
            regional_cleanup_zone = (
                boundary_zone | handle_zone | opening_zone | chain_zone
            )
            regional_white = (
                (
                    (cropped_lab_distance <= 24)
                    & (cropped_saturation <= 24)
                    & (cropped_value >= 224)
                )
                | (
                    (source_min >= 232)
                    & (source_spread <= 18)
                )
            )
            alpha[
                regional_cleanup_zone
                & regional_white
                & ~cropped_tight_hardware
            ] = 0

        # Remove only the floor residue below the model's last broad,
        # high-confidence product row. A single horizontal cutoff preserves
        # the bag silhouette; unlike per-column trimming it cannot reshape a
        # curved base. The outer columns and all verified hardware remain
        # untouched so low-hanging chains and fittings are never clipped.
        central_model_columns = (
            (xx >= round(cropped_width * 0.08))
            & (xx < round(cropped_width * 0.92))
        )
        confident_structure = (
            (cropped_model_matte >= 0.95)
            & (alpha > 0)
            & central_model_columns
            & ~cropped_hardware
        )
        model_floor: int | None = None
        minimum_structure_width = max(12, round(cropped_width * 0.06))
        for row in range(round(cropped_height * 0.68), cropped_height):
            if int(np.count_nonzero(confident_structure[row])) >= minimum_structure_width:
                model_floor = row
        if model_floor is not None and model_floor < cropped_height - 2:
            low_confidence_floor_residue = (
                (yy > model_floor)
                & central_model_columns
                & (alpha > 0)
                & (cropped_model_matte < 0.95)
                & ~cropped_hardware
                & ~cropped_colored_material
            )
            alpha[low_confidence_floor_residue] = 0

        # The segmentation model can assign very high confidence to a soft
        # contact shadow, especially below ivory leather and woven straw. Find
        # the product's last broad, opaque row instead: a real bag bottom stays
        # horizontally continuous, while its shadow immediately collapses to
        # a few thin fragments. Use one global floor and never trim individual
        # columns, so curved and rectangular silhouettes keep their shape.
        central_opaque_width = np.count_nonzero(
            (alpha >= 200) & central_model_columns,
            axis=1,
        )
        floor_scan_start = round(cropped_height * 0.60)
        floor_scan_end = cropped_height
        scan_widths = central_opaque_width[floor_scan_start:floor_scan_end]
        broad_width = int(np.max(scan_widths)) if scan_widths.size else 0
        broad_floor: int | None = None
        if broad_width >= max(24, round(cropped_width * 0.12)):
            minimum_broad_width = max(18, round(broad_width * 0.42))
            minimum_collapse = max(10, round(broad_width * 0.26))
            for index in range(max(0, scan_widths.size - 1)):
                current_width = int(scan_widths[index])
                next_width = int(scan_widths[index + 1])
                if (
                    current_width >= minimum_broad_width
                    and current_width - next_width >= minimum_collapse
                    and next_width <= round(current_width * 0.48)
                ):
                    broad_floor = floor_scan_start + index
            if broad_floor is None:
                broad_threshold = max(12, round(broad_width * 0.72))
                broad_rows = np.flatnonzero(scan_widths >= broad_threshold)
                if broad_rows.size:
                    broad_floor = floor_scan_start + int(broad_rows[-1])

        if broad_floor is not None and broad_floor < cropped_height - 1:
            floor_hardware_core = cv2.dilate(
                cropped_tight_hardware.astype(np.uint8),
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
                iterations=1,
            ).astype(bool)
            collapsed_floor_residue = (
                (yy > broad_floor)
                & central_model_columns
                & (alpha > 0)
                & ~floor_hardware_core
                & ~cropped_colored_material
            )
            alpha[collapsed_floor_residue] = 0

        if strongly_coloured_product:
            # Find the lowest broad row that still has the bag material's
            # normal brightness.  A cast shadow drops sharply below that row,
            # while the real curved/rectangular bottom remains above it.  Use
            # one global floor line (rather than trimming each column) so the
            # bag silhouette is never reshaped; gold chains and fittings are
            # explicitly retained below the line.
            median_material_value = (
                float(np.median(cropped_value[material_sample]))
                if np.any(material_sample)
                else 180.0
            )
            median_material_saturation = (
                float(np.median(cropped_saturation[material_sample]))
                if np.any(material_sample)
                else 40.0
            )
            material_value_floor = float(np.clip(
                median_material_value * 0.78,
                90,
                165,
            ))
            central_columns = (
                (xx >= round(cropped_width * 0.10))
                & (xx < round(cropped_width * 0.90))
            )
            material_floor: int | None = None
            dim_row_run = 0
            for row in range(round(cropped_height * 0.70), cropped_height):
                row_material = (
                    central_columns[row]
                    & (alpha[row] >= 200)
                    & ~cropped_gold_protection[row]
                    & (
                        cropped_saturation[row]
                        >= max(10.0, median_material_saturation * 0.34)
                    )
                )
                row_values = cropped_value[row][row_material]
                reliable_row = (
                    row_values.size >= max(12, round(cropped_width * 0.08))
                )
                if reliable_row and float(np.median(row_values)) >= material_value_floor:
                    if dim_row_run >= 2:
                        break
                    material_floor = row
                    dim_row_run = 0
                elif material_floor is not None:
                    dim_row_run += 1
                    if dim_row_run >= 2:
                        break
            if material_floor is not None:
                shadow_below_floor = (
                    (yy > material_floor)
                    & central_columns
                    & (alpha > 0)
                    & ~verified_floor_hardware
                    & ~cropped_colored_material
                )
                alpha[shadow_below_floor] = 0

                # Pale coloured bags can cast a darker contact shadow which
                # remains connected to the bottom seam. Remove only that
                # sudden value drop at/under the global floor. Hardware uses a
                # fresh, tight gold-neighbour mask here so an earlier broad
                # protection cannot accidentally preserve the shadow.
                if median_material_value >= 185:
                    authentic_floor_gold = (
                        (cropped_hue >= 8)
                        & (cropped_hue <= 38)
                        & (cropped_saturation >= 25)
                        & (cropped_value >= 110)
                    )
                    tight_floor_hardware = cv2.dilate(
                        authentic_floor_gold.astype(np.uint8),
                        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
                        iterations=1,
                    ).astype(bool)
                    connected_contact_shadow = (
                        (yy >= material_floor)
                        & central_columns
                        & (alpha > 0)
                        & (refined_value < material_value_floor + 12)
                        & ~tight_floor_hardware
                        & ~cropped_colored_material
                    )
                    alpha[connected_contact_shadow] = 0

        transparent_neighbour = cv2.dilate(
            (alpha == 0).astype(np.uint8),
            np.ones((3, 3), dtype=np.uint8),
            iterations=1,
        ).astype(bool)

        # Shadows are handled separately: flood only low-confidence pixels at
        # the central lower edge. The outer fifths are excluded because chains
        # and metal accessories commonly hang below the bag body there.
        floor_candidate = (
            (yy >= round(cropped_height * 0.90))
            & (xx >= round(cropped_width * 0.10))
            & (xx < round(cropped_width * 0.90))
            & (alpha > 0)
            & (
                (cropped_model_matte < 0.72)
                | (strongly_coloured_product & (cropped_saturation <= 32))
                | (strongly_coloured_product & (refined_value <= 115))
            )
            & ~verified_floor_hardware
            & ~cropped_colored_material
        )
        floor_component_count, floor_labels = cv2.connectedComponents(
            floor_candidate.astype(np.uint8),
            8,
        )
        for component in range(1, floor_component_count):
            component_pixels = floor_labels == component
            if np.any(component_pixels & transparent_neighbour):
                alpha[component_pixels] = 0

        # Preserve the first-version coloured lower edge. It uses model
        # confidence only as a soft alpha cap and does not invent a new body
        # contour, which avoids cutting real piping on varied bag shapes.
        if strongly_coloured_product:
            coloured_floor_band = (
                (yy >= round(cropped_height * 0.88))
                & (cropped_saturation >= 28)
                & (cropped_lab_distance >= 14)
                & (alpha > 0)
                & ~verified_floor_hardware
            )
            confidence_alpha = np.round(
                np.clip(
                    (cropped_model_matte - 0.80) / 0.10,
                    0.0,
                    1.0,
                ) * 255.0
            ).astype(np.uint8)
            alpha[coloured_floor_band] = np.minimum(
                alpha[coloured_floor_band],
                confidence_alpha[coloured_floor_band],
            )

    component_count, component_labels, component_stats, _ = cv2.connectedComponentsWithStats(
        (alpha > 8).astype(np.uint8),
        8,
    )
    if component_count > 2:
        largest_component = 1 + int(np.argmax(component_stats[1:, cv2.CC_STAT_AREA]))
        small_component_limit = max(24, cropped_mask.shape[1] // 10)
        lower_quarter = cropped_mask.shape[0] * 3 // 4
        for component in range(1, component_count):
            if component == largest_component:
                continue
            component_area = int(component_stats[component, cv2.CC_STAT_AREA])
            component_left = int(component_stats[component, cv2.CC_STAT_LEFT])
            component_top = int(component_stats[component, cv2.CC_STAT_TOP])
            component_width = int(component_stats[component, cv2.CC_STAT_WIDTH])
            component_height = int(component_stats[component, cv2.CC_STAT_HEIGHT])
            component_pixels = component_labels == component
            component_center_x = component_left + component_width / 2
            component_value = float(np.mean(
                np.max(cropped_rgb[component_pixels], axis=1)
            ))
            component_gold_fraction = float(np.mean(
                cropped_gold_protection[component_pixels]
            ))
            component_authentic_gold_fraction = float(np.mean(
                (
                    (cropped_hue >= 8)
                    & (cropped_hue <= 38)
                    & (cropped_saturation >= 25)
                    & (cropped_value >= 110)
                )[component_pixels]
            ))
            component_colored_fraction = float(np.mean(
                cropped_colored_material[component_pixels]
            ))
            central_dark_floor = (
                not dark_product
                and component_top >= cropped_mask.shape[0] * 0.90
                and cropped_mask.shape[1] * 0.20 <= component_center_x <= cropped_mask.shape[1] * 0.80
                and component_value <= 115
            )
            central_shallow_floor = (
                not dark_product
                and component_top >= cropped_mask.shape[0] * 0.90
                and component_height <= max(8, cropped_mask.shape[0] // 55)
                and cropped_mask.shape[1] * 0.20 <= component_center_x <= cropped_mask.shape[1] * 0.80
                and component_authentic_gold_fraction < 0.35
                and component_colored_fraction < 0.20
            )
            if (
                central_shallow_floor
                or (
                    component_area <= small_component_limit
                    and component_top >= lower_quarter
                    and (
                        central_dark_floor
                        or component_gold_fraction <= 0.15
                    )
                )
            ):
                alpha[component_pixels] = 0

    if model_matte is not None and not dark_product and strongly_coloured_product:
        contour_hardware_seed = (
            (
                cropped_gold_detail
                & (cropped_model_matte >= 0.78)
                & (cropped_value >= 110)
            )
            | (
                cropped_strict_silver_hardware
                & (cropped_model_matte >= 0.78)
                & (cropped_value >= 70)
                & (cropped_gradient >= 22)
            )
        )
        contour_hardware = cv2.dilate(
            contour_hardware_seed.astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            iterations=1,
        ).astype(bool) & (cropped_model_matte >= 0.72)

        # Chain hardware often has leather or fabric threaded through the
        # metal links. Protect that narrow, connected material together with
        # the confirmed metal core. Expansion is geodesic and colour-gated:
        # it can follow a thin blue/black strap but cannot spread into a broad
        # neutral floor shadow or across the bag body.
        chain_material = (
            (alpha > 8)
            & (cropped_model_matte >= 0.72)
            & (cropped_lab_distance >= 12)
            & (
                (cropped_saturation >= 26)
                | (cropped_value <= 175)
            )
        )
        chain_linked_detail = contour_hardware.copy()
        chain_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        for _ in range(6):
            expanded_detail = cv2.dilate(
                chain_linked_detail.astype(np.uint8),
                chain_kernel,
                iterations=1,
            ).astype(bool)
            next_detail = chain_linked_detail | (expanded_detail & chain_material)
            if np.array_equal(next_detail, chain_linked_detail):
                break
            chain_linked_detail = next_detail
        contour_hardware = chain_linked_detail
        contour_central_lane = (
            (xx >= round(cropped_width * 0.08))
            & (xx < round(cropped_width * 0.92))
        )
        contour_hardware_widths = np.count_nonzero(
            contour_hardware & contour_central_lane,
            axis=1,
        )
        overbroad_hardware_rows = (
            (np.arange(cropped_height) >= round(cropped_height * 0.72))
            & (contour_hardware_widths >= round(cropped_width * 0.50))
        )
        if np.any(overbroad_hardware_rows):
            contour_hardware[
                overbroad_hardware_rows[:, None] & contour_central_lane
            ] = False
        # Keep the contour/hardware analysis available for the final bottom
        # residue pass, but do not reshape the body here. The historical
        # global-floor algorithm is materially better for curved and square
        # coloured bags than a second per-body contour cut.

    if float(np.mean(source_alpha < 250)) <= 0.01:
        # Later anti-aliasing and floor-shadow passes may give a faint alpha
        # back to pixels already confirmed as the connected white backdrop.
        # White-background removal is the first, authoritative stage: keep
        # those pixels transparent before the established edge algorithm runs.
        cropped_locked_white_background = locked_white_background[
            top:bottom,
            left:right,
        ]
        alpha[cropped_locked_white_background] = 0

        # Do this last, after the established silhouette and floor-shadow
        # rules. JPEG compression and antialiasing mix several rings of studio
        # white into handles, zips, chains and fittings. Merely lowering their
        # alpha leaves a visible white outline on a grey/dark preview.
        #
        # Estimate the real foreground colour from the nearest source pixel
        # that is safely different from the white backdrop, then solve
        #   observed = alpha * foreground + (1-alpha) * background
        # for alpha. This is deliberately limited to the outer edge band and
        # can only lower alpha. Ivory/white products have too little colour
        # separation to satisfy the denominator test, so their structure is
        # left to the model and the established cutout logic.
        final_foreground = alpha > 8
        final_inside = cv2.distanceTransform(
            final_foreground.astype(np.uint8),
            cv2.DIST_L2,
            3,
        )
        final_boundary = final_foreground & (final_inside <= 18.0)
        neutral_white_fringe = (
            (cropped_lab_distance <= 46)
            & (cropped_saturation <= 22)
            & (cropped_value >= 214)
        )
        colour_seed = (
            (alpha >= 200)
            & (cropped_lab_distance >= 14)
            & ~neutral_white_fringe
            & (
                (cropped_saturation >= 12)
                | (cropped_value <= 236)
            )
        )
        if np.any(colour_seed) and np.any(final_boundary):
            _, nearest_labels = cv2.distanceTransformWithLabels(
                (~colour_seed).astype(np.uint8),
                cv2.DIST_L2,
                5,
                labelType=cv2.DIST_LABEL_PIXEL,
            )
            seed_colours = cropped_rgb[colour_seed]
            nearest_indices = np.clip(
                nearest_labels.astype(np.int64) - 1,
                0,
                len(seed_colours) - 1,
            )
            nearest_colours = seed_colours[nearest_indices]

            backdrop = background_color[None, None, :]
            denominator = backdrop - nearest_colours
            numerator = backdrop - cropped_rgb
            valid_channels = denominator >= 8.0
            channel_alpha = np.divide(
                numerator,
                np.maximum(denominator, 1.0),
                out=np.ones_like(cropped_rgb, dtype=np.float32),
                where=valid_channels,
            )
            channel_alpha = np.clip(channel_alpha, 0.0, 1.0)
            informative_channels = np.count_nonzero(valid_channels, axis=2)
            sorted_channel_alpha = np.sort(
                np.where(valid_channels, channel_alpha, np.nan),
                axis=2,
            )
            estimated_alpha = np.nanmedian(sorted_channel_alpha, axis=2)
            estimated_alpha = np.nan_to_num(estimated_alpha, nan=1.0)
            # Specular gold/silver highlights can have only one useful colour
            # channel. Keep the conservative maximum only on hardware already
            # verified before background removal; use the median elsewhere so
            # one contaminated channel cannot preserve a white halo.
            hardware_channel_alpha = np.max(
                np.where(valid_channels, channel_alpha, 0.0),
                axis=2,
            )
            estimated_alpha[cropped_tight_hardware] = hardware_channel_alpha[
                cropped_tight_hardware
            ]
            observed_background_distance = np.linalg.norm(
                cropped_rgb - backdrop,
                axis=2,
            )
            seed_background_distance = np.linalg.norm(
                nearest_colours - backdrop,
                axis=2,
            )
            white_mixed_edge = (
                final_boundary
                & (informative_channels >= 2)
                & (seed_background_distance >= 14.0)
                & ~cropped_tight_hardware
                & (
                    (observed_background_distance <= seed_background_distance * 0.985)
                    | (cropped_lab_distance <= 34)
                )
            )
            old_alpha = alpha.astype(np.float32) / 255.0
            solved_alpha = np.clip(estimated_alpha, 0.0, 1.0)
            new_alpha = old_alpha.copy()
            new_alpha[white_mixed_edge] = np.minimum(
                old_alpha[white_mixed_edge],
                solved_alpha[white_mixed_edge],
            )

            # Reconstruct straight (unassociated) edge colour so the grey
            # checker preview cannot reveal the original white studio fringe.
            reconstruct = white_mixed_edge & (new_alpha > 0.025)
            recovered_colour = (
                cropped_rgb
                - backdrop * (1.0 - new_alpha[:, :, None])
            ) / np.maximum(new_alpha[:, :, None], 0.04)
            recovered_colour = np.clip(recovered_colour, 0, 255)
            confidence = np.clip(
                (new_alpha - 0.03) / 0.55,
                0.0,
                1.0,
            )[:, :, None]
            clean_colour = (
                nearest_colours * (1.0 - confidence)
                + recovered_colour * confidence
            )
            cropped_rgb[reconstruct] = clean_colour[reconstruct]
            alpha = np.round(new_alpha * 255.0).astype(np.uint8)
            alpha[white_mixed_edge & (new_alpha <= 0.025)] = 0

        # A verified silver seed has model support, strong local contrast and
        # is already excluded from the connected white studio background.
        # Keep that metal core opaque after floor/fringe cleanup; only the
        # undilated seed is restored, so neighbouring white pixels stay clear.
        if model_matte is not None and np.any(cropped_strict_silver_hardware):
            silver_seed_alpha = np.round(
                np.clip(model_matte[top:bottom, left:right], 0.0, 1.0) * 255.0
            ).astype(np.uint8)
            alpha[cropped_strict_silver_hardware] = np.maximum(
                alpha[cropped_strict_silver_hardware],
                silver_seed_alpha[cropped_strict_silver_hardware],
            )

    result = Image.fromarray(np.clip(cropped_rgb, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    result.putalpha(Image.fromarray(alpha, "L"))
    return result


def _remove_detached_floor_fragments(image: Image.Image) -> Image.Image:
    """Remove faint floor slivers created when a cutout is resized for export."""
    result = image.convert("RGBA").copy()
    rgba = np.asarray(result).copy()
    alpha = rgba[:, :, 3]
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        (alpha > 8).astype(np.uint8),
        8,
    )
    if component_count <= 1:
        return result
    largest_component = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    main_left = int(stats[largest_component, cv2.CC_STAT_LEFT])
    main_top = int(stats[largest_component, cv2.CC_STAT_TOP])
    main_width = int(stats[largest_component, cv2.CC_STAT_WIDTH])
    main_height = int(stats[largest_component, cv2.CC_STAT_HEIGHT])
    main_area = int(stats[largest_component, cv2.CC_STAT_AREA])
    central_left = main_left + round(main_width * 0.12)
    central_right = main_left + main_width - round(main_width * 0.12)
    product_span_left = main_left - round(main_width * 0.02)
    product_span_right = main_left + main_width + round(main_width * 0.02)
    lower_start = main_top + round(main_height * 0.72)
    maximum_height = max(5, result.height // 150)
    maximum_area = max(160, round(main_area * 0.002))

    changed = False
    for component in range(1, component_count):
        if component == largest_component:
            continue
        left = int(stats[component, cv2.CC_STAT_LEFT])
        top = int(stats[component, cv2.CC_STAT_TOP])
        width = int(stats[component, cv2.CC_STAT_WIDTH])
        height = int(stats[component, cv2.CC_STAT_HEIGHT])
        area = int(stats[component, cv2.CC_STAT_AREA])
        center_x = left + width / 2
        component_pixels = labels == component
        mean_alpha = float(np.mean(alpha[component_pixels]))
        if (
            top >= lower_start
            and height <= maximum_height
            and area <= maximum_area
            and product_span_left <= center_x <= product_span_right
            and mean_alpha < 190
        ):
            alpha[component_pixels] = 0
            changed = True

    # A large contact shadow can remain attached to a woven or pale bag and is
    # therefore part of the largest component. Detect the single row where a
    # broad, opaque product base abruptly collapses into a much narrower floor
    # strip. Only apply the cut when the removable region is substantial; this
    # keeps the established pink/blue behaviour, where the few lower pixels
    # belong to a real chain or antialiased curved base.
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        (alpha > 8).astype(np.uint8),
        8,
    )
    if component_count > 1:
        largest_component = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        main_left = int(stats[largest_component, cv2.CC_STAT_LEFT])
        main_top = int(stats[largest_component, cv2.CC_STAT_TOP])
        main_width = int(stats[largest_component, cv2.CC_STAT_WIDTH])
        main_height = int(stats[largest_component, cv2.CC_STAT_HEIGHT])
        main_area = int(stats[largest_component, cv2.CC_STAT_AREA])
        central_left = main_left + round(main_width * 0.10)
        central_right = main_left + main_width - round(main_width * 0.10)
        scan_start = main_top + round(main_height * 0.58)
        main_pixels = labels == largest_component
        hsv = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2HSV)
        yy, xx = np.indices(alpha.shape)
        material_sample = (
            main_pixels
            & (yy >= main_top + round(main_height * 0.30))
            & (yy <= main_top + round(main_height * 0.72))
            & (xx >= central_left)
            & (xx < central_right)
        )
        median_material_value = (
            float(np.median(hsv[:, :, 2][material_sample]))
            if np.any(material_sample)
            else 255.0
        )
        median_material_saturation = (
            float(np.median(hsv[:, :, 1][material_sample]))
            if np.any(material_sample)
            else 0.0
        )
        row_widths = np.count_nonzero(
            (alpha[:, central_left:central_right] >= 128)
            & main_pixels[:, central_left:central_right],
            axis=1,
        )
        scan_widths = row_widths[scan_start:main_top + main_height]
        broad_width = int(np.max(scan_widths)) if scan_widths.size else 0
        floor_row: int | None = None
        strongest_width_drop = 0
        for index in range(max(0, scan_widths.size - 1)):
            current_width = int(scan_widths[index])
            next_width = int(scan_widths[index + 1])
            width_drop = current_width - next_width
            if (
                current_width >= max(18, round(broad_width * 0.42))
                and width_drop >= max(10, round(broad_width * 0.26))
                and next_width <= round(current_width * 0.48)
                and width_drop > strongest_width_drop
            ):
                floor_row = scan_start + index
                strongest_width_drop = width_drop

        if floor_row is not None and median_material_value > 105:
            rgb = rgba[:, :, :3]
            red = rgb[:, :, 0].astype(np.int16)
            green = rgb[:, :, 1].astype(np.int16)
            blue = rgb[:, :, 2].astype(np.int16)
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            gradient = cv2.magnitude(
                cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3),
                cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3),
            )
            gold_core = (
                (hsv[:, :, 0] >= 7)
                & (hsv[:, :, 0] <= 42)
                & (hsv[:, :, 1] >= 90)
                & (hsv[:, :, 2] >= 105)
                & (red >= blue + 10)
                & (green >= blue + 5)
            )
            silver_core = (
                (hsv[:, :, 1] <= 24)
                & (hsv[:, :, 2] >= 55)
                & (hsv[:, :, 2] <= 235)
                & (gradient >= 45)
            )
            lower_hardware_band = (
                (yy >= floor_row - 12)
                & (alpha > 8)
            )
            hardware_candidate = cv2.dilate(
                ((gold_core | silver_core) & lower_hardware_band).astype(np.uint8),
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
                iterations=2,
            ).astype(bool) & lower_hardware_band
            hardware_count, hardware_labels = cv2.connectedComponents(
                hardware_candidate.astype(np.uint8),
                8,
            )
            floor_hardware = np.zeros_like(hardware_candidate)
            for component in range(1, hardware_count):
                component_pixels = hardware_labels == component
                if np.any(
                    component_pixels
                    & ((xx < central_left) | (xx >= central_right))
                ):
                    floor_hardware |= component_pixels

            connected_floor_residue = (
                main_pixels
                & (yy > floor_row)
                & (xx >= central_left)
                & (xx < central_right)
                & ~floor_hardware
            )
            minimum_residue_area = max(600, round(main_area * 0.004))
            if int(np.count_nonzero(connected_floor_residue)) >= minimum_residue_area:
                # Near the true base, the product's centre run must keep
                # tapering inward. White-studio shadows often remain as pale
                # side runs which separate from that centre run, widen again,
                # or hang below a nearby fitting. Remove only neutral/bright
                # pixels outside the centre run. This uses the white-background
                # invariant and silhouette continuity rather than a bag shape,
                # colour or SKU-specific crop.
                taper_start = max(
                    scan_start,
                    floor_row - max(18, round(main_height * 0.05)),
                )
                body_core_envelope = np.zeros_like(main_pixels)
                lateral_shadow_runs = np.zeros_like(main_pixels)
                product_center_x = main_left + main_width / 2
                verified_floor_metal = gold_core | (
                    silver_core
                    & (blue >= red - 2)
                )
                maximum_shadow_gap = max(12, round(main_width * 0.08))
                for row in range(taper_start, floor_row + 1):
                    row_columns = np.flatnonzero(
                        (alpha[row] >= 128)
                        & main_pixels[row]
                        & (xx[row] >= central_left)
                        & (xx[row] < central_right)
                    )
                    if not row_columns.size:
                        continue
                    row_runs: list[tuple[int, int]] = []
                    run_start = previous_column = int(row_columns[0])
                    for column_value in row_columns[1:]:
                        column = int(column_value)
                        if column > previous_column + 1:
                            row_runs.append((run_start, previous_column))
                            run_start = column
                        previous_column = column
                    row_runs.append((run_start, previous_column))
                    centre_run = min(
                        row_runs,
                        key=lambda run: (
                            0
                            if run[0] <= product_center_x <= run[1]
                            else min(
                                abs(product_center_x - run[0]),
                                abs(product_center_x - run[1]),
                            )
                        ),
                    )
                    body_core_envelope[row, centre_run[0]:centre_run[1] + 1] = True
                    for run in row_runs:
                        if run == centre_run:
                            continue
                        if run[1] < centre_run[0]:
                            run_gap = centre_run[0] - run[1] - 1
                        else:
                            run_gap = run[0] - centre_run[1] - 1
                        if run_gap > maximum_shadow_gap:
                            continue
                        run_slice = slice(run[0], run[1] + 1)
                        run_pixels = (
                            main_pixels[row, run_slice]
                            & (alpha[row, run_slice] > 8)
                        )
                        if not np.any(run_pixels):
                            continue
                        median_saturation = float(np.median(
                            hsv[row, run_slice, 1][run_pixels]
                        ))
                        median_value = float(np.median(
                            hsv[row, run_slice, 2][run_pixels]
                        ))
                        metal_fraction = float(np.mean(
                            verified_floor_metal[row, run_slice][run_pixels]
                        ))
                        if (
                            median_saturation <= 60
                            and median_value >= 90
                            and metal_fraction < 0.18
                        ):
                            lateral_shadow_runs[row, run_slice] |= run_pixels

                background_like_floor = (
                    (hsv[:, :, 1] <= 45)
                    & (hsv[:, :, 2] >= 165)
                )
                lateral_floor_residue = (
                    lateral_shadow_runs
                    | (
                        main_pixels
                        & (yy >= taper_start)
                        & (yy <= floor_row)
                        & ~body_core_envelope
                        & background_like_floor
                        & ~verified_floor_metal
                    )
                )
                alpha[lateral_floor_residue] = 0
                alpha[connected_floor_residue] = 0
                changed = True

        # Dark products need a relative test: their white-studio shadow can be
        # much brighter than the material even when its absolute value is not
        # high enough for the pale/colourful branch above. Limit this cleanup
        # to the detected collapse row and below, and keep saturated gold plus
        # cool-channel silver cores. No bag class or fixed crop is involved.
        if floor_row is not None and median_material_value <= 105:
            rgb = rgba[:, :, :3]
            red = rgb[:, :, 0].astype(np.int16)
            green = rgb[:, :, 1].astype(np.int16)
            blue = rgb[:, :, 2].astype(np.int16)
            dark_gold_core = (
                (hsv[:, :, 0] >= 7)
                & (hsv[:, :, 0] <= 42)
                & (hsv[:, :, 1] >= 90)
                & (hsv[:, :, 2] >= 105)
                & (red >= blue + 10)
                & (green >= blue + 5)
            )
            dark_silver_core = (
                (hsv[:, :, 1] <= 24)
                & (hsv[:, :, 2] >= 55)
                & (hsv[:, :, 2] <= 235)
                & (blue >= red + 2)
            )
            relative_shadow_value = max(
                75,
                round(median_material_value * 1.70),
            )
            dark_floor_residue = (
                main_pixels
                & (yy >= floor_row)
                & (hsv[:, :, 1] <= 65)
                & (hsv[:, :, 2] >= relative_shadow_value)
                & ~dark_gold_core
                & ~dark_silver_core
            )
            minimum_dark_residue = max(100, round(main_area * 0.0005))
            if int(np.count_nonzero(dark_floor_residue)) >= minimum_dark_residue:
                alpha[dark_floor_residue] = 0
                changed = True

        # Confidence feathering can leave colour-cast studio shadow attached
        # by a one-pixel bridge. On genuinely coloured products, clean the
        # lowest ten percent using chroma and value relative to that product's
        # own material sample. Pale/white and dark products do not enter this
        # rule; verified gold/cool-silver pixels are explicitly retained.
        if (
            median_material_value > 105
            and median_material_saturation >= 28
        ):
            rgb = rgba[:, :, :3]
            red = rgb[:, :, 0].astype(np.int16)
            green = rgb[:, :, 1].astype(np.int16)
            blue = rgb[:, :, 2].astype(np.int16)
            coloured_floor_gold = (
                (hsv[:, :, 0] >= 7)
                & (hsv[:, :, 0] <= 42)
                & (hsv[:, :, 1] >= 90)
                & (hsv[:, :, 2] >= 105)
                & (red >= blue + 10)
                & (green >= blue + 5)
            )
            coloured_floor_silver = (
                (hsv[:, :, 1] <= 24)
                & (hsv[:, :, 2] >= 55)
                & (blue >= red + 2)
            )
            coloured_floor_hardware = cv2.dilate(
                (coloured_floor_gold | coloured_floor_silver).astype(np.uint8),
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
                iterations=1,
            ).astype(bool)

            # Judge chroma relative to the product itself. A fixed cutoff
            # mistakes pastel leather for a neutral studio shadow, while a
            # relative cutoff still removes grey/colour-cast residue from
            # both strongly coloured fabric and lightly coloured leather.
            opaque_chroma_limit = float(np.clip(
                median_material_saturation * 0.58,
                18,
                40,
            ))
            translucent_chroma_limit = float(np.clip(
                median_material_saturation * 0.90,
                24,
                60,
            ))
            cast_shadow_value_limit = median_material_value * 0.76
            cast_shadow_chroma_limit = float(np.clip(
                median_material_saturation * 1.35,
                36,
                70,
            ))
            low_chroma_floor_residue = (
                main_pixels
                & (yy >= main_top + round(main_height * 0.90))
                & (alpha > 8)
                & (
                    (
                        (alpha >= 230)
                        & (hsv[:, :, 1] <= opaque_chroma_limit)
                    )
                    | (
                        (alpha < 230)
                        & (hsv[:, :, 1] <= translucent_chroma_limit)
                    )
                    | (
                        (hsv[:, :, 2] <= cast_shadow_value_limit)
                        & (hsv[:, :, 1] <= cast_shadow_chroma_limit)
                    )
                )
                & ~coloured_floor_hardware
            )
            alpha[low_chroma_floor_residue] = 0
            if np.any(low_chroma_floor_residue):
                changed = True

    # Removing an attached shadow can expose tiny antialiased islands which
    # were connected to the product during the first component pass. Run the
    # same conservative faint-fragment test once more after that separation.
    if changed:
        final_component_count, final_labels, final_stats, _ = cv2.connectedComponentsWithStats(
            (alpha > 8).astype(np.uint8),
            8,
        )
        if final_component_count > 1:
            final_largest = 1 + int(np.argmax(final_stats[1:, cv2.CC_STAT_AREA]))
            for component in range(1, final_component_count):
                if component == final_largest:
                    continue
                left = int(final_stats[component, cv2.CC_STAT_LEFT])
                top = int(final_stats[component, cv2.CC_STAT_TOP])
                width = int(final_stats[component, cv2.CC_STAT_WIDTH])
                height = int(final_stats[component, cv2.CC_STAT_HEIGHT])
                area = int(final_stats[component, cv2.CC_STAT_AREA])
                center_x = left + width / 2
                component_pixels = final_labels == component
                mean_alpha = float(np.mean(alpha[component_pixels]))
                if (
                    top >= lower_start
                    and height <= maximum_height
                    and area <= maximum_area
                    and product_span_left <= center_x <= product_span_right
                    and mean_alpha < 190
                ):
                    alpha[component_pixels] = 0

    if changed:
        result.putalpha(Image.fromarray(alpha, "L"))
    return result


def _normalize_adjustment(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value or {}

    def number(name: str, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value.get(name, default))
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def optional_number(name: str, minimum: float, maximum: float) -> float | None:
        raw = value.get(name)
        if raw is None:
            return None
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed):
            return None
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
        "phone_alignment": "center" if value.get("phone_alignment") == "center" else "bottom",
        "product_show_ruler": value.get("product_show_ruler") is not False,
        "phone_show_ruler": value.get("phone_show_ruler") is not False,
        "product_ruler_group_scale": number("product_ruler_group_scale", 1.0, 0.25, 4.0),
        "product_ruler_group_offset_x": number("product_ruler_group_offset_x", 0.0, -1.5, 1.5),
        "product_ruler_group_offset_y": number("product_ruler_group_offset_y", 0.0, -1.5, 1.5),
        "product_ruler_base_left": optional_number("product_ruler_base_left", -2000.0, 4000.0),
        "product_ruler_base_top": optional_number("product_ruler_base_top", -2000.0, 4000.0),
        "product_ruler_base_right": optional_number("product_ruler_base_right", -2000.0, 4000.0),
        "product_ruler_base_bottom": optional_number("product_ruler_base_bottom", -2000.0, 4000.0),
        "length_ruler_scale": number("length_ruler_scale", 1.0, 0.5, 2.0),
        "length_ruler_offset_x": number("length_ruler_offset_x", 0.0, -1.5, 1.5),
        "length_ruler_offset_y": number("length_ruler_offset_y", 0.0, -1.5, 1.5),
        "height_ruler_scale": number("height_ruler_scale", 1.0, 0.5, 2.0),
        "height_ruler_offset_x": number("height_ruler_offset_x", 0.0, -1.5, 1.5),
        "height_ruler_offset_y": number("height_ruler_offset_y", 0.0, -1.5, 1.5),
        "width_ruler_scale": number("width_ruler_scale", 1.0, 0.5, 2.0),
        "width_ruler_offset_x": number("width_ruler_offset_x", 0.0, -1.5, 1.5),
        "width_ruler_offset_y": number("width_ruler_offset_y", 0.0, -1.5, 1.5),
        "phone_ruler_scale": number("phone_ruler_scale", 1.0, 0.5, 2.0),
        "phone_ruler_offset_x": number("phone_ruler_offset_x", 0.0, -1.5, 1.5),
        "phone_ruler_offset_y": number("phone_ruler_offset_y", 0.0, -1.5, 1.5),
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


@lru_cache(maxsize=16)
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


def _clamp_layer_origin(position: int, layer_size: int, minimum: int, maximum: int) -> int:
    available = max(1, maximum - minimum)
    if layer_size <= available:
        return max(minimum, min(position, maximum - layer_size))
    return max(maximum - layer_size, min(position, minimum))


def _paste_layer(
    canvas: Image.Image,
    layer: Image.Image,
    box: tuple[int, int, int, int],
    adjustment: dict[str, Any] | None = None,
    *,
    mode: str = "contain",
    clip_box: tuple[int, int, int, int] | None = None,
    minimum_top: int | None = None,
    maximum_bottom: int | None = None,
    allow_free_position: bool = False,
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
    if not allow_free_position:
        global_x = _clamp_layer_origin(global_x, rendered.width, clip_left, clip_right)
        global_y = _clamp_layer_origin(global_y, rendered.height, clip_top, clip_bottom)
    if minimum_top is not None:
        global_y = max(minimum_top, global_y)
    if maximum_bottom is not None:
        latest_y = maximum_bottom - rendered.height
        if minimum_top is None or latest_y >= minimum_top:
            global_y = min(latest_y, global_y)
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


def _detail_shape_offset_y(cutout: Image.Image) -> float:
    ratio = cutout.width / max(1, cutout.height)
    if ratio <= 0.78:
        return -0.105
    if ratio <= 1.05:
        return -0.11
    if ratio <= 1.45:
        return -0.12
    return -0.13


def _paste_detail_layer(
    canvas: Image.Image,
    source: Image.Image,
    box: tuple[int, int, int, int],
    adjustment: dict[str, Any] | None,
    *,
    clip_box: tuple[int, int, int, int] | None = None,
    auto_zoom: float = 0.9,
    auto_offset_y: float = -0.045,
    auto_handle_layout: bool = False,
    auto_shape_layout: bool = False,
    default_mode: str = "cover",
) -> None:
    cropped = _crop_source(source, adjustment)
    if not _has_manual_crop(adjustment) and _has_light_studio_border(cropped):
        cutout = _product_cutout(cropped)
        normalized = _normalize_adjustment(adjustment)
        handle_offset_y = -0.04 * _handle_visual_lift(cutout) if auto_handle_layout else 0.0
        shape_offset_y = _detail_shape_offset_y(cutout) if auto_shape_layout else auto_offset_y
        _paste_layer(
            canvas,
            cutout,
            box,
            {
                "zoom": normalized["zoom"] * auto_zoom,
                "offset_x": normalized["offset_x"],
                "offset_y": normalized["offset_y"] + shape_offset_y + handle_offset_y,
            },
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
    auto_handle_layout: bool = False,
    auto_tall_handle_drop: bool = False,
    tall_handle_drop_ratio: float = 0.12,
    auto_offset_y: float = 0.0,
    minimum_rendered_top: int | None = None,
    tall_handle_minimum_rendered_top: int | None = None,
    maximum_rendered_bottom: int | None = None,
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
    has_manual_layout = _has_manual_layout_adjustment(adjustment)
    effective_minimum_top = None if has_manual_layout else minimum_rendered_top
    if not has_manual_layout and tall_handle_minimum_rendered_top is not None:
        body_left, body_top, body_right, body_bottom = _info_measurement_bbox(cutout)
        body_ratio = (body_right - body_left) / max(1, body_bottom - body_top)
        if body_ratio <= 1.15 and _handle_visual_lift(cutout) >= 0.55:
            effective_minimum_top = max(
                effective_minimum_top or 0,
                tall_handle_minimum_rendered_top,
            )
    layout_adjustment = adjustment
    if auto_handle_layout and not _has_manual_crop(adjustment):
        normalized = _normalize_adjustment(adjustment)
        left, top, right, bottom = box
        box_width = max(1, right - left)
        box_height = max(1, bottom - top)
        scale = min(box_width / cutout.width, box_height / cutout.height) * normalized["zoom"]
        body_left, body_top, body_right, body_bottom = _info_measurement_bbox(cutout)
        body_center_x = (body_left + body_right) / 2
        body_center_y = (body_top + body_bottom) / 2
        tall_handle_drop_y = tall_handle_drop_ratio * _handle_visual_lift(cutout) if auto_tall_handle_drop else 0.0
        layout_adjustment = {
            **normalized,
            "offset_x": normalized["offset_x"] + (cutout.width / 2 - body_center_x) * scale / box_width,
            "offset_y": (
                normalized["offset_y"]
                + auto_offset_y
                + tall_handle_drop_y
                + (cutout.height / 2 - body_center_y) * scale / box_height
            ),
        }
    _paste_layer(
        canvas,
        cutout,
        box,
        layout_adjustment,
        clip_box=clip_box,
        minimum_top=effective_minimum_top,
        maximum_bottom=None if has_manual_layout else maximum_rendered_bottom,
    )


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


def _info_measurement_bbox(cutout: Image.Image) -> tuple[int, int, int, int]:
    """Estimate the bag outline used by the 401 length/height rulers."""
    alpha = np.asarray(cutout.getchannel("A"))
    mask = alpha > 28
    ys, xs = np.where(mask)
    if not len(xs):
        return 0, 0, cutout.width, cutout.height

    full_left, full_top = int(xs.min()), int(ys.min())
    full_right, full_bottom = int(xs.max()) + 1, int(ys.max()) + 1
    row_counts = mask.sum(axis=1)
    row_spans = np.zeros(mask.shape[0], dtype=np.int32)
    row_longest_segments = np.zeros(mask.shape[0], dtype=np.int32)
    for row_index in np.flatnonzero(row_counts):
        row_xs = np.flatnonzero(mask[row_index])
        row_spans[row_index] = int(row_xs[-1] - row_xs[0] + 1)
        split_points = np.flatnonzero(np.diff(row_xs) > 1)
        segment_starts = np.r_[0, split_points + 1]
        segment_ends = np.r_[split_points, len(row_xs) - 1]
        row_longest_segments[row_index] = int(
            np.max(row_xs[segment_ends] - row_xs[segment_starts] + 1)
        )
    # A handle or chain may connect to the bag but normally occupies far fewer
    # pixels per row than the body. Keep the visible body shoulders while
    # excluding those sparse rows from the physical height ruler.
    # Handles, chain loops and sparse hardware can be visually thick while
    # still covering far less of each row than the actual bag body.
    max_row_count = float(row_counts.max())
    body_rows = row_counts >= max(8, int(round(max_row_count * 0.65)))
    full_width = max(1, full_right - full_left)
    row_fill = np.divide(
        row_counts,
        row_spans,
        out=np.zeros_like(row_counts, dtype=np.float64),
        where=row_spans > 0,
    )
    # A hobo/crescent bag has two high body shoulders separated by its opening.
    # Those rows are sparse by total pixel count, but span most of the bag and
    # contain wider solid segments than chains. Include them without pulling a
    # compact, continuous tote handle into the physical height measurement.
    shoulder_rows = (
        (row_counts >= max(6, int(round(max_row_count * 0.10))))
        & (row_spans >= max(12, int(round(full_width * 0.55))))
        & (row_longest_segments >= max(6, int(round(full_width * 0.07))))
        & (row_fill >= 0.12)
        & (row_fill <= 0.82)
    )
    body_rows |= shoulder_rows
    row_run = _mask_longest_run(body_rows)
    if row_run is None:
        return full_left, full_top, full_right, full_bottom

    body_top, body_bottom = row_run
    body_mask = mask[body_top:body_bottom]
    column_counts = body_mask.sum(axis=0)
    body_columns = column_counts >= max(2, int(round((body_bottom - body_top) * 0.08)))
    column_run = _mask_longest_run(body_columns)
    if column_run is None:
        body_left, body_right = full_left, full_right
    else:
        body_left, body_right = column_run
    return (
        max(full_left, body_left),
        max(full_top, body_top),
        min(full_right, body_right),
        min(full_bottom, body_bottom),
    )


def _handle_visual_lift(cutout: Image.Image) -> float:
    """Return a proportional upward shift for handles above the solid bag body."""
    alpha = np.asarray(cutout.getchannel("A"))
    ys, _ = np.where(alpha > 28)
    if not len(ys):
        return 0.0
    full_top = int(ys.min())
    full_bottom = int(ys.max()) + 1
    _, body_top, _, body_bottom = _info_measurement_bbox(cutout)
    body_height = max(1, body_bottom - body_top)
    headroom = max(0, body_top - full_top)
    if headroom < max(6, round((full_bottom - full_top) * 0.04)):
        return 0.0
    ratio = headroom / body_height
    return max(0.0, min(1.0, (ratio - 0.06) / 0.28))


def _multi_angle_visual_row_shift(source: Image.Image) -> int:
    """Pull the two 606 rows toward the center when the bag has tall handles."""
    image_id = source.info.get("_organizer_image_id")
    modified_ns = source.info.get("_organizer_modified_ns")
    if isinstance(image_id, int) and isinstance(modified_ns, int):
        cutout = _cached_product_cutout(
            image_id,
            modified_ns,
            _crop_cache_key(None),
        ).copy()
    else:
        cutout = _product_cutout(source)
    handle_lift = _handle_visual_lift(cutout)
    if handle_lift < 0.55:
        return 0
    return round(13 * handle_lift)


def _paste_info_product(
    canvas: Image.Image,
    source: Image.Image,
    adjustment: dict[str, Any] | None,
) -> tuple[float, float, float, float]:
    image_id = source.info.get("_organizer_image_id")
    modified_ns = source.info.get("_organizer_modified_ns")
    if isinstance(image_id, int) and isinstance(modified_ns, int):
        cutout = _cached_product_cutout(image_id, modified_ns, _crop_cache_key(adjustment)).copy()
    else:
        cutout = _product_cutout(_crop_source(source, adjustment))

    left, top, right, bottom = INFO_PRODUCT_BOX
    box_width = right - left
    box_height = bottom - top
    normalized = _normalize_adjustment(adjustment)
    scale = min(box_width / cutout.width, box_height / cutout.height) * normalized["zoom"]
    rendered = cutout.resize(
        (max(1, round(cutout.width * scale)), max(1, round(cutout.height * scale))),
        Image.Resampling.LANCZOS,
    )
    body_left, body_top, body_right, body_bottom = _jd_product_body_bbox(cutout)
    body_center_x = (body_left + body_right) / 2
    body_center_y = (body_top + body_bottom) / 2
    x = left + (box_width - rendered.width) // 2 + round(normalized["offset_x"] * box_width)
    y = top + (box_height - rendered.height) // 2 + round(normalized["offset_y"] * box_height)
    if not _has_manual_crop(adjustment):
        x += round((cutout.width / 2 - body_center_x) * scale)
        y += round((cutout.height / 2 - body_center_y) * scale)
    canvas.paste(rendered.convert("RGB"), (x, y), rendered.getchannel("A"))
    return (
        x + body_left * scale,
        y + body_top * scale,
        x + body_right * scale,
        y + body_bottom * scale,
    )


def _info_ruler_geometry(
    body: tuple[float, float, float, float],
) -> dict[str, int]:
    ruler_gap = 34
    body_left, body_top, body_right, body_bottom = body
    line_left = round(body_left + 4)
    line_right = round(body_right - 4)
    line_bottom = round(body_bottom - 9)
    line_top = round(body_top - 5)
    vertical_x = max(285, line_left - ruler_gap)
    horizontal_y = min(535, line_bottom + ruler_gap)
    return {
        "left": line_left,
        "right": line_right,
        "top": line_top,
        "bottom": line_bottom,
        "vertical_x": vertical_x,
        "horizontal_y": horizontal_y,
    }


def _transform_ruler_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    scale: float,
    offset_x: float,
    offset_y: float,
    canvas_size: tuple[int, int],
    origin: tuple[float, float] | None = None,
) -> tuple[tuple[int, int], tuple[int, int]]:
    center_x = origin[0] if origin is not None else (start[0] + end[0]) / 2
    center_y = origin[1] if origin is not None else (start[1] + end[1]) / 2
    move_x = offset_x * canvas_size[0] * 0.18
    move_y = offset_y * canvas_size[1] * 0.18

    def transform(point: tuple[float, float]) -> tuple[int, int]:
        return (
            round(center_x + (point[0] - center_x) * scale + move_x),
            round(center_y + (point[1] - center_y) * scale + move_y),
        )

    return transform(start), transform(end)


def _transform_product_ruler_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    product_center: tuple[float, float],
    normalized: dict[str, Any],
    *,
    scale: float,
    offset_x: float,
    offset_y: float,
    canvas_size: tuple[int, int],
) -> tuple[tuple[int, int], tuple[int, int]]:
    grouped_start, grouped_end = _transform_ruler_segment(
        start,
        end,
        scale=normalized["product_ruler_group_scale"],
        offset_x=normalized["product_ruler_group_offset_x"],
        offset_y=normalized["product_ruler_group_offset_y"],
        canvas_size=canvas_size,
        origin=product_center,
    )
    return _transform_ruler_segment(
        grouped_start,
        grouped_end,
        scale=scale,
        offset_x=offset_x,
        offset_y=offset_y,
        canvas_size=canvas_size,
    )


def _stored_product_ruler_base(
    normalized: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    values = (
        normalized.get("product_ruler_base_left"),
        normalized.get("product_ruler_base_top"),
        normalized.get("product_ruler_base_right"),
        normalized.get("product_ruler_base_bottom"),
    )
    if not all(isinstance(value, (int, float)) for value in values):
        return None
    left, top, right, bottom = (float(value) for value in values)
    return (left, top, right, bottom) if right > left and bottom > top else None


def _info_width_ruler_geometry(
    base_body: tuple[float, float, float, float],
    adjustment: dict[str, Any] | None = None,
    *,
    product_center: tuple[float, float] | None = None,
    canvas_size: tuple[int, int] = (750, 665),
) -> dict[str, Any]:
    normalized = _normalize_adjustment(adjustment)
    _, _, body_right, body_bottom = base_body
    ruler_gap = 34.0
    anchor_direction = (22.0 ** 2 + 18.0 ** 2) ** 0.5
    start = (
        min(660.0, body_right + 22.0 / anchor_direction * ruler_gap),
        min(520.0, body_bottom + 18.0 / anchor_direction * ruler_gap),
    )
    end = (start[0] + 51.0, start[1] - 27.0)
    def transform(
        segment_start: tuple[float, float],
        segment_end: tuple[float, float],
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        if product_center is not None:
            return _transform_product_ruler_segment(
                segment_start,
                segment_end,
                product_center,
                normalized,
                scale=normalized["width_ruler_scale"],
                offset_x=normalized["width_ruler_offset_x"],
                offset_y=normalized["width_ruler_offset_y"],
                canvas_size=canvas_size,
            )
        return _transform_ruler_segment(
            segment_start,
            segment_end,
            scale=normalized["width_ruler_scale"],
            offset_x=normalized["width_ruler_offset_x"],
            offset_y=normalized["width_ruler_offset_y"],
            canvas_size=canvas_size,
        )

    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    length = max(1.0, (delta_x ** 2 + delta_y ** 2) ** 0.5)
    perpendicular = (-delta_y / length * 9.0, delta_x / length * 9.0)
    segments = [
        (start, end),
        ((start[0] - perpendicular[0], start[1] - perpendicular[1]), (start[0] + perpendicular[0], start[1] + perpendicular[1])),
        ((end[0] - perpendicular[0], end[1] - perpendicular[1]), (end[0] + perpendicular[0], end[1] + perpendicular[1])),
    ]
    text_point = (start[0] + 8, start[1] + 8)
    return {
        "segments": [transform(segment_start, segment_end) for segment_start, segment_end in segments],
        "text": transform(text_point, text_point)[0],
        "scale": normalized["width_ruler_scale"],
    }


def _normalized_product_page(
    source: Image.Image,
    size: tuple[int, int] = (800, 800),
    box: tuple[int, int, int, int] | None = None,
    transparent: bool = False,
    adjustment: dict[str, Any] | None = None,
    auto_handle_layout: bool = False,
    auto_tall_handle_drop: bool = False,
    tall_handle_drop_ratio: float = 0.12,
    auto_offset_y: float = 0.0,
    manual_padding_ratio: float | None = None,
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
    clip_box = _expanded_safe_box(
        safe_box,
        size,
        padding_ratio=manual_padding_ratio if manual_padding_ratio is not None else (0.18 if auto_handle_layout else 0.055),
    ) if _has_manual_layout_adjustment(adjustment) or auto_handle_layout else None
    _paste_product(
        canvas,
        source,
        safe_box,
        adjustment,
        clip_box=clip_box,
        auto_handle_layout=auto_handle_layout,
        auto_tall_handle_drop=auto_tall_handle_drop,
        tall_handle_drop_ratio=tall_handle_drop_ratio,
        auto_offset_y=auto_offset_y,
    )
    return canvas


def _tag_certificate_page(
    source: Image.Image,
    adjustment: dict[str, Any] | None = None,
) -> Image.Image:
    """Keep the uploaded tag image intact unless the designer explicitly crops it."""
    canvas = Image.new("RGB", (750, 750), "white")
    normalized_source = ImageOps.exif_transpose(source).convert("RGB")
    layer = _crop_source(normalized_source, adjustment)
    _paste_layer(
        canvas,
        layer,
        (0, 0, 750, 750),
        adjustment,
        mode="contain",
        clip_box=(0, 0, 750, 750),
    )
    return canvas


def _catalog_product_page(
    source: Image.Image,
    adjustment: dict[str, Any] | None = None,
    *,
    tall_handle_drop_ratio: float = 0.12,
) -> Image.Image:
    """Match the catalog reference while centering the visible bag body."""
    return _normalized_product_page(
        source,
        adjustment=adjustment,
        auto_handle_layout=True,
        auto_tall_handle_drop=True,
        tall_handle_drop_ratio=tall_handle_drop_ratio,
        auto_offset_y=-0.03,
    )


def _dimension_value_mm(value: str | None) -> float | None:
    normalized = str(value or "").strip().lower().replace("，", ".")
    if not normalized:
        return None
    match = re.search(r"\d+(?:\.\d+)?", normalized)
    if not match:
        return None
    number = float(match.group(0))
    if "cm" in normalized:
        number *= 10
    return number


def _jd_size_dimensions_ready(product_info: dict[str, str]) -> bool:
    return (
        _dimension_value_mm(product_info.get("product_length")) is not None
        and _dimension_value_mm(product_info.get("product_height")) is not None
    )


def _vip_info_ready(product_info: dict[str, str]) -> bool:
    return _jd_size_dimensions_ready(product_info)


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
    title_font = _font(32, True)
    draw.text((290, 40), title, font=title_font, fill="#101010")

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

    normalized = _normalize_adjustment(adjustment)
    base_body = (332.0, 242.0, 647.0, 506.0)
    body = base_body
    if product_image is not None:
        if _has_manual_layout_adjustment(adjustment):
            base_adjustment = {
                **normalized,
                "zoom": 1.0,
                "offset_x": 0.0,
                "offset_y": 0.0,
            }
            base_body = _paste_info_product(
                Image.new("RGB", image.size, "white"),
                product_image,
                base_adjustment,
            )
        body = _paste_info_product(image, product_image, adjustment)
        if not _has_manual_layout_adjustment(adjustment):
            base_body = body
    ruler_body = _stored_product_ruler_base(normalized) or body
    ruler = _info_ruler_geometry(ruler_body)
    product_ruler_center = (
        (INFO_PRODUCT_BOX[0] + INFO_PRODUCT_BOX[2]) / 2,
        (INFO_PRODUCT_BOX[1] + INFO_PRODUCT_BOX[3]) / 2,
    )
    width_ruler = _info_width_ruler_geometry(
        ruler_body,
        adjustment,
        product_center=product_ruler_center,
        canvas_size=image.size,
    )

    line_color = "#8a8a8a"
    length_start, length_end = _transform_product_ruler_segment(
        (ruler["left"], ruler["horizontal_y"]),
        (ruler["right"], ruler["horizontal_y"]),
        product_ruler_center,
        normalized,
        scale=normalized["length_ruler_scale"],
        offset_x=normalized["length_ruler_offset_x"],
        offset_y=normalized["length_ruler_offset_y"],
        canvas_size=image.size,
    )
    draw.line((length_start, length_end), fill=line_color, width=2)
    draw.line((length_start[0], length_start[1] - 9, length_start[0], length_start[1] + 9), fill=line_color, width=2)
    draw.line((length_end[0], length_end[1] - 9, length_end[0], length_end[1] + 9), fill=line_color, width=2)
    length_text = _dimension_mm(info.get("product_length") or "")
    length_font = _font(19)
    length_box = draw.textbbox((0, 0), length_text, font=length_font)
    length_center = (length_start[0] + length_end[0]) / 2
    draw.text((length_center - (length_box[2] - length_box[0]) / 2, length_start[1] + 16), length_text, font=length_font, fill="#555555")

    height_start, height_end = _transform_product_ruler_segment(
        (ruler["vertical_x"], ruler["top"]),
        (ruler["vertical_x"], ruler["bottom"]),
        product_ruler_center,
        normalized,
        scale=normalized["height_ruler_scale"],
        offset_x=normalized["height_ruler_offset_x"],
        offset_y=normalized["height_ruler_offset_y"],
        canvas_size=image.size,
    )
    draw.line((height_start, height_end), fill=line_color, width=2)
    draw.line((height_start[0] - 9, height_start[1], height_start[0] + 9, height_start[1]), fill=line_color, width=2)
    draw.line((height_end[0] - 9, height_end[1], height_end[0] + 9, height_end[1]), fill=line_color, width=2)
    _draw_rotated_text(
        image,
        _dimension_mm(info.get("product_height") or ""),
        (height_start[0] - 45, round((height_start[1] + height_end[1]) / 2 - 49)),
        90,
        _font(18),
    )

    ruler_layer = Image.new("RGBA", (image.width * 4, image.height * 4), (0, 0, 0, 0))
    ruler_draw = ImageDraw.Draw(ruler_layer)
    for start, end in width_ruler["segments"]:
        ruler_draw.line(
            (start[0] * 4, start[1] * 4, end[0] * 4, end[1] * 4),
            fill=line_color,
            width=8,
        )
    ruler_layer = ruler_layer.resize(image.size, Image.Resampling.LANCZOS)
    image.paste(ruler_layer, (0, 0), ruler_layer)
    _draw_rotated_text(
        image,
        _dimension_mm(info.get("product_width") or ""),
        width_ruler["text"],
        26,
        _font(18),
    )

    disclaimer = info.get("disclaimer") or "包身长宽高测量均为最长部分\n误差在1-2cm之间因手工测量均属正常"
    notes = [line.strip() for line in disclaimer.splitlines() if line.strip()]
    if len(notes) < 2:
        notes = textwrap.wrap(disclaimer.replace("\n", " "), width=31)[:2]
    for index, line in enumerate(notes):
        draw.text((330, 585 + index * 27), f"* {line[:34]}", font=_font(15), fill="#222222")
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
    """Match the titled 604 interior-detail reference."""
    canvas = Image.new("RGB", (750, 750), "white")
    draw = ImageDraw.Draw(canvas)
    title = "细节展示"
    title_font = _font(34, True)
    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((750 - (title_box[2] - title_box[0])) / 2, 70), title, font=title_font, fill="#c4c4c4")
    box = (52, 181, 695, 704)
    # Keep automatic output and every zoom level on one fixed clipping canvas.
    # Switching clip bounds after the first zoom made 604/605 appear to grow
    # from alternating sides and could differ after saving.
    clip_box = (30, 135, 720, 720)
    _paste_detail_layer(
        canvas,
        source,
        box,
        adjustment,
        clip_box=clip_box,
        auto_zoom=0.82,
        auto_shape_layout=True,
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
    sources = [_load_image(image_id) for image_id in image_ids[:4]]
    visual_row_shift = _multi_angle_visual_row_shift(sources[0]) if sources else 0
    for index, (source, box) in enumerate(zip(sources, boxes)):
        adjustment = adjustments[index] if index < len(adjustments) else None
        row_shift = visual_row_shift if index < 2 else -visual_row_shift
        shifted_box = (box[0], box[1] + row_shift, box[2], box[3] + row_shift)
        clip_box = (
            _expanded_safe_box(shifted_box, canvas.size, padding_ratio=0.06)
            if _has_manual_layout_adjustment(adjustment)
            else None
        )
        _paste_product(canvas, source, shifted_box, adjustment, clip_box=clip_box)
    draw.line((346, 420, 404, 420), fill="#a8a8a8", width=2)
    draw.line((375, 391, 375, 449), fill="#a8a8a8", width=2)
    return canvas


def _save_png_30(image: Image.Image, path: Path) -> None:
    canvas = _normalized_product_page(image, transparent=True)
    _save_bounded_png(canvas, path)


def _save_bounded_png(image: Image.Image, path: Path) -> None:
    image.save(path, optimize=True)
    if path.stat().st_size > 600_000:
        for colors in (256, 192, 128, 96, 64):
            image.quantize(colors=colors).save(path, optimize=True)
            if path.stat().st_size <= 600_000:
                break
    if path.stat().st_size < 100_000:
        padding = PngImagePlugin.PngInfo()
        padding.add_text("size-padding", "0" * (100_000 - path.stat().st_size + 128), zip=False)
        image.save(path, optimize=True, pnginfo=padding)
    size = path.stat().st_size
    if not 100_000 <= size <= 600_000:
        raise ValueError("30.png 文件大小无法控制在 100KB 到 600KB")


@lru_cache(maxsize=8)
def _jd_logo_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [JD_LOGO_FONT_PATH, BUNDLED_FONT_PATH]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


@lru_cache(maxsize=2)
def _jd_elle_logo_layer(color: str = "black") -> Image.Image:
    template_path = JD_LOGO_WHITE_PATH if color == "white" else JD_LOGO_BLACK_PATH
    if template_path.is_file():
        with Image.open(template_path) as source:
            return source.convert("RGBA").resize((190, 60), Image.Resampling.LANCZOS)

    font = _jd_logo_font(160)
    text = "E L L E"
    bbox = font.getbbox(text)
    layer = Image.new("L", (bbox[2] - bbox[0] + 8, bbox[3] - bbox[1] + 8), 0)
    draw = ImageDraw.Draw(layer)
    draw.text((4 - bbox[0], 4 - bbox[1]), text, font=font, fill=255, stroke_width=0)
    glyph_bbox = layer.getbbox()
    if glyph_bbox:
        layer = layer.crop(glyph_bbox)
    mask = layer.resize((190, 60), Image.Resampling.LANCZOS)
    rendered = Image.new("RGBA", mask.size, "#ffffff" if color == "white" else "#111111")
    rendered.putalpha(mask)
    return rendered


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
    logo = _jd_elle_logo_layer("white" if color == "white" else "black")
    canvas.paste(logo.convert("RGB"), position, logo.getchannel("A"))


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
    detail_offset_y: float = -0.055,
    handle_aware: bool = False,
    logo_color: str = "black",
) -> Image.Image:
    canvas = Image.new("RGB", size, "white")
    if detail:
        detail_box = (
            round(size[0] * 0.0875),
            round(size[1] * 0.145),
            round(size[0] * 0.9125),
            round(size[1] * 0.92),
        )
        clip_box = _expanded_safe_box(detail_box, size, padding_ratio=0.14) if _has_manual_layout_adjustment(adjustment) else None
        _paste_detail_layer(
            canvas,
            source,
            detail_box,
            adjustment,
            clip_box=clip_box,
            auto_zoom=0.9,
            auto_offset_y=detail_offset_y,
            auto_handle_layout=handle_aware,
        )
    else:
        if size == (800, 800):
            box = (100, 135, 700, 700)
        else:
            box = (100, 145, 650, 900)
        clip_box = _expanded_safe_box(box, size, padding_ratio=0.18)
        _paste_product(
            canvas,
            source,
            box,
            adjustment,
            clip_box=clip_box,
            auto_handle_layout=True,
            auto_tall_handle_drop=True,
            minimum_rendered_top=(
                162
                if size == (800, 800)
                else 175
            ),
            tall_handle_minimum_rendered_top=(
                180
                if size == (800, 800)
                else 185
            ),
            maximum_rendered_bottom=(
                740
                if size == (800, 800)
                else 930
            ),
        )
    _draw_jd_elle_logo(canvas, size, logo_color)
    return canvas


def _jd_interior_detail_page(
    source: Image.Image,
    size: tuple[int, int],
    adjustment: dict[str, Any] | None,
    logo_color: str = "black",
) -> Image.Image:
    """Use the VIP 15 interior-detail framing while retaining the JD logo."""
    canvas = Image.new("RGB", size, "white")
    _paste_detail_layer(
        canvas,
        source,
        (0, 0, *size),
        adjustment,
        auto_zoom=0.9,
        auto_offset_y=0.0,
        default_mode="contain",
    )
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
    """Measure the bag body, including crescent shoulders but excluding handles and chains."""
    return _info_measurement_bbox(cutout)


def _jd_product_shape_profile(
    body_width: int,
    body_height: int,
    physical_ratio: float | None = None,
) -> tuple[str, float, float, float]:
    """Return stable template limits for tall, balanced, and wide handbag bodies."""
    visual_ratio = body_width / max(1, body_height)
    ratio = visual_ratio
    if physical_ratio is not None and 0.2 <= physical_ratio <= 5.0:
        ratio = visual_ratio * 0.20 + physical_ratio * 0.80
    if ratio < 0.55:
        return "very_tall", 0.25, 0.44, 0.24
    if ratio < 0.78:
        return "tall", 0.30, 0.42, 0.29
    if ratio > 2.0:
        return "very_wide", 0.43, 0.26, 0.39
    if ratio > 1.35:
        return "wide", 0.40, 0.31, 0.36
    return "balanced", 0.35, 0.37, 0.34


def _jd_size_product_layout(
    cutout: Image.Image,
    body_box: tuple[int, int, int, int],
    size: tuple[int, int],
    product_info: dict[str, str],
    adjustment: dict[str, Any] | None,
    *,
    enforce_logo_clearance: bool = True,
    clamp_to_safe: bool = True,
) -> dict[str, Any]:
    """Compute one immutable baseline transform, then apply user zoom and movement."""
    width, height = size
    normalized = _normalize_adjustment(adjustment)
    body_left, body_top, body_right, body_bottom = body_box
    body_width = max(1, body_right - body_left)
    body_height = max(1, body_bottom - body_top)
    length_mm = _dimension_value_mm(product_info.get("product_length", "")) or 200.0
    height_mm = _dimension_value_mm(product_info.get("product_height", ""))
    if height_mm is None:
        height_mm = max(60.0, length_mm * body_height / body_width)
    physical_ratio = length_mm / max(1.0, height_mm)
    shape, max_width_ratio, max_height_ratio, preferred_width_ratio = _jd_product_shape_profile(
        body_width,
        body_height,
        physical_ratio,
    )

    preferred_body_width = width * preferred_width_ratio * max(0.82, min(1.08, length_mm / 205.0))
    base_scale = min(
        width * max_width_ratio / body_width,
        height * max_height_ratio / body_height,
        width * 0.46 / max(1, cutout.width),
        height * 0.60 / max(1, cutout.height),
        preferred_body_width / body_width,
    )
    scale = base_scale * normalized["zoom"]
    rendered_width = max(1, round(cutout.width * scale))
    rendered_height = max(1, round(cutout.height * scale))
    scaled_body = (
        round(body_left * scale),
        round(body_top * scale),
        round(body_right * scale),
        round(body_bottom * scale),
    )

    desired_body_center_x = width * 0.34 + normalized["offset_x"] * width * 0.18
    desired_body_bottom = height * (0.70 if height > width else 0.73) + normalized["offset_y"] * height * 0.18
    paste_x = round(desired_body_center_x - (scaled_body[0] + scaled_body[2]) / 2)
    paste_y = round(desired_body_bottom - scaled_body[3])
    safe_left = round(width * 0.04)
    safe_top = round(height * 0.04)
    safe_right = round(width * 0.96)
    safe_bottom = round(height * 0.96)

    def clamp_origin(position: int, layer_size: int, minimum: int, maximum: int) -> int:
        if layer_size <= maximum - minimum:
            return min(max(minimum, position), maximum - layer_size)
        return min(max(maximum - layer_size, position), minimum)

    if clamp_to_safe:
        paste_x = clamp_origin(paste_x, rendered_width, safe_left, safe_right)
    effective_safe_top = safe_top
    if enforce_logo_clearance:
        logo_left, logo_top = ((32, 38) if size == (800, 800) else (56, 45))
        logo_right = logo_left + 190
        logo_bottom = logo_top + 60
        horizontal_gap = round(width * 0.02)
        overlaps_logo_columns = (
            paste_x < logo_right + horizontal_gap
            and paste_x + rendered_width > logo_left - horizontal_gap
        )
        if overlaps_logo_columns:
            body_ratio = body_width / max(1, body_height)
            is_tall_handle_bag = body_ratio <= 1.15 and _handle_visual_lift(cutout) >= 0.55
            if size == (800, 800):
                clearance = 97 if is_tall_handle_bag else round(height * 0.09)
            else:
                clearance = round(height * (0.07 if is_tall_handle_bag else 0.04))
            effective_safe_top = max(effective_safe_top, logo_bottom + clearance)
    if clamp_to_safe:
        paste_y = clamp_origin(paste_y, rendered_height, effective_safe_top, safe_bottom)
    rendered_body = (
        paste_x + scaled_body[0],
        paste_y + scaled_body[1],
        paste_x + scaled_body[2],
        paste_y + scaled_body[3],
    )
    return {
        "shape": shape,
        "base_scale": base_scale,
        "base_body_height": body_height * base_scale,
        "scale": scale,
        "paste_x": paste_x,
        "paste_y": paste_y,
        "rendered_width": rendered_width,
        "rendered_height": rendered_height,
        "body_box": rendered_body,
        "height_mm": height_mm,
        "safe_box": (safe_left, safe_top, safe_right, safe_bottom),
    }


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
    color = JD_MEASURE_COLOR
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
    center_x = round(center_x)
    top = round(top)
    height = round(height)
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


def _jd_aligned_phone_top(
    body_box: tuple[int, int, int, int],
    phone_height: int,
    alignment: str,
) -> int:
    """Align the phone against the stable automatic bag-body baseline."""
    body_top = body_box[1]
    body_bottom = body_box[3]
    if alignment == "bottom":
        return round(body_bottom - phone_height)
    return round((body_top + body_bottom - phone_height) / 2)


def _jd_comparison_product_layout(
    cutout: Image.Image,
    body_bbox: tuple[int, int, int, int],
    size: tuple[int, int],
    product_info: dict[str, str],
    adjustment: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep every manual transform anchored to the automatic JD5 layout."""
    base_layout = _jd_size_product_layout(cutout, body_bbox, size, product_info, None)
    if not _has_manual_layout_adjustment(adjustment):
        return base_layout, base_layout

    layout = _jd_size_product_layout(
        cutout,
        body_bbox,
        size,
        product_info,
        adjustment,
        enforce_logo_clearance=False,
        clamp_to_safe=False,
    )
    width, height = size
    base_body = base_layout["body_box"]
    baseline_shift_x = round((base_body[0] + base_body[2]) / 2 - width * 0.34)
    baseline_shift_y = round(base_body[3] - height * (0.70 if height > width else 0.73))
    shifted_body = tuple(
        value + (baseline_shift_x if index % 2 == 0 else baseline_shift_y)
        for index, value in enumerate(layout["body_box"])
    )
    return {
        **layout,
        "paste_x": layout["paste_x"] + baseline_shift_x,
        "paste_y": layout["paste_y"] + baseline_shift_y,
        "body_box": shifted_body,
    }, base_layout


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
    body_bbox = _jd_product_body_bbox(cutout)
    layout, base_layout = _jd_comparison_product_layout(
        cutout,
        body_bbox,
        size,
        product_info,
        adjustment,
    )
    resized_width = layout["rendered_width"]
    resized_height = layout["rendered_height"]
    cutout = cutout.resize((resized_width, resized_height), Image.Resampling.LANCZOS)
    paste_x = layout["paste_x"]
    paste_y = layout["paste_y"]
    safe_left, safe_top, safe_right, safe_bottom = layout["safe_box"]
    canvas.paste(cutout, (paste_x, paste_y), cutout)
    rendered_body = layout["body_box"]
    draw = ImageDraw.Draw(canvas)
    rendered_pixels_per_mm = layout["base_body_height"] / max(1.0, layout["height_mm"])
    base_phone_height = round(JD_PHONE_HEIGHT_MM * rendered_pixels_per_mm)
    base_phone_height = max(round(height * 0.095), min(round(height * 0.46), base_phone_height))
    phone_height = round(JD_PHONE_HEIGHT_MM * rendered_pixels_per_mm * normalized["phone_scale"])
    phone_height = max(round(height * 0.095), min(round(height * 0.46), phone_height))
    reference = _jd_phone_reference_layer()
    reference_ratio = reference.width / reference.height if reference is not None else 0.83
    base_phone_width = max(42, round(base_phone_height * reference_ratio))
    phone_width = max(42, round(phone_height * reference_ratio))
    phone_ruler_gap = max(22, round(width * 0.035))
    phone_label_clearance = max(40, round(width * 0.05))
    phone_right_allowance = phone_ruler_gap + phone_label_clearance
    phone_bottom_allowance = max(28, round(height * 0.055))
    base_phone_left = round(width * 0.75 - base_phone_width / 2)
    base_phone_top = _jd_aligned_phone_top(
        base_layout["body_box"],
        base_phone_height,
        normalized["phone_alignment"],
    )
    base_phone_left = min(
        max(safe_left, base_phone_left),
        max(safe_left, safe_right - base_phone_width - phone_right_allowance),
    )
    base_phone_top = min(
        max(safe_top, base_phone_top),
        max(safe_top, safe_bottom - base_phone_height - phone_bottom_allowance),
    )
    # Scale around the already-clamped automatic phone anchor. Previously the
    # 100% phone used a clamped position while 102% switched to an unclamped
    # position, causing a visible jump on the first zoom step.
    base_phone_center_x = base_phone_left + base_phone_width / 2
    phone_left = round(
        base_phone_center_x
        + normalized["phone_offset_x"] * width * 0.18
        - phone_width / 2
    )
    if normalized["phone_alignment"] == "bottom":
        base_phone_anchor_y = base_phone_top + base_phone_height
        phone_top = round(
            base_phone_anchor_y
            + normalized["phone_offset_y"] * height * 0.18
            - phone_height
        )
    else:
        base_phone_anchor_y = base_phone_top + base_phone_height / 2
        phone_top = round(
            base_phone_anchor_y
            + normalized["phone_offset_y"] * height * 0.18
            - phone_height / 2
        )
    phone_center_x = phone_left + phone_width / 2
    phone_box = _draw_jd_phone_reference(
        canvas,
        round(phone_center_x),
        phone_top,
        phone_height,
    )

    ruler_gap = max(28, round(width * 0.045))
    product_ruler_body = _stored_product_ruler_base(normalized) or rendered_body
    horizontal_y = min(height - 70, product_ruler_body[3] + ruler_gap)
    product_ruler_center = (
        (product_ruler_body[0] + product_ruler_body[2]) / 2,
        product_ruler_body[3],
    )
    length_start, length_end = _transform_product_ruler_segment(
        (product_ruler_body[0], horizontal_y),
        (product_ruler_body[2], horizontal_y),
        product_ruler_center,
        normalized,
        scale=normalized["length_ruler_scale"],
        offset_x=normalized["length_ruler_offset_x"],
        offset_y=normalized["length_ruler_offset_y"],
        canvas_size=size,
    )
    _draw_jd_dimension_bar(
        canvas,
        length_start,
        length_end,
        _dimension_mm(product_info.get("product_length", "")),
    )
    vertical_x = max(30, product_ruler_body[0] - ruler_gap)
    height_start, height_end = _transform_product_ruler_segment(
        (vertical_x, product_ruler_body[1]),
        (vertical_x, product_ruler_body[3]),
        product_ruler_center,
        normalized,
        scale=normalized["height_ruler_scale"],
        offset_x=normalized["height_ruler_offset_x"],
        offset_y=normalized["height_ruler_offset_y"],
        canvas_size=size,
    )
    _draw_jd_dimension_bar(
        canvas,
        height_start,
        height_end,
        _dimension_mm(product_info.get("product_height", "")),
        vertical=True,
    )

    phone_is_at_baseline = (
        abs(normalized["phone_scale"] - 1.0) <= 0.0001
        and abs(normalized["phone_offset_x"]) <= 0.0001
        and abs(normalized["phone_offset_y"]) <= 0.0001
    )
    if normalized["phone_show_ruler"] or phone_is_at_baseline:
        phone_ruler_box = phone_box
    else:
        phone_ruler_box = (
            base_phone_left,
            base_phone_top,
            base_phone_left + base_phone_width,
            base_phone_top + base_phone_height,
        )

    phone_ruler_x = min(safe_right - 12, phone_ruler_box[2] + phone_ruler_gap)
    phone_ruler_start, phone_ruler_end = _transform_ruler_segment(
        (phone_ruler_x, phone_ruler_box[1]),
        (phone_ruler_x, phone_ruler_box[3]),
        scale=normalized["phone_ruler_scale"],
        offset_x=normalized["phone_ruler_offset_x"],
        offset_y=normalized["phone_ruler_offset_y"],
        canvas_size=size,
    )
    _draw_jd_dimension_bar(
        canvas,
        phone_ruler_start,
        phone_ruler_end,
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
        fill=JD_MEASURE_COLOR,
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
    if file_name == "3.jpg":
        canvas = Image.new("RGB", size, "white")
        _paste_layer(
            canvas,
            _crop_source(source.convert("RGB"), adjustment),
            (0, 0, *size),
            adjustment,
            mode=_crop_aware_mode(adjustment, "cover"),
            allow_free_position=_has_manual_layout_adjustment(adjustment),
        )
        _draw_jd_elle_logo(canvas, size, logo_color)
        return canvas
    if file_name == "4.jpg":
        return _jd_interior_detail_page(source, size, adjustment, logo_color)
    if file_name == "5.jpg":
        if not _jd_size_dimensions_ready(product_info):
            return None
        return _jd_size_comparison_page(source, size, product_info, adjustment, logo_color)
    if file_name == "透明.png":
        return _normalized_product_page(source, transparent=True, adjustment=adjustment, manual_padding_ratio=0.18)
    return None


def _slot_map(slots: list[dict[str, Any]], platform: str = "vip") -> dict[str, dict[str, Any]]:
    slot_map = {
        item["file_name"]: {
            "image_ids": [int(value) for value in item.get("image_ids", [])],
            "adjustments": [
                _normalize_adjustment(value if isinstance(value, dict) else None)
                for value in item.get("adjustments", [])
            ],
            "folder_adjustments": {
                str(folder): [
                    _normalize_adjustment(value if isinstance(value, dict) else None)
                    for value in values
                ]
                for folder, values in (
                    item.get("folder_adjustments", {}).items()
                    if isinstance(item.get("folder_adjustments"), dict)
                    else []
                )
                if folder in {"800", "750"} and isinstance(values, list)
            },
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


def _slot_adjustments_for_folder(slot: dict[str, Any], target_folder: str) -> list[dict[str, Any]]:
    folder_adjustments = slot.get("folder_adjustments")
    if not isinstance(folder_adjustments, dict):
        return slot.get("adjustments", [])
    return folder_adjustments.get(target_folder, slot.get("adjustments", []))


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
        if not _vip_info_ready(product_info):
            return None
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
        return _normalized_product_page(
            source,
            transparent=True,
            adjustment=adjustment,
            auto_handle_layout=True,
            auto_tall_handle_drop=True,
            auto_offset_y=-0.03,
            manual_padding_ratio=0.18,
        )
    if file_name == "50.jpg":
        canvas = Image.new("RGB", (950, 1200), "white")
        _paste_layer(canvas, _crop_source(source.convert("RGB"), adjustment), (0, 0, 950, 1200), adjustment, mode=_crop_aware_mode(adjustment, "cover"))
        return canvas
    if file_name == "4.jpg":
        canvas = Image.new("RGB", (800, 800), "white")
        _paste_layer(
            canvas,
            _crop_source(source.convert("RGB"), adjustment),
            (0, 0, 800, 800),
            adjustment,
            mode=_crop_aware_mode(adjustment, "cover"),
        )
        return canvas
    if file_name == "15.jpg":
        canvas = Image.new("RGB", (800, 800), "white")
        _paste_detail_layer(
            canvas,
            source,
            (0, 0, 800, 800),
            adjustment,
            auto_zoom=0.9,
            auto_offset_y=0.0,
            default_mode="contain",
        )
        return canvas
    if file_name == "2.jpg":
        return _catalog_product_page(source, adjustment, tall_handle_drop_ratio=0.14)
    if file_name in {"601.jpg", "602.jpg", "603.jpg"}:
        return _model_showcase_page(source, adjustment)
    if file_name in {"604.jpg", "605.jpg"}:
        return _detail_showcase_page(source, adjustment)
    if file_name == "801.jpg":
        return _tag_certificate_page(source, adjustment)
    return _catalog_product_page(source, adjustment)


def _save_slot_image(image: Image.Image, file_name: str, output: Path) -> None:
    if file_name.endswith(".png"):
        _save_bounded_png(image, output)
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
    return run_heavy_task(
        "backend.services.organizer_render_worker",
        {
            "operation": "preview",
            "session_id": session_id,
            "slots": slots,
            "product_info": product_info,
            "platform": platform,
            "target_folder": target_folder,
        },
        timeout=600,
    )


def render_slot_preview(
    session_id: str,
    slots: list[dict[str, Any]],
    product_info: dict[str, str],
    file_name: str,
    platform: str = "vip",
    target_folder: str = "800",
) -> dict[str, str]:
    return run_heavy_task(
        "backend.services.organizer_render_worker",
        {
            "operation": "preview_slot",
            "session_id": session_id,
            "slots": slots,
            "product_info": product_info,
            "file_name": file_name,
            "platform": platform,
            "target_folder": target_folder,
        },
        timeout=600,
    )


def _render_slot_preview(
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
    slot = {
        **slot,
        "adjustments": _slot_adjustments_for_folder(slot, target_folder),
    }
    if platform == "jd" and target_folder not in {"800", "750"}:
        raise ValueError("京东预览目录必须是 800 或 750")
    if platform == "jd" and file_name == "5.jpg" and not _jd_size_dimensions_ready(product_info):
        raise ValueError("请先填写商品长和高，再生成尺寸与手机对比图")
    if platform == "vip" and file_name == "401.jpg" and not _vip_info_ready(product_info):
        raise ValueError("请先填写商品长和高，再生成产品信息图")
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
        slot = {
            **slot,
            "adjustments": _slot_adjustments_for_folder(slot, target_folder),
        }
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
    return run_heavy_task(
        "backend.services.organizer_render_worker",
        {
            "operation": "export",
            "session_id": session_id,
            "slots": slots,
            "product_info": product_info,
            "platform": platform,
        },
        timeout=900,
    )


def _export_package(
    session_id: str,
    slots: list[dict[str, Any]],
    product_info: dict[str, str],
    platform: str = "vip",
) -> dict[str, Any]:
    slot_definitions = _platform_slot_definitions(platform)
    slot_map = _slot_map(slots, platform)
    _validate_slot_map(session_id, slot_map, platform)
    if platform == "jd" and not _jd_size_dimensions_ready(product_info):
        raise ValueError("请先填写商品长和高，再下载京东套图")
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
                target_adjustments = _slot_adjustments_for_folder(slot, target)
                image = _render_slot_image(
                    file_name,
                    slot["image_ids"],
                    product_info,
                    target_adjustments,
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
