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
_FAST_SLOT_PREVIEW_LOCK = Lock()
PREVIEW_RENDER_VERSION = 31
MAX_PREVIEW_CACHE_ENTRIES = 48
JD_PHONE_HEIGHT_MM = 163.0
JD_PHONE_LABEL = "iPhone 17 Pro Max"
JD_MEASURE_COLOR = "#707070"
JD_PHONE_ASPECT_RATIO = 553 / 710


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
INFO_PRODUCT_SCALE = 1.0
INFO_PRODUCT_HANDLE_SCALE = 0.08
INFO_PRODUCT_HANDLE_LIFT_Y = 0.04
INFO_PRODUCT_NO_HANDLE_DROP_Y = 0.028
INFO_PRODUCT_WIDTH_EDGE_SAFE_RIGHT = 714
INFO_PRODUCT_WIDTH_RULER_ALLOWANCE = 84
INFO_PRODUCT_WIDTH_EDGE_RANGE = 36
INFO_PRODUCT_WIDTH_EDGE_MAX_SHRINK = 0.08
INFO_PRODUCT_WIDTH_EDGE_MAX_SHIFT_X = 16
INFO_TEXT_X = 53
INFO_HEIGHT_RULER_SHIFT_Y = 5
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


def asset_organizer_layer(
    image_id: int,
    adjustment: dict[str, Any] | None = None,
) -> Path:
    """Return the exact product layer shared by the editor and renderer."""
    source_path = asset_original(image_id)
    modified_ns = source_path.stat().st_mtime_ns
    crop_key = _crop_cache_key(adjustment)
    cache_path = _organizer_layer_cache_path(image_id, modified_ns, crop_key)
    if not cache_path.is_file():
        run_heavy_task(
            "backend.services.organizer_render_worker",
            {
                "operation": "organizer_layer",
                "image_id": image_id,
                "adjustment": _normalize_adjustment(adjustment),
            },
            timeout=180,
        )
    if not cache_path.is_file():
        raise ValueError("商品编辑层生成失败")
    return cache_path


def asset_organizer_layer_info(
    image_id: int,
    adjustment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_adjustment(adjustment)
    path = asset_organizer_layer(image_id, adjustment)
    with Image.open(path) as cached:
        layer = cached.convert("RGBA")
    measurement = _info_measurement_bbox(layer)
    product_body = _jd_product_body_bbox(layer)
    return {
        "url": (
            f"/api/vip-organizer/assets/{image_id}/organizer-layer"
            f"?crop_x={normalized['crop_x']:.6f}"
            f"&crop_y={normalized['crop_y']:.6f}"
            f"&crop_width={normalized['crop_width']:.6f}"
            f"&crop_height={normalized['crop_height']:.6f}"
        ),
        "width": layer.width,
        "height": layer.height,
        "measurement_bbox": list(measurement),
        "product_body_bbox": list(product_body),
        "handle_lift": _handle_visual_lift(layer),
    }


def _render_organizer_layer_cache(
    image_id: int,
    adjustment: dict[str, Any] | None = None,
) -> str:
    source_path = asset_original(image_id)
    modified_ns = source_path.stat().st_mtime_ns
    crop_key = _crop_cache_key(adjustment)
    _cached_product_cutout(image_id, modified_ns, crop_key)
    return str(_organizer_layer_cache_path(image_id, modified_ns, crop_key))


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


def _rgb_on_white(image: Image.Image) -> Image.Image:
    """Flatten organizer source transparency onto white, never implicit black."""
    rgba = ImageOps.exif_transpose(image).convert("RGBA")
    flattened = Image.new("RGB", rgba.size, "white")
    flattened.paste(rgba.convert("RGB"), (0, 0), rgba.getchannel("A"))
    return flattened


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


def _model_dominant_pale_silver_alpha(
    model_matte: np.ndarray,
    *,
    connected_background: np.ndarray,
    lab_distance: np.ndarray,
    saturation: np.ndarray,
    value: np.ndarray,
    current_alpha: np.ndarray,
    strict_hardware: np.ndarray,
) -> np.ndarray:
    """Build a white-studio matte for pale bags with repeated silver chain.

    The model is the more reliable body prior for this narrow case. Studio
    white still wins where it is connected to the canvas and model confidence
    is not decisive, which opens chain-link gaps without cutting white leather.
    """
    shape = model_matte.shape
    if any(
        array.shape != shape
        for array in (
            connected_background,
            lab_distance,
            saturation,
            value,
            current_alpha,
            strict_hardware,
        )
    ):
        return current_alpha
    model = np.clip(model_matte.astype(np.float32), 0.0, 1.0)
    confidence = np.clip((model - 0.55) / (0.98 - 0.55), 0.0, 1.0)
    confidence = cv2.GaussianBlur(confidence, (0, 0), 0.45)
    locked_studio_white = (
        connected_background
        & (lab_distance <= 14)
        & (saturation <= 22)
        & (value >= 225)
        & (model < 0.94)
    )
    confidence[locked_studio_white] = 0.0
    confidence[model >= 0.97] = 1.0
    alpha = np.round(confidence * 255.0).astype(np.uint8)
    alpha[strict_hardware] = np.maximum(
        alpha[strict_hardware],
        current_alpha[strict_hardware],
    )
    return _clear_shallow_model_body_fringe(alpha)


def _clear_shallow_model_body_fringe(alpha: np.ndarray) -> np.ndarray:
    """Clear short white feather shelves above a broad model-supported body."""
    if alpha.ndim != 2 or min(alpha.shape) < 24:
        return alpha
    strong = alpha >= 220
    row_widths = np.count_nonzero(strong, axis=1)
    widest = int(np.max(row_widths))
    if widest < max(24, round(alpha.shape[1] * 0.20)):
        return alpha
    broad_rows = np.flatnonzero(row_widths >= round(widest * 0.70))
    if not broad_rows.size:
        return alpha
    body_top = int(broad_rows[0])
    sample_bottom = min(alpha.shape[0], body_top + max(6, round(alpha.shape[0] * 0.03)))
    sample_rows = np.arange(body_top, sample_bottom)
    if not sample_rows.size:
        return alpha
    widest_sample_row = int(sample_rows[np.argmax(row_widths[sample_rows])])
    body_columns = np.flatnonzero(strong[widest_sample_row])
    if not body_columns.size:
        return alpha
    body_left = int(body_columns.min())
    body_right = int(body_columns.max())
    clearance = max(5, round(alpha.shape[0] * 0.010))
    row_grid = np.arange(alpha.shape[0])[:, None]
    column_grid = np.arange(alpha.shape[1])[None, :]
    remote_upper_structure = (
        (alpha >= 24)
        & (row_grid <= body_top - clearance)
    )
    upper_keep = cv2.dilate(
        remote_upper_structure.astype(np.uint8),
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=clearance + 1,
    ).astype(bool)
    shallow_fringe = (
        (alpha > 0)
        & (row_grid < body_top)
        & (column_grid >= body_left)
        & (column_grid <= body_right)
        & ~upper_keep
    )
    cleaned = alpha.copy()
    cleaned[shallow_fringe] = 0
    return cleaned


def _decontaminate_pale_studio_edge(
    rgb: np.ndarray,
    alpha: np.ndarray,
    *,
    lab_distance: np.ndarray,
    saturation: np.ndarray,
    value: np.ndarray,
    hardware: np.ndarray,
) -> np.ndarray:
    """Reduce white-studio colour mixed into the edge of pale products.

    Pale leather cannot use the normal colour-key alpha solve: its real pixels
    are close to the white backdrop.  Keep alpha unchanged and borrow colour
    only from a nearby, opaque pale-material seed when an outer edge pixel is
    measurably closer to the backdrop and brighter than that seed.
    """
    if rgb.ndim != 3 or rgb.shape[:2] != alpha.shape:
        return rgb
    foreground = alpha > 8
    if not np.any(foreground):
        return rgb
    inside = cv2.distanceTransform(
        foreground.astype(np.uint8),
        cv2.DIST_L2,
        3,
    )
    boundary = foreground & (inside <= 4.5) & ~hardware
    seed = (
        (alpha >= 245)
        & (inside >= 5.0)
        & (saturation <= 42)
        & (value >= 135)
        & (lab_distance >= 4)
        & ~hardware
    )
    if not np.any(boundary) or not np.any(seed):
        return rgb
    _, nearest_labels = cv2.distanceTransformWithLabels(
        (~seed).astype(np.uint8),
        cv2.DIST_L2,
        5,
        labelType=cv2.DIST_LABEL_PIXEL,
    )
    seed_colours = rgb[seed].astype(np.float32)
    nearest_indices = np.clip(
        nearest_labels.astype(np.int64) - 1,
        0,
        len(seed_colours) - 1,
    )
    nearest_colours = seed_colours[nearest_indices]
    nearest_value = nearest_colours.max(axis=2)
    current_value = rgb.max(axis=2).astype(np.float32)
    contaminated = (
        boundary
        & (saturation <= 38)
        & (value >= 214)
        & (lab_distance <= 42)
        & (current_value >= nearest_value + 3.0)
    )
    if not np.any(contaminated):
        return rgb
    strength = np.clip(
        (current_value - nearest_value - 2.0) / 18.0,
        0.25,
        0.82,
    )[:, :, None]
    result = rgb.astype(np.float32)
    result[contaminated] = (
        result[contaminated]
        * (1.0 - strength[contaminated])
        + nearest_colours[contaminated] * strength[contaminated]
    )
    return np.clip(result, 0, 255).astype(np.uint8)


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


def _audit_bottom_contour_candidate(
    alpha: np.ndarray,
    rgb: np.ndarray,
    model_matte: np.ndarray,
    protected_detail: np.ndarray,
    *,
    confidence_floor: float,
    contour_mode: str,
) -> np.ndarray:
    """Accept a contour cleanup only when it is shallow and shadow-dominant."""
    candidate = _clean_bottom_against_body_contour(
        alpha,
        rgb,
        model_matte,
        protected_detail,
        confidence_floor=confidence_floor,
        contour_mode=contour_mode,
    )
    removed = (alpha > 8) & (candidate <= 8)
    removed_count = int(np.count_nonzero(removed))
    if removed_count == 0:
        return alpha

    foreground_count = int(np.count_nonzero(alpha > 8))
    height, _ = alpha.shape
    removed_rows = np.flatnonzero(np.any(removed, axis=1))
    if (
        removed_count > max(80, round(foreground_count * 0.018))
        or not removed_rows.size
        or int(removed_rows.min()) < round(height * 0.72)
        or int(removed_rows.max() - removed_rows.min() + 1)
        > max(8, round(height * 0.065))
    ):
        return alpha

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    strongly_material_like = (
        (model_matte >= 0.94)
        & (hsv[:, :, 1] >= 32)
    )
    material_fraction = float(np.mean(strongly_material_like[removed]))
    if material_fraction > 0.30:
        return alpha
    return candidate


def _audit_white_studio_residue(
    alpha: np.ndarray,
    *,
    lab_distance: np.ndarray,
    saturation: np.ndarray,
    value: np.ndarray,
    source_min: np.ndarray,
    source_spread: np.ndarray,
    model_matte: np.ndarray,
    tight_hardware: np.ndarray,
    pale_body_protection: np.ndarray,
    colored_material: np.ndarray,
    compact_detail: np.ndarray,
    verified_floor_hardware: np.ndarray,
) -> np.ndarray:
    """Conditionally clear residue after the conservative white-studio pass.

    This is deliberately an audit rather than another full cutout pass. It
    reclassifies only small unknown regions beside confirmed hardware and
    shallow components touching the lower transparent boundary. Every region
    has a deletion budget; an unexpectedly large change is rolled back.
    """
    if alpha.size == 0 or not np.any(alpha > 8):
        return alpha

    audited = alpha.copy()
    height, width = audited.shape
    yy, xx = np.indices(audited.shape)
    foreground_count = int(np.count_nonzero(audited > 8))
    white_like = (
        (
            (lab_distance <= 28)
            & (saturation <= 30)
            & (value >= 212)
        )
        | (
            (source_min >= 226)
            & (source_spread <= 24)
        )
    )
    central_foreground = (
        (audited > 200)
        & (xx >= round(width * 0.20))
        & (xx < round(width * 0.80))
        & (yy >= round(height * 0.20))
        & (yy < round(height * 0.78))
    )
    foreground_value = (
        float(np.median(value[central_foreground]))
        if np.any(central_foreground)
        else 180.0
    )
    relative_bright_neutral = (
        (saturation <= 34)
        & (value >= max(92.0, foreground_value + 42.0))
        & (model_matte < 0.88)
    )
    protected_material = (
        tight_hardware
        | pale_body_protection
        | colored_material
        | verified_floor_hardware
    )

    # First audit the white pockets immediately around confirmed metal. The
    # undilated hardware seed itself can never be removed. Isolated pockets are
    # accepted only when they are small relative to the verified metal area.
    if np.any(tight_hardware):
        hardware_shell = cv2.dilate(
            tight_hardware.astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)),
            iterations=1,
        ).astype(bool) & ~tight_hardware
        hardware_unknown = (
            (audited > 8)
            & hardware_shell
            & (white_like | relative_bright_neutral)
            & ~pale_body_protection
            & ~colored_material
            & ~verified_floor_hardware
        )
        transparent_neighbour = cv2.dilate(
            (audited <= 8).astype(np.uint8),
            np.ones((3, 3), dtype=np.uint8),
            iterations=1,
        ).astype(bool)
        hardware_count, hardware_labels, hardware_stats, _ = (
            cv2.connectedComponentsWithStats(
                hardware_unknown.astype(np.uint8),
                8,
            )
        )
        hardware_remove = np.zeros_like(hardware_unknown)
        maximum_pocket_area = max(
            20,
            min(
                round(foreground_count * 0.0025),
                round(np.count_nonzero(tight_hardware) * 0.55),
            ),
        )
        for component in range(1, hardware_count):
            component_pixels = hardware_labels == component
            component_area = int(hardware_stats[component, cv2.CC_STAT_AREA])
            touches_background = bool(
                np.any(component_pixels & transparent_neighbour)
            )
            if component_area <= maximum_pocket_area and (
                touches_background
                or float(np.mean(white_like[component_pixels])) >= 0.92
                or float(
                    np.mean(relative_bright_neutral[component_pixels])
                ) >= 0.85
            ):
                hardware_remove |= component_pixels

        hardware_budget = max(
            24,
            min(
                round(foreground_count * 0.004),
                round(np.count_nonzero(tight_hardware) * 1.2),
            ),
        )
        if 0 < int(np.count_nonzero(hardware_remove)) <= hardware_budget:
            audited[hardware_remove] = 0

    # Then inspect only the lower boundary. Real bag material usually forms a
    # broad coherent body, while studio shadow/residue is a shallow component
    # reached from the already transparent floor. Compact detail is protected
    # when it has real colour/contrast; a grey overconfident shadow is not.
    transparent_neighbour = cv2.dilate(
        (audited <= 8).astype(np.uint8),
        np.ones((5, 5), dtype=np.uint8),
        iterations=1,
    ).astype(bool)
    compact_material = compact_detail & (
        (saturation >= 20)
        | (lab_distance >= 18)
    )
    bottom_unknown = (
        (yy >= round(height * 0.86))
        & (xx >= round(width * 0.06))
        & (xx < round(width * 0.94))
        & (audited > 8)
        & transparent_neighbour
        & (
            (model_matte < 0.82)
            | white_like
            | (
                (saturation <= 34)
                & (value <= 135)
            )
        )
        & ~protected_material
        & ~compact_material
    )
    bottom_count, bottom_labels, bottom_stats, _ = (
        cv2.connectedComponentsWithStats(
            bottom_unknown.astype(np.uint8),
            8,
        )
    )
    bottom_remove = np.zeros_like(bottom_unknown)
    maximum_height = max(5, round(height * 0.055))
    minimum_width = max(4, round(width * 0.012))
    for component in range(1, bottom_count):
        component_pixels = bottom_labels == component
        component_width = int(bottom_stats[component, cv2.CC_STAT_WIDTH])
        component_height = int(bottom_stats[component, cv2.CC_STAT_HEIGHT])
        component_area = int(bottom_stats[component, cv2.CC_STAT_AREA])
        if component_area == 0:
            continue
        mean_model = float(np.mean(model_matte[component_pixels]))
        white_fraction = float(np.mean(white_like[component_pixels]))
        shallow = (
            component_height <= maximum_height
            and component_width >= minimum_width
            and component_width >= component_height * 1.35
        )
        if shallow and (mean_model < 0.80 or white_fraction >= 0.72):
            bottom_remove |= component_pixels

    bottom_budget = max(48, round(foreground_count * 0.012))
    if 0 < int(np.count_nonzero(bottom_remove)) <= bottom_budget:
        audited[bottom_remove] = 0
    return audited


def _restore_compact_source_silver(
    alpha: np.ndarray,
    *,
    saturation: np.ndarray,
    value: np.ndarray,
    lab_distance: np.ndarray,
    gradient: np.ndarray,
    strict_seed: np.ndarray,
) -> np.ndarray:
    """Restore low-matte silver links without reviving a floor shadow.

    The first pass deliberately trusts the segmentation model.  A thin silver
    chain can nevertheless have a weak matte because most of every link is a
    white reflection.  This audit uses source contrast only after the image
    has already demonstrated a repeated silver signature.  It restores the
    original high-contrast pixels of compact seed-bearing components; broad,
    shallow runs (the geometry of a studio contact shadow) are rejected.
    White holes inside each link are never filled.
    """
    if alpha.shape != strict_seed.shape or not np.any(strict_seed):
        return alpha

    local_value_range = (
        cv2.dilate(value, np.ones((3, 3), dtype=np.uint8))
        - cv2.erode(value, np.ones((3, 3), dtype=np.uint8))
    )
    source_metal = (
        (alpha < 220)
        & (saturation <= 48)
        & (value >= 35)
        & (value <= 245)
        & (lab_distance >= 4.5)
        & (
            (gradient >= 22)
            | (local_value_range >= 17)
        )
    )
    if not np.any(source_metal):
        return alpha

    component_bridge = cv2.morphologyEx(
        source_metal.astype(np.uint8),
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        component_bridge,
        8,
    )
    height, width = alpha.shape
    foreground_count = int(np.count_nonzero(alpha > 8))
    accepted = np.zeros_like(source_metal)
    for component in range(1, component_count):
        component_pixels = labels == component
        original_pixels = component_pixels & source_metal
        area = int(np.count_nonzero(original_pixels))
        if area < 3 or not np.any(original_pixels & strict_seed):
            continue
        component_width = int(stats[component, cv2.CC_STAT_WIDTH])
        component_height = int(stats[component, cv2.CC_STAT_HEIGHT])
        broad_shallow_shadow = (
            component_height <= max(3, round(height * 0.008))
            and component_width >= max(14, component_height * 5)
        )
        implausibly_large = area > max(
            180,
            round(foreground_count * 0.010),
        )
        implausibly_flat = component_width > max(
            20,
            component_height * 14,
        )
        if broad_shallow_shadow or implausibly_large or implausibly_flat:
            continue
        accepted |= original_pixels

    accepted_count = int(np.count_nonzero(accepted))
    restore_budget = max(80, round(foreground_count * 0.018))
    if accepted_count == 0 or accepted_count > restore_budget:
        return alpha

    contrast_alpha = np.clip(
        0.56
        + (lab_distance.astype(np.float32) - 4.5) / 26.0
        + local_value_range.astype(np.float32) / 180.0,
        0.56,
        1.0,
    )
    restored = alpha.copy()
    restored[accepted] = np.maximum(
        restored[accepted],
        np.round(contrast_alpha[accepted] * 255.0).astype(np.uint8),
    )
    return restored


def _prepared_product_cutout(
    source: Image.Image,
    model_matte: np.ndarray | None = None,
    *,
    _bottom_contour_mode: str = "adaptive",
    _bottom_contour_confidence: float = 0.96,
    _use_historical_bottom_pipeline: bool | None = None,
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
    restore_source_silver = False
    strict_hardware_protection = np.zeros((height, width), dtype=bool)
    hardware_protection = np.zeros((height, width), dtype=bool)
    pale_body_protection = np.zeros((height, width), dtype=bool)
    pale_opening_background = np.zeros((height, width), dtype=bool)
    model_compact_detail = np.zeros((height, width), dtype=bool)
    use_historical_bottom_pipeline = bool(
        _use_historical_bottom_pipeline
    )

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
            if _use_historical_bottom_pipeline is None:
                # The earlier GitHub floor continuation is cleaner on
                # coloured and dark products, while the newer protection is
                # safer for pale/white bags. Select between them only after a
                # model-supported material classification; the upper
                # silhouette and hardware path remains shared.
                use_historical_bottom_pipeline = not pale_product

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
                # Pale shoulder bags and luggage can have narrow straps,
                # quilted seams, side gussets, wheels or fittings outside the
                # broad body run.  A white-background flood may reach those
                # parts even though the segmentation model is very certain
                # they are foreground.  Extend protection only where the
                # source still has measurable material evidence; truly white
                # openings and chain gaps therefore remain background.
                pale_detail_protection = (
                    (model_matte >= 0.90)
                    & (
                        (lab_distance >= 4)
                        | (saturation >= 5)
                        | (value <= 249)
                        | (gradient >= 5)
                    )
                    & ~pale_opening_background
                )
                pale_body_protection |= pale_detail_protection
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
            proximity_radius = max(5, min(24, min(height, width) // 45))
            proximity_size = proximity_radius * 2 + 1
            model_proximity = cv2.dilate(
                (model_matte >= 0.10).astype(np.uint8),
                cv2.getStructuringElement(
                    cv2.MORPH_ELLIPSE,
                    (proximity_size, proximity_size),
                ),
                iterations=1,
            ).astype(bool)
            local_value_range = (
                cv2.dilate(value, np.ones((3, 3), dtype=np.uint8))
                - cv2.erode(value, np.ones((3, 3), dtype=np.uint8))
            )
            silver_seed = (
                exterior_neighbour
                & (
                    (model_matte >= 0.55)
                    | (
                        model_proximity
                        &
                        (gradient >= 48)
                        & (local_value_range >= 34)
                    )
                )
                & (saturation <= 40)
                & (value >= 45)
                & (value <= 225)
                & (lab_distance >= 7)
                & (gradient >= 26)
                & (local_value_range >= 20)
            )
            upper_silver_seed = (
                silver_seed
                & (np.arange(height)[:, None] < round(height * 0.72))
            )
            upper_dark_silver = (
                upper_silver_seed
                & (saturation <= 18)
                & (value <= 175)
            )
            # A real silver chain contains many alternating dark neutral rims
            # and bright highlights. Pale leather stitching and floor shadows
            # can create a few neutral edges, but not this repeated metal
            # signature. This image-level gate lets low-model silver cores use
            # source evidence without introducing a product/SKU special case.
            restore_source_silver = bool(
                pale_product
                and int(np.count_nonzero(upper_silver_seed))
                >= max(36, round(height * width * 0.000025))
                and int(np.count_nonzero(upper_dark_silver))
                >= max(
                    18,
                    round(np.count_nonzero(upper_silver_seed) * 0.015),
                )
            )
            if (
                _use_historical_bottom_pipeline is None
                and restore_source_silver
            ):
                # Pale bags normally use the conservative body-contour path.
                # A repeated silver-chain signature is different: side chain
                # shadows sit outside that central contour and are cleaned
                # more reliably by the established material-tail pass. The
                # silver seed itself remains protected below.
                use_historical_bottom_pipeline = True
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
                    & (
                        (model_matte >= 0.40)
                        | (
                            model_proximity
                            &
                            (gradient >= 22)
                            & (local_value_range >= 28)
                        )
                    )
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
            | (pale_product & pale_body_protection)
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
                | (pale_product & pale_body_protection)
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
            model_compact_detail.fill(False)
            if model_matte is not None:
                model_detail = (
                    (model_matte >= 0.82)
                    & (gradient >= 18)
                    & (saturation >= 18)
                    & (value >= 140)
                )
                # Wheels, feet and low-hanging fittings are compact/tall
                # model-supported shapes below the dense body edge.  A floor
                # shadow is instead wide and shallow.  Protect the former
                # independent of product colour so mint luggage and dark
                # rubber wheels do not depend on the pale-bag branch.
                model_lower_tail = (
                    (yy > dense_bottom)
                    & (model_matte >= 0.90)
                ).astype(np.uint8)
                (
                    lower_component_count,
                    lower_component_labels,
                    lower_component_stats,
                    _,
                ) = cv2.connectedComponentsWithStats(model_lower_tail, 8)
                for component in range(1, lower_component_count):
                    component_width = int(
                        lower_component_stats[component, cv2.CC_STAT_WIDTH]
                    )
                    component_height = int(
                        lower_component_stats[component, cv2.CC_STAT_HEIGHT]
                    )
                    component_area = int(
                        lower_component_stats[component, cv2.CC_STAT_AREA]
                    )
                    if (
                        component_width <= max(18, round(width * 0.18))
                        and component_height >= max(
                            7,
                            round(component_width * 0.32),
                        )
                        and component_area >= max(18, width // 25)
                    ):
                        model_compact_detail |= (
                            lower_component_labels == component
                        )
            detail_below_body = (
                gold_detail
                | hardware_protection
                | dark_detail
                | outside_colored_detail
                | central_colored_detail
                | model_detail
                | model_compact_detail
                | (pale_product & pale_body_protection)
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
                    & ~pale_body_protection
                    & ~(
                        gold_detail
                        | hardware_protection
                        | dark_detail
                        | outside_colored_detail
                        | central_colored_detail
                        | model_compact_detail
                    )
                )
                mask_u8[connected_floor_shadow] = 0

                if use_historical_bottom_pipeline:
                    # The first GitHub version followed the real coloured
                    # material down each column before removing a neutral
                    # studio tail.  It was particularly reliable on curved
                    # blue/pink bags, so keep it available as a bottom-only
                    # candidate while the current upper silhouette and
                    # hardware masks remain unchanged.
                    sample_saturation = saturation[
                        sample_top:sample_bottom,
                        sample_left:sample_right,
                    ][sample_mask]
                    if sample_saturation.size:
                        median_saturation = float(np.median(sample_saturation))
                        median_value = (
                            float(np.median(sample_value))
                            if sample_value.size
                            else 180.0
                        )
                        material_saturation = float(np.clip(
                            median_saturation * 0.75,
                            18,
                            50,
                        ))
                        material_value_floor = float(np.clip(
                            median_value * 0.78,
                            90,
                            165,
                        ))
                        column_material = mask_u8.astype(bool) & (
                            (dark_product & (value <= 105))
                            | gold_detail
                        )
                        if median_saturation >= 18:
                            sample_hue = hue[
                                sample_top:sample_bottom,
                                sample_left:sample_right,
                            ][sample_mask]
                            sample_hue = sample_hue[
                                sample_saturation >= material_saturation
                            ]
                            if sample_hue.size:
                                angles = (
                                    sample_hue.astype(np.float32)
                                    * (2 * np.pi / 180.0)
                                )
                                dominant_hue = (
                                    np.arctan2(
                                        np.mean(np.sin(angles)),
                                        np.mean(np.cos(angles)),
                                    )
                                    * 180.0
                                    / (2 * np.pi)
                                ) % 180.0
                                hue_distance = np.abs(
                                    hue.astype(np.float32) - dominant_hue
                                )
                                hue_distance = np.minimum(
                                    hue_distance,
                                    180.0 - hue_distance,
                                )
                                coloured_material = (
                                    (saturation >= material_saturation)
                                    & (hue_distance <= 20)
                                    & (value >= material_value_floor)
                                    & (gradient >= 7)
                                )
                                column_material |= (
                                    mask_u8.astype(bool) & coloured_material
                                )
                        has_column_material = np.any(
                            column_material,
                            axis=0,
                        )
                        column_bottoms = (
                            height
                            - 1
                            - np.argmax(column_material[::-1], axis=0)
                        ).astype(np.int32)
                        column_bottoms[~has_column_material] = -1
                        smoothing_width = max(5, min(31, width // 80))
                        if smoothing_width % 2 == 0:
                            smoothing_width += 1
                        padded_bottoms = np.pad(
                            column_bottoms,
                            smoothing_width // 2,
                            mode="edge",
                        )
                        column_bottoms = np.median(
                            np.lib.stride_tricks.sliding_window_view(
                                padded_bottoms,
                                smoothing_width,
                            ),
                            axis=1,
                        ).astype(np.int32)
                        below_column_material = (
                            (column_bottoms[None, :] >= 0)
                            & (
                                yy
                                > column_bottoms[None, :]
                                + max(1, height // 800)
                            )
                        )
                        neutral_column_tail = (
                            (yy >= floor_start)
                            & below_column_material
                            & mask_u8.astype(bool)
                            & ~(
                                gold_detail
                                | hardware_protection
                                | (dark_product & dark_detail)
                                | outside_colored_detail
                                | model_compact_detail
                            )
                        )
                        mask_u8[neutral_column_tail] = 0

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
    cropped_model_compact_detail = model_compact_detail[
        top:bottom,
        left:right,
    ]
    cropped_hardware = cropped_elle_hardware | cropped_silver_hardware
    cropped_gold_protection = cropped_gold_only_protection | cropped_hardware
    cropped_tight_hardware = cropped_strict_hardware.copy()
    # Dark products intentionally skip the model-assisted coloured-floor
    # branch below. Keep the later detached-component cleanup well-defined for
    # that path; no pixels should receive coloured-material protection unless
    # the model-assisted branch explicitly verifies them.
    cropped_colored_material = np.zeros(alpha.shape, dtype=bool)
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
            & (source_min >= 233)
            & (source_spread <= 20)
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
        if (
            structure_rows.size
            and structure_columns.size
            and not pale_product
            and not use_historical_bottom_pipeline
        ):
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
        if (
            not use_historical_bottom_pipeline
            and model_floor is not None
            and model_floor < cropped_height - 2
        ):
            low_confidence_floor_residue = (
                (yy > model_floor)
                & central_model_columns
                & (alpha > 0)
                & (cropped_model_matte < 0.95)
                & ~cropped_hardware
                & ~cropped_colored_material
                & ~cropped_model_compact_detail
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

        if (
            not use_historical_bottom_pipeline
            and broad_floor is not None
            and broad_floor < cropped_height - 1
        ):
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
                & ~cropped_model_compact_detail
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
                    & ~cropped_model_compact_detail
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
                        & ~cropped_model_compact_detail
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
                (cropped_model_matte < 0.76)
                | (strongly_coloured_product & (cropped_saturation <= 34))
                | (strongly_coloured_product & (refined_value <= 120))
            )
            & ~verified_floor_hardware
            & ~cropped_colored_material
            & ~cropped_model_compact_detail
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

        alpha = _audit_white_studio_residue(
            alpha,
            lab_distance=cropped_lab_distance,
            saturation=cropped_saturation,
            value=cropped_value,
            source_min=source_min,
            source_spread=source_spread,
            model_matte=cropped_model_matte,
            tight_hardware=cropped_tight_hardware,
            pale_body_protection=cropped_pale_body_protection,
            colored_material=cropped_colored_material,
            compact_detail=cropped_model_compact_detail,
            verified_floor_hardware=verified_floor_hardware,
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

    contour_hardware = np.zeros_like(alpha, dtype=bool)
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
        if (
            model_matte is not None
            and not dark_product
            and strongly_coloured_product
        ):
            # A second pass may only remove a shallow, shadow-dominant tail
            # below the colour-continuous body contour.  The audit rolls back
            # broad or material-rich changes, and all verified hardware is
            # excluded.  Upper handles and openings are outside this pass.
            contour_protection = (
                contour_hardware
                | cropped_tight_hardware
                | verified_floor_hardware
                | cropped_model_compact_detail
            )
            alpha = _audit_bottom_contour_candidate(
                alpha,
                np.clip(cropped_rgb, 0, 255).astype(np.uint8),
                cropped_model_matte,
                contour_protection,
                confidence_floor=min(_bottom_contour_confidence, 0.92),
                contour_mode=_bottom_contour_mode,
            )

            # Recover a coloured handle edge only when it is locally attached
            # to the established silhouette and retains real source colour.
            # This compensates for a slightly weak first matte without filling
            # white handle openings or touching the bottom audit region.
            attached_shell = cv2.dilate(
                (alpha > 48).astype(np.uint8),
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
                iterations=1,
            ).astype(bool)
            upper_coloured_edge = (
                attached_shell
                & (yy < round(cropped_height * 0.76))
                & (cropped_model_matte >= 0.82)
                & (cropped_saturation >= 10)
                & (cropped_lab_distance >= 7)
                & (cropped_value <= 248)
                & ~locked_white_background[top:bottom, left:right]
            )
            colour_strength = np.clip(
                (cropped_lab_distance - 7.0) / 22.0,
                0.0,
                1.0,
            )
            recovered_edge_alpha = np.round(
                cropped_model_matte
                * colour_strength
                * 255.0
            ).astype(np.uint8)
            alpha[upper_coloured_edge] = np.maximum(
                alpha[upper_coloured_edge],
                recovered_edge_alpha[upper_coloured_edge],
            )

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

        if pale_product:
            cropped_rgb = _decontaminate_pale_studio_edge(
                np.clip(cropped_rgb, 0, 255).astype(np.uint8),
                alpha,
                lab_distance=cropped_lab_distance,
                saturation=cropped_saturation,
                value=cropped_value,
                hardware=cropped_tight_hardware,
            ).astype(np.float32)

        # A verified silver seed has model support, strong local contrast and
        # is already excluded from the connected white studio background.
        # Keep that metal core opaque after floor/fringe cleanup; only the
        # undilated seed is restored, so neighbouring white pixels stay clear.
        if model_matte is not None and np.any(cropped_strict_silver_hardware):
            silver_seed_alpha = np.round(
                np.clip(model_matte[top:bottom, left:right], 0.0, 1.0) * 255.0
            ).astype(np.uint8)
            if restore_source_silver:
                source_silver_alpha = np.round(
                    np.clip(
                        0.52
                        +
                        (cropped_lab_distance.astype(np.float32) - 7.0)
                        / 34.0,
                        0.52,
                        1.0,
                    )
                    * 255.0
                ).astype(np.uint8)
                silver_seed_alpha[cropped_strict_silver_hardware] = np.maximum(
                    silver_seed_alpha[cropped_strict_silver_hardware],
                    source_silver_alpha[cropped_strict_silver_hardware],
                )
            alpha[cropped_strict_silver_hardware] = np.maximum(
                alpha[cropped_strict_silver_hardware],
                silver_seed_alpha[cropped_strict_silver_hardware],
            )
            if restore_source_silver:
                alpha = _restore_compact_source_silver(
                    alpha,
                    saturation=cropped_saturation,
                    value=cropped_value,
                    lab_distance=cropped_lab_distance,
                    gradient=cropped_gradient,
                    strict_seed=cropped_strict_silver_hardware,
                )

        if model_matte is not None and pale_product and restore_source_silver:
            alpha = _model_dominant_pale_silver_alpha(
                cropped_model_matte,
                connected_background=connected_background[
                    top:bottom,
                    left:right,
                ],
                lab_distance=cropped_lab_distance,
                saturation=cropped_saturation,
                value=cropped_value,
                current_alpha=alpha,
                strict_hardware=cropped_strict_silver_hardware,
            )

    result = Image.fromarray(np.clip(cropped_rgb, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    result.putalpha(Image.fromarray(alpha, "L"))
    return result


def _retain_compact_terminal_hardware(
    seed: np.ndarray,
    *,
    main_width: int,
    main_height: int,
    main_area: int,
) -> np.ndarray:
    """Keep compact metal components while rejecting broad floor reflections."""
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        seed.astype(np.uint8),
        8,
    )
    retained = np.zeros_like(seed, dtype=bool)
    maximum_width = max(12, round(main_width * 0.45))
    maximum_area = max(90, round(main_area * 0.018))
    shallow_height = max(3, round(main_height * 0.012))
    broad_reflection_width = max(16, round(main_width * 0.075))
    for component in range(1, component_count):
        component_width = int(stats[component, cv2.CC_STAT_WIDTH])
        component_height = int(stats[component, cv2.CC_STAT_HEIGHT])
        component_area = int(stats[component, cv2.CC_STAT_AREA])
        broad_shallow_reflection = (
            component_height <= shallow_height
            and component_width >= broad_reflection_width
        )
        if (
            component_width <= maximum_width
            and component_area <= maximum_area
            and not broad_shallow_reflection
        ):
            retained |= labels == component
    return retained


def _restore_overcut_bottom_material(
    rgba: np.ndarray,
    original_alpha: np.ndarray,
    cleaned_alpha: np.ndarray,
) -> bool:
    """Restore a short, colour-continuous product edge removed as floor.

    This is deliberately a validation pass, not a new foreground detector. It
    can only restore pixels which were present before floor cleanup, touch the
    retained central product base, and match the local material immediately
    above them. Once colour or opacity stops matching, the column is closed so
    the recovery cannot continue into the studio shadow.
    """
    # Genuine product material which was cut too high normally kept an opaque
    # model prediction. A confidence-collapse tail is precisely the opposite;
    # never use this pass to undo that lower-confidence evidence.
    removed = (original_alpha >= 230) & (cleaned_alpha <= 8)
    if not np.any(removed):
        return False

    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        (cleaned_alpha > 8).astype(np.uint8),
        8,
    )
    if component_count <= 1:
        return False
    largest_component = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    main_pixels = labels == largest_component
    main_left = int(stats[largest_component, cv2.CC_STAT_LEFT])
    main_top = int(stats[largest_component, cv2.CC_STAT_TOP])
    main_width = int(stats[largest_component, cv2.CC_STAT_WIDTH])
    main_height = int(stats[largest_component, cv2.CC_STAT_HEIGHT])
    if main_width < 20 or main_height < 20:
        return False

    central_left = main_left + round(main_width * 0.10)
    central_right = main_left + main_width - round(main_width * 0.10)
    lower_start = main_top + round(main_height * 0.80)
    maximum_recovery_rows = max(
        3,
        min(7, round(main_height * 0.010)),
    )
    rgb = rgba[:, :, :3]
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).astype(np.float32)
    recovery_candidate = np.zeros_like(removed)

    for column in range(central_left, central_right):
        retained_rows = np.flatnonzero(
            main_pixels[lower_start:main_top + main_height, column]
            & (
                cleaned_alpha[
                    lower_start:main_top + main_height,
                    column,
                ]
                >= 128
            )
        )
        if not retained_rows.size:
            continue
        anchor_row = lower_start + int(retained_rows[-1])
        anchor_top = max(lower_start, anchor_row - 3)
        anchor_left = max(central_left, column - 1)
        anchor_right = min(central_right, column + 2)
        anchor_pixels = (
            main_pixels[
                anchor_top:anchor_row + 1,
                anchor_left:anchor_right,
            ]
            & (
                cleaned_alpha[
                    anchor_top:anchor_row + 1,
                    anchor_left:anchor_right,
                ]
                >= 128
            )
        )
        if not np.any(anchor_pixels):
            continue
        anchor_lab = np.median(
            lab[
                anchor_top:anchor_row + 1,
                anchor_left:anchor_right,
            ][anchor_pixels],
            axis=0,
        )
        anchor_hsv = np.median(
            hsv[
                anchor_top:anchor_row + 1,
                anchor_left:anchor_right,
            ][anchor_pixels],
            axis=0,
        )

        for row in range(
            anchor_row + 1,
            min(
                cleaned_alpha.shape[0],
                anchor_row + maximum_recovery_rows + 1,
            ),
        ):
            if not removed[row, column]:
                break
            colour_distance = float(np.linalg.norm(
                lab[row, column] - anchor_lab
            ))
            value_difference = abs(
                float(hsv[row, column, 2]) - float(anchor_hsv[2])
            )
            saturation_difference = abs(
                float(hsv[row, column, 1]) - float(anchor_hsv[1])
            )
            same_local_material = (
                colour_distance <= 9.0
                and value_difference <= 9.0
                and saturation_difference <= 14.0
            )
            if not same_local_material:
                break
            recovery_candidate[row, column] = True

    if not np.any(recovery_candidate):
        return False

    # A real base/piping edge has horizontal support. Reject isolated matching
    # specks, which are common in a softly coloured studio shadow.
    candidate_count, candidate_labels, candidate_stats, _ = (
        cv2.connectedComponentsWithStats(
            recovery_candidate.astype(np.uint8),
            8,
        )
    )
    supported_recovery = np.zeros_like(recovery_candidate)
    minimum_width = max(8, round(main_width * 0.025))
    retained_neighbourhood = cv2.dilate(
        (cleaned_alpha >= 128).astype(np.uint8),
        np.ones((3, 3), dtype=np.uint8),
        iterations=1,
    ).astype(bool)
    for component in range(1, candidate_count):
        component_pixels = candidate_labels == component
        component_width = int(
            candidate_stats[component, cv2.CC_STAT_WIDTH]
        )
        if (
            component_width >= minimum_width
            and np.any(component_pixels & retained_neighbourhood)
        ):
            supported_recovery |= component_pixels

    if not np.any(supported_recovery):
        return False
    cleaned_alpha[supported_recovery] = original_alpha[supported_recovery]
    return True


def _remove_central_bottom_orphan_tails(
    alpha: np.ndarray,
    *,
    left: int,
    right: int,
    top: int,
    bottom: int,
    protected_hardware: np.ndarray,
    maximum_height: int,
) -> bool:
    """Remove shallow bottom runs separated vertically from the product."""
    candidates = np.zeros_like(alpha, dtype=bool)
    for column in range(max(0, left), min(alpha.shape[1], right)):
        occupied = alpha[top:bottom, column] > 8
        occupied_rows = np.flatnonzero(occupied)
        if occupied_rows.size < 2:
            continue
        first_occupied = int(occupied_rows[0])
        gap_start: int | None = None
        for local_row in range(first_occupied + 1, occupied.size):
            if occupied[local_row]:
                if gap_start is not None:
                    tail_rows = np.flatnonzero(occupied[local_row:])
                    if tail_rows.size:
                        last_tail_row = local_row + int(tail_rows[-1])
                        if last_tail_row - local_row + 1 <= maximum_height:
                            candidates[
                                top + local_row:top + last_tail_row + 1,
                                column,
                            ] |= occupied[local_row:last_tail_row + 1]
                    break
            elif gap_start is None:
                gap_start = local_row

    if not np.any(candidates):
        return False
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        candidates.astype(np.uint8),
        8,
    )
    removable = np.zeros_like(candidates)
    for component in range(1, component_count):
        component_pixels = labels == component
        component_height = int(stats[component, cv2.CC_STAT_HEIGHT])
        hardware_fraction = float(np.mean(
            protected_hardware[component_pixels]
        ))
        if (
            component_height <= maximum_height
            and hardware_fraction < 0.12
        ):
            removable |= component_pixels & ~protected_hardware
    if not np.any(removable):
        return False
    alpha[removable] = 0
    return True


def _repair_continuous_woven_bottom_edge(
    rgba: np.ndarray,
    original_alpha: np.ndarray,
    cleaned_alpha: np.ndarray,
) -> bool:
    """Restore a woven bag's dark piping and clear its white-floor tail."""
    foreground = original_alpha > 8
    foreground_y, foreground_x = np.where(foreground)
    if not len(foreground_x):
        return False

    main_left = int(foreground_x.min())
    main_right = int(foreground_x.max()) + 1
    main_top = int(foreground_y.min())
    main_bottom = int(foreground_y.max()) + 1
    main_width = main_right - main_left
    main_height = main_bottom - main_top
    if main_width < 40 or main_height < 40:
        return False

    central_left = main_left + round(main_width * 0.10)
    central_right = main_right - round(main_width * 0.10)
    rgb = rgba[:, :, :3]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, saturation, value = cv2.split(hsv)
    material = foreground & (saturation >= 45) & (value <= 220)
    sample = material.copy()
    sample[: main_top + round(main_height * 0.30)] = False
    sample[main_top + round(main_height * 0.75) :] = False
    sample[:, :central_left] = False
    sample[:, central_right:] = False
    if not np.any(sample):
        return False
    material_value = float(np.median(value[sample]))
    material_saturation = float(np.median(saturation[sample]))
    if material_value <= 105:
        return False

    minimum_count = max(20, round(main_width * 0.28))
    measurements: list[tuple[int, float, float, float]] = []
    scan_start = main_top + round(main_height * 0.88)
    for row in range(scan_start, main_bottom):
        row_pixels = foreground[row, central_left:central_right]
        if int(np.count_nonzero(row_pixels)) < minimum_count:
            continue
        measurements.append(
            (
                row,
                float(np.median(value[row, central_left:central_right][row_pixels])),
                float(np.median(saturation[row, central_left:central_right][row_pixels])),
                float(np.median(original_alpha[row, central_left:central_right][row_pixels])),
            )
        )

    first_dark: int | None = None
    for index, (_, row_value, _, _) in enumerate(measurements):
        preceding = measurements[max(0, index - 8):index]
        if (
            preceding
            and row_value <= max(45.0, material_value * 0.56)
            and float(np.median([item[1] for item in preceding]))
            >= material_value * 0.66
        ):
            first_dark = index
            break
    if first_dark is None:
        return False

    plateau_end = first_dark
    minimum_trim_value = max(22.0, material_value * 0.15)
    minimum_trim_saturation = max(28.0, material_saturation * 0.52)
    while plateau_end + 1 < len(measurements):
        next_row, next_value, next_saturation, _ = measurements[plateau_end + 1]
        previous_row = measurements[plateau_end][0]
        reference_value = float(np.median(
            [item[1] for item in measurements[first_dark:plateau_end + 1]]
        ))
        if (
            next_row != previous_row + 1
            or next_value < max(minimum_trim_value, reference_value * 0.70)
            or next_value > reference_value * 1.40
            or next_saturation < minimum_trim_saturation
        ):
            break
        plateau_end += 1

    following = plateau_end + 1
    if plateau_end - first_dark + 1 < 3 or following >= len(measurements):
        return False
    reference_value = float(np.median(
        [item[1] for item in measurements[first_dark:plateau_end + 1]]
    ))
    shadow_start, shadow_value, _, _ = measurements[following]
    if shadow_value > max(18.0, reference_value * 0.72):
        return False
    trim_start = measurements[first_dark][0]

    before = cleaned_alpha.copy()
    yy = np.arange(cleaned_alpha.shape[0])[:, None]
    xx = np.arange(cleaned_alpha.shape[1])[None, :]
    central = (xx >= central_left) & (xx < central_right)
    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gradient = cv2.magnitude(
        cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3),
    )
    gold = (
        (hue >= 6)
        & (hue <= 42)
        & (saturation >= 75)
        & (value >= 90)
        & (red >= blue + 10)
        & (green >= blue + 5)
    )
    silver = (
        (saturation <= 38)
        & (value >= 90)
        & (gradient >= 50)
    )

    trim_region = (
        central
        & (yy >= trim_start)
        & (yy < shadow_start)
        & (original_alpha > 8)
    )
    cleaned_alpha[trim_region] = 0
    trim_core = (
        trim_region
        & (original_alpha >= 230)
        & (
            ((saturation >= 48) & (value <= 210))
            | (value <= 145)
            | gold
        )
    ).astype(np.uint8)
    trim_support = cv2.morphologyEx(
        trim_core,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (9, 1)),
    ).astype(bool)
    restore_trim = trim_region & (original_alpha >= 230) & trim_support
    cleaned_alpha[restore_trim] = original_alpha[restore_trim]
    cleaned_alpha[central & (yy >= shadow_start) & ~gold] = 0

    side_overlap = max(4, round(main_width * 0.10))
    side_ranges = (
        (main_left, min(central_right, central_left + side_overlap)),
        (max(central_left, central_right - side_overlap), main_right),
    )
    for side_left, side_right in side_ranges:
        side_width = side_right - side_left
        if side_width < 5:
            continue
        side_original_alpha = original_alpha[:, side_left:side_right]
        side_saturation = saturation[:, side_left:side_right]
        side_value = value[:, side_left:side_right]
        side_gold = gold[:, side_left:side_right]
        anchor = (
            (side_original_alpha >= 180)
            & (
                ((side_saturation >= 55) & (side_value <= 225))
                | (side_value <= 120)
            )
        )
        counts = anchor.sum(axis=1)
        anchor_rows = np.where(
            (np.arange(cleaned_alpha.shape[0]) >= trim_start - 30)
            & (counts >= max(4, round(side_width * 0.06)))
        )[0]
        if not len(anchor_rows):
            continue
        anchor_bottom = int(anchor_rows[-1])
        object_core = (
            (side_original_alpha > 8)
            & (
                ((side_saturation >= 72) & (side_value <= 235))
                | (side_value <= 112)
                | side_gold
            )
        )
        core_bottom = np.full(side_width, -1, dtype=np.int32)
        for column in range(side_width):
            core_rows = np.where(object_core[:, column])[0]
            if len(core_rows):
                core_bottom[column] = int(core_rows[-1])
        local_bottom = core_bottom.copy()
        for column in range(side_width):
            start = max(0, column - 4)
            end = min(side_width, column + 5)
            local_bottom[column] = int(np.max(core_bottom[start:end]))
        below_local_object = (
            yy[:, :side_width] > local_bottom[None, :] + 2
        ) & (local_bottom[None, :] >= 0)
        white_floor = (
            (yy[:, :side_width] >= trim_start - 35)
            & (side_original_alpha > 8)
            & (side_saturation <= 55)
            & (side_value >= 120)
            & below_local_object
        )
        cleaned_alpha[:, side_left:side_right][white_floor] = 0
        side_shadow = (
            (yy[:, :side_width] > anchor_bottom)
            & (side_original_alpha < 230)
            & (side_saturation <= 58)
            & (side_value >= 75)
            & ~side_gold
            & ~(
                silver[:, side_left:side_right]
                & (side_original_alpha >= 245)
            )
        )
        cleaned_alpha[:, side_left:side_right][side_shadow] = 0

    for confidence in (160, 96, 32):
        count, labels, stats, _ = cv2.connectedComponentsWithStats(
            (cleaned_alpha > confidence).astype(np.uint8),
            8,
        )
        if count <= 1:
            continue
        main_component = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        for component in range(1, count):
            if component == main_component:
                continue
            left = int(stats[component, cv2.CC_STAT_LEFT])
            top = int(stats[component, cv2.CC_STAT_TOP])
            width = int(stats[component, cv2.CC_STAT_WIDTH])
            height = int(stats[component, cv2.CC_STAT_HEIGHT])
            area = int(stats[component, cv2.CC_STAT_AREA])
            if (
                top < trim_start - 35
                or height > 6
                or width > round(main_width * 0.16)
                or area > 180
            ):
                continue
            component_pixels = labels == component
            gold_fraction = float(np.count_nonzero(gold & component_pixels)) / area
            if gold_fraction >= 0.45:
                continue
            box_left = max(0, left - 2)
            box_right = min(cleaned_alpha.shape[1], left + width + 2)
            box_bottom = min(cleaned_alpha.shape[0], top + height + 2)
            box = np.zeros_like(cleaned_alpha, dtype=bool)
            box[top:box_bottom, box_left:box_right] = True
            cleaned_alpha[box & (labels != main_component)] = 0

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        (cleaned_alpha > 8).astype(np.uint8),
        8,
    )
    if count > 1:
        main_component = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        maximum_fragment_height = max(6, round((shadow_start - trim_start) * 1.2))
        for component in range(1, count):
            if component == main_component:
                continue
            top = int(stats[component, cv2.CC_STAT_TOP])
            height = int(stats[component, cv2.CC_STAT_HEIGHT])
            component_pixels = labels == component
            component_area = int(stats[component, cv2.CC_STAT_AREA])
            gold_fraction = (
                float(np.count_nonzero(gold & component_pixels))
                / component_area
            )
            if (
                top >= trim_start - 35
                and height <= maximum_fragment_height
                and gold_fraction < 0.45
            ):
                cleaned_alpha[component_pixels] = 0

    return not np.array_equal(before, cleaned_alpha)


def _remove_detached_floor_fragments(image: Image.Image) -> Image.Image:
    """Remove faint floor slivers created when a cutout is resized for export."""
    result = image.convert("RGBA").copy()
    rgba = np.asarray(result).copy()
    alpha = rgba[:, :, 3]
    original_alpha = alpha.copy()
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
    rgb = rgba[:, :, :3]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gradient = cv2.magnitude(
        cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3),
    )
    local_value_range = (
        cv2.dilate(gray, np.ones((3, 3), dtype=np.uint8))
        - cv2.erode(gray, np.ones((3, 3), dtype=np.uint8))
    )
    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    detached_gold_core = (
        (hsv[:, :, 0] >= 7)
        & (hsv[:, :, 0] <= 42)
        & (hsv[:, :, 1] >= 32)
        & (hsv[:, :, 2] >= 100)
        & (red >= blue + 8)
        & (green >= blue + 3)
        & (gradient >= 12)
    )
    detached_silver_core = (
        (hsv[:, :, 1] <= 48)
        & (hsv[:, :, 2] >= 38)
        & (hsv[:, :, 2] <= 245)
        & (gradient >= 24)
        & (local_value_range >= 16)
    )
    detached_metal_core = detached_gold_core | detached_silver_core

    changed = False
    deferred_orphan_cleanup: tuple[int, int, int, int, int] | None = None
    deferred_orphan_hardware: np.ndarray | None = None
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
        metal_count = int(np.count_nonzero(
            detached_metal_core[component_pixels]
        ))
        compact_metal_shape = (
            height >= max(3, result.height // 300)
            or width <= max(10, height * 5)
        )
        verified_detached_metal = (
            compact_metal_shape
            and metal_count
            >= (
                1
                if area <= 12
                else max(3, round(area * 0.020))
            )
        )
        if (
            top >= lower_start
            and height <= maximum_height
            and area <= maximum_area
            and product_span_left <= center_x <= product_span_right
            and mean_alpha < 190
            and not verified_detached_metal
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
        # White-studio shadows can stay attached by a one-pixel bridge after
        # resizing. On a light or coloured product, a near-black terminal run
        # in the central lower edge is not bag material: the real piping above
        # it keeps appreciable colour/value continuity. Apply this only at the
        # very end of the main component and never to verified metal. Dark
        # bags are excluded by their own material median.
        if median_material_value > 105:
            # On a non-dark product, a broad near-black terminal row is the
            # contact shadow rather than the bag.  Judge complete rows instead
            # of applying a global dark-pixel threshold: real stitching and
            # hardware are sparse, while a floor shadow occupies most of a
            # shallow row.  This also avoids the old failure where increasing
            # one black threshold cut coloured piping from the bag itself.
            terminal_start = main_top + round(main_height * 0.955)
            terminal_black = np.zeros_like(main_pixels)
            for row in range(terminal_start, main_top + main_height):
                row_pixels = (
                    main_pixels[row]
                    & (xx[row] >= central_left)
                    & (xx[row] < central_right)
                    & ~detached_metal_core[row]
                )
                row_count = int(np.count_nonzero(row_pixels))
                if row_count < max(12, round(main_width * 0.10)):
                    continue
                row_values = hsv[row, :, 2][row_pixels]
                row_median = float(np.median(row_values))
                near_black_fraction = float(np.mean(
                    row_values
                    <= max(18.0, median_material_value * 0.10)
                ))
                dark_tail_fraction = float(np.mean(
                    row_values
                    <= min(55.0, median_material_value * 0.30)
                ))
                broad_near_black = near_black_fraction >= 0.62
                broad_dark_tail = (
                    row_median <= min(45.0, median_material_value * 0.24)
                    and dark_tail_fraction >= 0.58
                )
                if broad_near_black or broad_dark_tail:
                    terminal_black[row] |= row_pixels
            if np.any(terminal_black):
                alpha[terminal_black] = 0
                changed = True
        terminal_gold_core = (
            (hsv[:, :, 0] >= 7)
            & (hsv[:, :, 0] <= 42)
            & (hsv[:, :, 1] >= 90)
            & (hsv[:, :, 2] >= 105)
            & (red >= blue + 10)
            & (green >= blue + 5)
        )
        terminal_silver_core = (
            (hsv[:, :, 1] <= 50)
            & (hsv[:, :, 2] >= 35)
            & (hsv[:, :, 2] <= 245)
            & (alpha > 8)
            # Silver can become warm-neutral under studio lighting. Require
            # both a hard edge and alternating local values, then let the
            # compact-component filter below reject broad shadow boundaries.
            & (gradient >= 22)
            & (local_value_range >= 17)
        )
        terminal_silver_core = cv2.morphologyEx(
            terminal_silver_core.astype(np.uint8),
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        ).astype(bool)
        terminal_silver_core = (
            terminal_silver_core
            & (
                (gradient >= 18)
                | (local_value_range >= 14)
            )
        )
        terminal_silver_core = _retain_compact_terminal_hardware(
            terminal_silver_core,
            main_width=main_width,
            main_height=main_height,
            main_area=main_area,
        )
        terminal_hardware = cv2.dilate(
            (terminal_gold_core | terminal_silver_core).astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            iterations=1,
        ).astype(bool)
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

        # The useful part of the first GitHub algorithm was its decisive
        # treatment of the low-confidence tail after the real product edge.
        # Reuse only that observation: locate a late alpha collapse in the
        # current silhouette, then clear low-chroma tail pixels strictly below
        # it. The collapse row itself is retained, so this cannot flatten or
        # synthesise the bag bottom. Verified gold/silver feet and chains are
        # excluded. A minimum affected area prevents this rule from touching
        # the few antialiased pixels under an already-clean pale bag.
        row_alpha_medians = np.zeros(alpha.shape[0], dtype=np.float32)
        for row in range(main_top, main_top + main_height):
            row_pixels = (
                main_pixels[row, central_left:central_right]
                & (alpha[row, central_left:central_right] > 8)
            )
            if np.any(row_pixels):
                row_alpha_medians[row] = float(np.median(
                    alpha[row, central_left:central_right][row_pixels]
                ))
        confidence_collapse_row: int | None = None
        confidence_scan_start = main_top + round(main_height * 0.82)
        for row in range(
            max(main_top + 3, confidence_scan_start),
            main_top + main_height,
        ):
            previous_alpha = row_alpha_medians[row - 3:row]
            previous_alpha = previous_alpha[previous_alpha > 0]
            current_alpha = float(row_alpha_medians[row])
            if (
                previous_alpha.size
                and float(np.median(previous_alpha)) >= 235
                and 0 < current_alpha <= 220
            ):
                confidence_collapse_row = row
                break
        if confidence_collapse_row is not None:
            terminal_chroma_limit = max(
                35.0,
                median_material_saturation * 1.50,
            )
            very_neutral_limit = max(
                22.0,
                median_material_saturation * 0.80,
            )
            historical_tail_candidate = (
                main_pixels
                & (yy > confidence_collapse_row)
                & (xx >= central_left)
                & (xx < central_right)
                & (alpha < 245)
                & (hsv[:, :, 1] <= terminal_chroma_limit)
                & (
                    (alpha < 220)
                    | (hsv[:, :, 1] <= very_neutral_limit)
                )
                & ~terminal_hardware
            )
            minimum_historical_tail = max(
                40,
                round(main_area * 0.00020),
            )
            if (
                int(np.count_nonzero(historical_tail_candidate))
                >= minimum_historical_tail
            ):
                alpha[historical_tail_candidate] = 0
                changed = True

            # A white bag on a white sweep often leaves a fully opaque pale
            # shelf plus darker contact-shadow pixels after the confidence
            # collapse. Colour thresholds cannot remove both: the shelf looks
            # like leather and the contact shadow is no longer bright. In this
            # narrow white-studio case the sharp late alpha collapse is the
            # reliable product boundary. Clear everything from that boundary
            # down, while protecting compact gold/silver fittings and chains.
            collapse_width = int(np.count_nonzero(
                main_pixels[confidence_collapse_row]
                & (alpha[confidence_collapse_row] > 8)
            ))
            pale_flat_terminal = (
                median_material_value >= 165
                and median_material_saturation <= 28
                and collapse_width >= max(20, round(main_width * 0.40))
            )
            if pale_flat_terminal:
                tail_start = confidence_collapse_row
                if (
                    confidence_collapse_row > main_top
                    and row_alpha_medians[confidence_collapse_row - 1] < 245
                ):
                    tail_start -= 1
                body_anchor_row = max(main_top, tail_start - 5)
                body_anchor_columns = np.flatnonzero(
                    main_pixels[body_anchor_row]
                    & (alpha[body_anchor_row] >= 128)
                )
                terminal_body_left = central_left
                terminal_body_right = central_right
                if body_anchor_columns.size >= max(
                    20,
                    round(main_width * 0.40),
                ):
                    terminal_body_left = int(body_anchor_columns.min())
                    terminal_body_right = int(body_anchor_columns.max()) + 1
                central_tail = (
                    main_pixels
                    & (yy >= tail_start)
                    & (xx >= terminal_body_left)
                    & (xx < terminal_body_right)
                )
                side_tail = (
                    main_pixels
                    & (yy >= tail_start)
                    & (
                        (xx < terminal_body_left)
                        | (xx >= terminal_body_right)
                    )
                    & ~terminal_hardware
                )
                pale_flat_tail = (
                    central_tail
                    | side_tail
                )
                if np.any(pale_flat_tail):
                    alpha[pale_flat_tail] = 0
                    changed = True

        # Coloured and woven bags can carry a fully opaque, colour-cast
        # contact shadow. Alpha-only and low-chroma rules miss it because it
        # looks like a dark continuation of the material. Detect a late,
        # broad drop in value relative to the bag itself, keep the last real
        # piping rows above it, and clear the tail only inside the body span.
        # Gold/silver chains crossing that span remain protected.
        if (
            median_material_value > 105
            and median_material_saturation >= 50
        ):
            dark_contact_start: int | None = None
            dark_contact_value_limit = max(
                45.0,
                median_material_value * 0.56,
            )
            minimum_dark_contact_width = max(
                20,
                round(main_width * 0.28),
            )
            dark_contact_scan_start = main_top + round(main_height * 0.90)
            dark_row_measurements: list[tuple[int, float]] = []
            for row in range(
                max(main_top, dark_contact_scan_start),
                main_top + main_height,
            ):
                row_pixels = (
                    main_pixels[row, central_left:central_right]
                    & (alpha[row, central_left:central_right] > 8)
                )
                row_count = int(np.count_nonzero(row_pixels))
                if row_count < minimum_dark_contact_width:
                    continue
                row_value = float(np.median(
                    hsv[row, central_left:central_right, 2][row_pixels]
                ))
                dark_row_measurements.append((row, row_value))

            for index, (row, row_value) in enumerate(dark_row_measurements):
                if row_value > dark_contact_value_limit:
                    continue
                previous_values = [
                    value
                    for _, value in dark_row_measurements[
                        max(0, index - 8):index
                    ]
                ]
                previous_material_value = (
                    float(np.median(previous_values))
                    if previous_values
                    else median_material_value
                )
                if previous_material_value < median_material_value * 0.66:
                    continue

                # A real leather/straw piping band can itself be dark. It is
                # usually several stable rows followed by a second, much
                # darker drop into the cast shadow. Preserve that plateau and
                # start at the second drop; a one-row drop is shadow already.
                plateau_end = index
                while plateau_end + 1 < len(dark_row_measurements):
                    next_row, next_value = dark_row_measurements[plateau_end + 1]
                    previous_row = dark_row_measurements[plateau_end][0]
                    if (
                        next_row != previous_row + 1
                        or next_value < row_value * 0.85
                        or next_value > row_value * 1.15
                    ):
                        break
                    plateau_end += 1
                plateau_length = plateau_end - index + 1
                following_index = plateau_end + 1
                if (
                    plateau_length >= 3
                    and following_index < len(dark_row_measurements)
                    and dark_row_measurements[following_index][0]
                    == dark_row_measurements[plateau_end][0] + 1
                    and dark_row_measurements[following_index][1]
                    <= row_value * 0.82
                ):
                    dark_contact_start = dark_row_measurements[
                        following_index
                    ][0]
                else:
                    dark_contact_start = row
                break

            if dark_contact_start is not None:
                # Do not erase a genuine one- or two-row piping edge.  An
                # opaque cast shadow has a measurable vertical footprint;
                # isolated terminal lines are deliberately left intact when
                # there is no second, deeper value transition.
                dark_contact_rows = 0
                for row in range(
                    dark_contact_start,
                    main_top + main_height,
                ):
                    row_pixels = (
                        main_pixels[row, central_left:central_right]
                        & (alpha[row, central_left:central_right] > 8)
                    )
                    if (
                        int(np.count_nonzero(row_pixels))
                        < minimum_dark_contact_width
                    ):
                        continue
                    row_value = float(np.median(
                        hsv[row, central_left:central_right, 2][row_pixels]
                    ))
                    if row_value <= max(
                        65.0,
                        median_material_value * 0.72,
                    ):
                        dark_contact_rows += 1
                minimum_contact_rows = max(
                    3,
                    round(main_height * 0.004),
                )
                if dark_contact_rows < minimum_contact_rows:
                    dark_contact_start = None

            if dark_contact_start is not None:
                body_anchor_row = max(main_top, dark_contact_start - 10)
                body_anchor_columns = np.flatnonzero(
                    main_pixels[body_anchor_row]
                    & (alpha[body_anchor_row] >= 128)
                    & (xx[body_anchor_row] >= central_left)
                    & (xx[body_anchor_row] < central_right)
                )
                if body_anchor_columns.size >= minimum_dark_contact_width:
                    dark_body_left = int(body_anchor_columns.min())
                    dark_body_right = int(body_anchor_columns.max()) + 1
                    dark_contact_tail = (
                        main_pixels
                        & (yy >= dark_contact_start)
                        & (xx >= dark_body_left)
                        & (xx < dark_body_right)
                        & ~terminal_hardware
                    )
                    if np.any(dark_contact_tail):
                        alpha[dark_contact_tail] = 0
                        changed = True

                    # Mixed-brightness shadows can leave shallow islands just
                    # above the detected contact start. Defer their removal
                    # until every later floor pass has run, because those
                    # passes can be what finally exposes the vertical gap.
                    orphan_scan_top = max(
                        main_top,
                        dark_contact_start
                        - max(12, round(main_height * 0.025)),
                    )
                    deferred_orphan_cleanup = (
                        dark_body_left,
                        dark_body_right,
                        orphan_scan_top,
                        min(
                            alpha.shape[0],
                            main_top + main_height,
                        ),
                        max(
                            4,
                            round(main_height * 0.015),
                        ),
                    )
                    deferred_orphan_hardware = terminal_hardware.copy()

        if floor_row is not None and median_material_value > 105:
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
            silver_core = _retain_compact_terminal_hardware(
                silver_core,
                main_width=main_width,
                main_height=main_height,
                main_area=main_area,
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
            has_substantial_floor_residue = (
                int(np.count_nonzero(connected_floor_residue))
                >= minimum_residue_area
            )
            has_confident_terminal_collapse = (
                strongest_width_drop
                >= max(20, round(broad_width * 0.40))
            )
            if (
                has_substantial_floor_residue
                or has_confident_terminal_collapse
            ):
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
                verified_floor_metal = gold_core | silver_core
                for row in range(taper_start, floor_row + 1):
                    row_columns = np.flatnonzero(
                        (alpha[row] >= 128)
                        & main_pixels[row]
                        & (xx[row] >= main_left)
                        & (xx[row] < main_left + main_width)
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
                ) & (xx >= main_left) & (xx < main_left + main_width)
                alpha[lateral_floor_residue] = 0
                if has_substantial_floor_residue:
                    alpha[connected_floor_residue] = 0

                # Resampling can leave a dark/neutral one-pixel tail below a
                # real coloured base or piping. Follow the last trustworthy
                # material pixel independently in each central column, then
                # clear only non-material pixels at or below that envelope.
                # This works for both curved bases and flat bucket-bag piping
                # without synthesising a new bottom contour.
                material_chroma_floor = float(np.clip(
                    median_material_saturation * 0.55,
                    18,
                    55,
                ))
                material_value_floor = max(
                    55.0,
                    median_material_value * 0.35,
                )
                lower_material_core = (
                    main_pixels
                    & (xx >= central_left)
                    & (xx < central_right)
                    & (yy >= main_top + round(main_height * 0.55))
                    & (hsv[:, :, 1] >= material_chroma_floor)
                    & (hsv[:, :, 2] >= material_value_floor)
                )
                has_lower_material = np.any(
                    lower_material_core,
                    axis=0,
                )
                material_bottoms = (
                    alpha.shape[0]
                    - 1
                    - np.argmax(lower_material_core[::-1], axis=0)
                ).astype(np.int32)
                material_bottoms[~has_lower_material] = -1
                smoothing_width = max(
                    5,
                    min(17, main_width // 45),
                )
                if smoothing_width % 2 == 0:
                    smoothing_width += 1
                padded_bottoms = np.pad(
                    material_bottoms,
                    smoothing_width // 2,
                    mode="edge",
                )
                smoothed_material_bottoms = np.median(
                    np.lib.stride_tricks.sliding_window_view(
                        padded_bottoms,
                        smoothing_width,
                    ),
                    axis=1,
                ).astype(np.int32)
                below_material_envelope = (
                    (smoothed_material_bottoms[None, :] >= 0)
                    # Keep the last trustworthy material row and its
                    # antialiased neighbour.  Treat only pixels genuinely
                    # below that contour as possible studio-floor residue.
                    & (yy > smoothed_material_bottoms[None, :] + 1)
                )
                non_material_shadow = (
                    main_pixels
                    & (xx >= central_left)
                    & (xx < central_right)
                    & below_material_envelope
                    & ~lower_material_core
                    & (
                        (
                            hsv[:, :, 1]
                            < material_chroma_floor * 1.10
                        )
                        | (
                            (hsv[:, :, 2] < median_material_value * 0.50)
                            & (
                                hsv[:, :, 1]
                                < median_material_saturation * 0.90
                            )
                        )
                    )
                    & ~verified_floor_metal
                )
                alpha[non_material_shadow] = 0

                # On a confidently terminated light/coloured base, a faint
                # studio shadow may continue below the contour at the sides.
                # Side fittings, leather tabs and dark piping live in exactly
                # the same zone, so only clear genuinely translucent pixels
                # *below* the detected floor.  Opaque side pixels are product
                # evidence and must survive even when their colour is neutral.
                if (
                    (
                        has_confident_terminal_collapse
                        or has_substantial_floor_residue
                    )
                    # White leather can have a single-digit median
                    # saturation. It still needs the same side-shadow pass;
                    # the metal mask above, rather than product saturation,
                    # is what protects real silver links.
                    and median_material_saturation >= 10
                ):
                    tight_terminal_metal = cv2.dilate(
                        verified_floor_metal.astype(np.uint8),
                        cv2.getStructuringElement(
                            cv2.MORPH_ELLIPSE,
                            (3, 3),
                        ),
                        iterations=1,
                    ).astype(bool)
                    side_neutral_shadow = (
                        main_pixels
                        & (yy > floor_row + 2)
                        & (
                            (xx < central_left)
                            | (xx >= central_right)
                        )
                        & (alpha < 160)
                        & (hsv[:, :, 1] <= 50)
                        & (hsv[:, :, 2] >= 70)
                        & ~tight_terminal_metal
                    )
                    alpha[side_neutral_shadow] = 0
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
                # This is a second-pass floor inspection, not a general
                # colour key.  Keep the useful historical central-bottom
                # cleanup for pale shadows, but never extend it into the side
                # zones where chain, tabs and fittings sit.
                & (yy >= main_top + round(main_height * 0.90))
                & (xx >= central_left)
                & (xx < central_right)
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

        # Resizing can leave several opaque colour-cast slivers detached from
        # a woven or textured bottom. They are too strong for the old faint
        # fragment rule, but still have the geometry of a floor residue:
        # shallow, low, central, and materially darker or less chromatic than
        # the body. This pass never removes a tall cord/strap or a verified
        # metal component.
        detached_count, detached_labels, detached_stats, _ = (
            cv2.connectedComponentsWithStats(
                (alpha > 8).astype(np.uint8),
                8,
            )
        )
        if detached_count > 1:
            detached_largest = 1 + int(np.argmax(
                detached_stats[1:, cv2.CC_STAT_AREA]
            ))
            detached_lower_start = main_top + round(main_height * 0.86)
            detached_maximum_height = max(
                8,
                round(main_height * 0.035),
            )
            detached_maximum_area = max(
                180,
                round(main_area * 0.012),
            )
            for component in range(1, detached_count):
                if component == detached_largest:
                    continue
                component_left = int(
                    detached_stats[component, cv2.CC_STAT_LEFT]
                )
                component_top = int(
                    detached_stats[component, cv2.CC_STAT_TOP]
                )
                component_width = int(
                    detached_stats[component, cv2.CC_STAT_WIDTH]
                )
                component_height = int(
                    detached_stats[component, cv2.CC_STAT_HEIGHT]
                )
                component_area = int(
                    detached_stats[component, cv2.CC_STAT_AREA]
                )
                component_center_x = component_left + component_width / 2
                component_pixels = detached_labels == component
                hardware_fraction = float(np.mean(
                    terminal_hardware[component_pixels]
                ))
                component_value = float(np.median(
                    hsv[:, :, 2][component_pixels]
                ))
                component_saturation = float(np.median(
                    hsv[:, :, 1][component_pixels]
                ))
                material_mismatch = (
                    component_value
                    < max(55.0, median_material_value * 0.90)
                    or component_saturation
                    < max(18.0, median_material_saturation * 0.70)
                )
                if (
                    component_top >= detached_lower_start
                    and component_width >= 6
                    and component_height <= detached_maximum_height
                    and component_area <= detached_maximum_area
                    and product_span_left
                    <= component_center_x
                    <= product_span_right
                    and hardware_fraction < 0.12
                    and material_mismatch
                ):
                    alpha[component_pixels] = 0
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

    if (
        deferred_orphan_cleanup is not None
        and deferred_orphan_hardware is not None
    ):
        (
            orphan_left,
            orphan_right,
            orphan_top,
            orphan_bottom,
            orphan_maximum_height,
        ) = deferred_orphan_cleanup
        if _remove_central_bottom_orphan_tails(
            alpha,
            left=orphan_left,
            right=orphan_right,
            top=orphan_top,
            bottom=orphan_bottom,
            protected_hardware=deferred_orphan_hardware,
            maximum_height=orphan_maximum_height,
        ):
            changed = True

    # Verify the removal against the product's own local bottom colour. This
    # can add back a few over-cut edge pixels, but never pixels which were not
    # present in the incoming matte.
    if _restore_overcut_bottom_material(rgba, original_alpha, alpha):
        changed = True

    # White-background woven bags can have a continuous dark leather piping
    # immediately above a second, darker floor-shadow band.  Handle that
    # specific geometry after the generic over-cut recovery has restored the
    # real edge; other materials must pass the detector without modification.
    if _repair_continuous_woven_bottom_edge(rgba, original_alpha, alpha):
        changed = True

    # Lanczos resizing and alpha feathering can leave sub-visible (1..8)
    # colour specks after an attached shadow has been removed. They become a
    # dirty dotted line on a grey marketplace preview even though they carry
    # less than 3.2% opacity. Normalise this numerical tail globally; alpha 9
    # and above remains untouched so real antialiased product edges survive.
    numerical_alpha_tail = (alpha > 0) & (alpha <= 8)
    if np.any(numerical_alpha_tail):
        alpha[numerical_alpha_tail] = 0
        changed = True

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
        "phone_scale": number("phone_scale", 1.0, 0.5, 4.0),
        "phone_offset_x": number("phone_offset_x", 0.0, -1.5, 1.5),
        "phone_offset_y": number("phone_offset_y", 0.0, -1.5, 1.5),
        "phone_label_scale": number("phone_label_scale", 1.0, 0.5, 2.0),
        "phone_label_offset_x": number("phone_label_offset_x", 0.0, -1.5, 1.5),
        "phone_label_offset_y": number("phone_label_offset_y", 0.0, -1.5, 1.5),
        "phone_label_linked": value.get("phone_label_linked") is not False,
        "phone_alignment": "center" if value.get("phone_alignment") == "center" else "bottom",
        "product_show_ruler": value.get("product_show_ruler") is not False,
        "phone_show_ruler": value.get("phone_show_ruler") is not False,
        "product_ruler_gap_scale": number("product_ruler_gap_scale", 1.0, 0.5, 4.0),
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


def _organizer_layer_cache_path(
    image_id: int,
    modified_ns: int,
    crop_key: tuple[int, int, int, int],
) -> Path:
    source_path = asset_original(image_id)
    crop_token = "-".join(str(value) for value in crop_key)
    return source_path.parent / "render-cache" / f"organizer-layer-{image_id}-{modified_ns}-{crop_token}.png"


@lru_cache(maxsize=16)
def _cached_product_cutout(
    image_id: int,
    modified_ns: int,
    crop_key: tuple[int, int, int, int],
) -> Image.Image:
    cache_path = _organizer_layer_cache_path(image_id, modified_ns, crop_key)
    if cache_path.is_file():
        with Image.open(cache_path) as cached:
            return cached.convert("RGBA").copy()

    crop_x, crop_y, crop_width, crop_height = (value / 1_000_000 for value in crop_key)
    source = _load_image(image_id)
    cropped = _crop_source(source, {
        "crop_x": crop_x,
        "crop_y": crop_y,
        "crop_width": crop_width,
        "crop_height": crop_height,
    })
    cutout = _product_cutout(cropped)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_name(f".{cache_path.stem}-{uuid.uuid4().hex[:8]}.png")
    try:
        cutout.save(temporary, format="PNG", compress_level=1)
        os.replace(temporary, cache_path)
    finally:
        temporary.unlink(missing_ok=True)
    return cutout


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
    allow_free_x_position: bool = False,
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
    if not allow_free_position and not allow_free_x_position:
        global_x = _clamp_layer_origin(global_x, rendered.width, clip_left, clip_right)
    if not allow_free_position:
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
    auto_shape_shift_y: float = 0.0,
    default_mode: str = "cover",
) -> None:
    cropped = _crop_source(source, adjustment)
    has_manual_layout = _has_manual_layout_adjustment(adjustment)
    if not _has_manual_crop(adjustment) and _has_light_studio_border(cropped):
        image_id = source.info.get("_organizer_image_id")
        modified_ns = source.info.get("_organizer_modified_ns")
        if isinstance(image_id, int) and isinstance(modified_ns, int):
            cutout = _cached_product_cutout(
                image_id,
                modified_ns,
                _crop_cache_key(adjustment),
            ).copy()
        else:
            cutout = _product_cutout(cropped)
        normalized = _normalize_adjustment(adjustment)
        handle_offset_y = -0.04 * _handle_visual_lift(cutout) if auto_handle_layout else 0.0
        shape_offset_y = (
            _detail_shape_offset_y(cutout) + auto_shape_shift_y
            if auto_shape_layout
            else auto_offset_y
        )
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
            allow_free_position=has_manual_layout,
        )
        return
    _paste_layer(
        canvas,
        _rgb_on_white(cropped),
        box,
        adjustment,
        mode=_crop_aware_mode(adjustment, default_mode),
        clip_box=clip_box,
        allow_free_position=has_manual_layout,
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
    automatic_minimum_top = minimum_rendered_top
    if tall_handle_minimum_rendered_top is not None:
        body_left, body_top, body_right, body_bottom = _info_measurement_bbox(cutout)
        body_ratio = (body_right - body_left) / max(1, body_bottom - body_top)
        if body_ratio <= 1.15 and _handle_visual_lift(cutout) >= 0.55:
            automatic_minimum_top = max(
                automatic_minimum_top or 0,
                tall_handle_minimum_rendered_top,
            )
    effective_minimum_top = None if has_manual_layout else automatic_minimum_top
    layout_adjustment = adjustment
    automatic_layout_adjustment: dict[str, Any] | None = None
    if auto_handle_layout and not _has_manual_crop(adjustment):
        normalized = _normalize_adjustment(adjustment)
        left, top, right, bottom = box
        box_width = max(1, right - left)
        box_height = max(1, bottom - top)
        handle_lift = _handle_visual_lift(cutout)
        scale = min(box_width / cutout.width, box_height / cutout.height) * normalized["zoom"]
        body_left, body_top, body_right, body_bottom = _info_measurement_bbox(cutout)
        body_center_x = (body_left + body_right) / 2
        body_center_y = (body_top + body_bottom) / 2
        tall_handle_drop_y = tall_handle_drop_ratio * handle_lift if auto_tall_handle_drop else 0.0
        automatic_layout_adjustment = {
            **normalized,
            "offset_x": (cutout.width / 2 - body_center_x) * scale / box_width,
            "offset_y": (
                auto_offset_y
                + tall_handle_drop_y
                + (cutout.height / 2 - body_center_y) * scale / box_height
            ),
        }
        layout_adjustment = {
            **automatic_layout_adjustment,
            "offset_x": automatic_layout_adjustment["offset_x"] + normalized["offset_x"],
            "offset_y": automatic_layout_adjustment["offset_y"] + normalized["offset_y"],
        }
    if has_manual_layout and automatic_layout_adjustment is not None:
        left, top, right, bottom = box
        box_width = max(1, right - left)
        box_height = max(1, bottom - top)
        clip_left, clip_top, clip_right, clip_bottom = clip_box or (0, 0, *canvas.size)
        rendered_width = max(1, int(round(cutout.width * scale)))
        rendered_height = max(1, int(round(cutout.height * scale)))
        automatic_x = left + (box_width - rendered_width) // 2 + int(round(automatic_layout_adjustment["offset_x"] * box_width))
        automatic_y = top + (box_height - rendered_height) // 2 + int(round(automatic_layout_adjustment["offset_y"] * box_height))
        anchored_x = _clamp_layer_origin(automatic_x, rendered_width, clip_left, clip_right)
        anchored_y = _clamp_layer_origin(automatic_y, rendered_height, clip_top, clip_bottom)
        if automatic_minimum_top is not None:
            anchored_y = max(automatic_minimum_top, anchored_y)
        if maximum_rendered_bottom is not None:
            latest_y = maximum_rendered_bottom - rendered_height
            if automatic_minimum_top is None or latest_y >= automatic_minimum_top:
                anchored_y = min(anchored_y, latest_y)
        layout_adjustment = {
            **layout_adjustment,
            "offset_x": layout_adjustment["offset_x"] + (anchored_x - automatic_x) / box_width,
            "offset_y": layout_adjustment["offset_y"] + (anchored_y - automatic_y) / box_height,
        }
    _paste_layer(
        canvas,
        cutout,
        box,
        layout_adjustment,
        clip_box=clip_box,
        minimum_top=effective_minimum_top,
        maximum_bottom=None if has_manual_layout else maximum_rendered_bottom,
        allow_free_position=has_manual_layout,
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
    scale = (
        min(box_width / cutout.width, box_height / cutout.height)
        * normalized["zoom"]
        * INFO_PRODUCT_SCALE
    )
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
    # Keep the exact/final 401 renderer on the same handle-aware baseline as
    # the live editor so the product does not jump when exact preview arrives.
    handle_lift = _handle_visual_lift(cutout)
    body_left, body_top, body_right, body_bottom = _jd_product_body_bbox(cutout)
    automatic_layout = _info_product_auto_layout(
        cutout.width,
        cutout.height,
        (body_left, body_top, body_right, body_bottom),
        handle_lift,
    )
    scale = (
        min(box_width / cutout.width, box_height / cutout.height)
        * normalized["zoom"]
        * INFO_PRODUCT_SCALE
        * (1.0 + INFO_PRODUCT_HANDLE_SCALE * handle_lift)
        * automatic_layout["scale"]
    )
    rendered = cutout.resize(
        (max(1, round(cutout.width * scale)), max(1, round(cutout.height * scale))),
        Image.Resampling.LANCZOS,
    )
    x = (
        left
        + (box_width - rendered.width) // 2
        + round(normalized["offset_x"] * box_width)
        + round(automatic_layout["shift_x"])
    )
    y = (
        top
        + (box_height - rendered.height) // 2
        + round(normalized["offset_y"] * box_height)
        - round(INFO_PRODUCT_HANDLE_LIFT_Y * handle_lift * box_height)
        + round(automatic_layout["drop_y"] * box_height)
    )
    canvas.paste(rendered.convert("RGB"), (x, y), rendered.getchannel("A"))
    return (
        x + body_left * scale,
        y + body_top * scale,
        x + body_right * scale,
        y + body_bottom * scale,
    )


def _info_ruler_geometry(
    body: tuple[float, float, float, float],
    gap_scale: float = 1.0,
) -> dict[str, int]:
    ruler_gap = 34 * gap_scale
    body_left, body_top, body_right, body_bottom = body
    line_left = round(body_left + 4)
    line_right = round(body_right - 4)
    # Alpha bounds contain an anti-aliased edge/shadow fringe.  These calibrated
    # insets track the visible bag body more accurately than the raw extrema.
    line_bottom = round(body_bottom - 9)
    line_top = round(body_top - 5)
    vertical_x = line_left - ruler_gap
    horizontal_y = line_bottom + ruler_gap
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


def _info_product_auto_layout(
    layer_width: int,
    layer_height: int,
    body: tuple[float, float, float, float],
    handle_lift: float,
) -> dict[str, float]:
    """Return a stable 401 baseline independent of manual zoom and movement."""
    left, top, right, bottom = INFO_PRODUCT_BOX
    box_width = right - left
    box_height = bottom - top
    bounded_handle_lift = max(0.0, min(1.0, handle_lift))
    base_scale = (
        min(box_width / max(1, layer_width), box_height / max(1, layer_height))
        * INFO_PRODUCT_SCALE
        * (1.0 + INFO_PRODUCT_HANDLE_SCALE * bounded_handle_lift)
    )
    base_x = left + (box_width - layer_width * base_scale) / 2
    projected_width_ruler_right = (
        base_x
        + body[2] * base_scale
        + INFO_PRODUCT_WIDTH_RULER_ALLOWANCE
    )
    edge_pressure = max(
        0.0,
        min(
            1.0,
            (projected_width_ruler_right - INFO_PRODUCT_WIDTH_EDGE_SAFE_RIGHT)
            / INFO_PRODUCT_WIDTH_EDGE_RANGE,
        ),
    )
    no_handle_weight = 1.0 - min(1.0, bounded_handle_lift / 0.35)
    return {
        "scale": 1.0 - INFO_PRODUCT_WIDTH_EDGE_MAX_SHRINK * edge_pressure,
        "shift_x": -INFO_PRODUCT_WIDTH_EDGE_MAX_SHIFT_X * edge_pressure,
        "drop_y": INFO_PRODUCT_NO_HANDLE_DROP_Y * no_handle_weight,
        "edge_pressure": edge_pressure,
    }


def _info_width_ruler_geometry(
    base_body: tuple[float, float, float, float],
    adjustment: dict[str, Any] | None = None,
    *,
    product_center: tuple[float, float] | None = None,
    canvas_size: tuple[int, int] = (750, 665),
) -> dict[str, Any]:
    normalized = _normalize_adjustment(adjustment)
    _, _, body_right, body_bottom = base_body
    ruler_gap = 34.0 * normalized["product_ruler_gap_scale"]
    anchor_direction = (22.0 ** 2 + 18.0 ** 2) ** 0.5
    start = (
        body_right + 22.0 / anchor_direction * ruler_gap,
        body_bottom + 18.0 / anchor_direction * ruler_gap,
    )
    end = (start[0] + 51.0, start[1] - 27.0)
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    length = max(1.0, (delta_x ** 2 + delta_y ** 2) ** 0.5)
    perpendicular = (-delta_y / length * 9.0, delta_x / length * 9.0)
    raw_segments = [
        (start, end),
        ((start[0] - perpendicular[0], start[1] - perpendicular[1]), (start[0] + perpendicular[0], start[1] + perpendicular[1])),
        ((end[0] - perpendicular[0], end[1] - perpendicular[1]), (end[0] + perpendicular[0], end[1] + perpendicular[1])),
    ]
    if product_center is not None:
        grouped_segments = [
            _transform_ruler_segment(
                segment_start,
                segment_end,
                scale=normalized["product_ruler_group_scale"],
                offset_x=normalized["product_ruler_group_offset_x"],
                offset_y=normalized["product_ruler_group_offset_y"],
                canvas_size=canvas_size,
                origin=product_center,
            )
            for segment_start, segment_end in raw_segments
        ]
    else:
        grouped_segments = raw_segments
    grouped_main = grouped_segments[0]
    width_origin = (
        (grouped_main[0][0] + grouped_main[1][0]) / 2,
        (grouped_main[0][1] + grouped_main[1][1]) / 2,
    )
    segments = [
        _transform_ruler_segment(
            segment_start,
            segment_end,
            scale=normalized["width_ruler_scale"],
            offset_x=normalized["width_ruler_offset_x"],
            offset_y=normalized["width_ruler_offset_y"],
            canvas_size=canvas_size,
            origin=width_origin,
        )
        for segment_start, segment_end in grouped_segments
    ]
    main_start, main_end = segments[0]
    transformed_delta_x = main_end[0] - main_start[0]
    transformed_delta_y = main_end[1] - main_start[1]
    transformed_length = max(
        1.0,
        (transformed_delta_x ** 2 + transformed_delta_y ** 2) ** 0.5,
    )
    label_normal = (
        -transformed_delta_y / transformed_length,
        transformed_delta_x / transformed_length,
    )
    text_center = (
        round((main_start[0] + main_end[0]) / 2 + label_normal[0] * 26),
        round((main_start[1] + main_end[1]) / 2 + label_normal[1] * 26),
    )
    return {
        "segments": segments,
        "text": text_center,
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
    normalized_source = _rgb_on_white(source)
    layer = _crop_source(normalized_source, adjustment)
    _paste_layer(
        canvas,
        layer,
        (0, 0, 750, 750),
        adjustment,
        mode="contain",
        clip_box=(0, 0, 750, 750),
        allow_free_position=_has_manual_layout_adjustment(adjustment),
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


def _product_thickness(product_info: dict[str, str]) -> str:
    """Read the canonical thickness field while accepting legacy width payloads."""
    return product_info.get("product_thickness") or product_info.get("product_width") or ""


def _draw_rotated_text(
    canvas: Image.Image,
    text: str,
    xy: tuple[int, int],
    angle: float,
    font: ImageFont.ImageFont,
    *,
    fill: str = "#555555",
) -> None:
    box = font.getbbox(text)
    layer = Image.new("RGBA", (max(1, box[2] - box[0] + 12), max(1, box[3] - box[1] + 12)), (255, 255, 255, 0))
    ImageDraw.Draw(layer).text((6 - box[0], 6 - box[1]), text, font=font, fill=fill)
    normalized_angle = angle % 360
    if normalized_angle == 90:
        rotated = layer.transpose(Image.Transpose.ROTATE_90)
    elif normalized_angle == 270:
        rotated = layer.transpose(Image.Transpose.ROTATE_270)
    elif normalized_angle == 180:
        rotated = layer.transpose(Image.Transpose.ROTATE_180)
    else:
        rotated = layer.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
    canvas.paste(rotated, xy, rotated)


def _draw_rotated_text_centered(
    canvas: Image.Image,
    text: str,
    center: tuple[int, int],
    angle: float,
    font: ImageFont.ImageFont,
) -> None:
    box = font.getbbox(text)
    layer = Image.new("RGBA", (max(1, box[2] - box[0] + 12), max(1, box[3] - box[1] + 12)), (255, 255, 255, 0))
    ImageDraw.Draw(layer).text((6 - box[0], 6 - box[1]), text, font=font, fill="#555555")
    rotated = layer.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
    xy = (
        round(center[0] - rotated.width / 2),
        round(center[1] - rotated.height / 2),
    )
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
        draw.text((INFO_TEXT_X, y), label, font=_font(20, True), fill="#111111")
        draw.text((INFO_TEXT_X, y + 34), value[:18], font=_font(19), fill="#555555")
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
    ruler = _info_ruler_geometry(
        ruler_body,
        normalized["product_ruler_gap_scale"],
    )
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
    def draw_ruler_segments(
        segments: list[tuple[tuple[float, float], tuple[float, float]]],
    ) -> None:
        scale = 4
        layer = Image.new("RGBA", (image.width * scale, image.height * scale), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        for start, end in segments:
            layer_draw.line(
                (start[0] * scale, start[1] * scale, end[0] * scale, end[1] * scale),
                fill=line_color,
                width=2 * scale,
            )
        layer = layer.resize(image.size, Image.Resampling.LANCZOS)
        image.paste(layer, (0, 0), layer)

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
    draw_ruler_segments([
        (length_start, length_end),
        ((length_start[0], length_start[1] - 9), (length_start[0], length_start[1] + 9)),
        ((length_end[0], length_end[1] - 9), (length_end[0], length_end[1] + 9)),
    ])
    length_text = _dimension_mm(info.get("product_length") or "")
    length_font = _font(19)
    length_box = draw.textbbox((0, 0), length_text, font=length_font)
    length_center = (length_start[0] + length_end[0]) / 2
    draw.text((length_center - (length_box[2] - length_box[0]) / 2, length_start[1] + 16), length_text, font=length_font, fill="#555555")

    height_start, height_end = _transform_product_ruler_segment(
        (ruler["vertical_x"], ruler["top"] + INFO_HEIGHT_RULER_SHIFT_Y),
        (ruler["vertical_x"], ruler["bottom"] + INFO_HEIGHT_RULER_SHIFT_Y),
        product_ruler_center,
        normalized,
        scale=normalized["height_ruler_scale"],
        offset_x=normalized["height_ruler_offset_x"],
        offset_y=normalized["height_ruler_offset_y"],
        canvas_size=image.size,
    )
    draw_ruler_segments([
        (height_start, height_end),
        ((height_start[0] - 9, height_start[1]), (height_start[0] + 9, height_start[1])),
        ((height_end[0] - 9, height_end[1]), (height_end[0] + 9, height_end[1])),
    ])
    _draw_rotated_text(
        image,
        _dimension_mm(info.get("product_height") or ""),
        (height_start[0] - 45, round((height_start[1] + height_end[1]) / 2 - 49)),
        90,
        _font(18),
    )

    draw_ruler_segments(width_ruler["segments"])
    _draw_rotated_text_centered(
        image,
        _dimension_mm(_product_thickness(info)),
        width_ruler["text"],
        26,
        _font(18),
    )

    disclaimer = info.get("disclaimer") or "包身长高厚测量均为最长部分\n误差在1-2cm之间因手工测量均属正常"
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
    _paste_layer(
        canvas,
        cropped,
        box,
        adjustment,
        mode=_crop_aware_mode(adjustment, "cover"),
        clip_box=clip_box,
        allow_free_position=_has_manual_layout_adjustment(adjustment),
    )
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
        auto_shape_shift_y=0.02,
    )
    return canvas


def _multi_angle_safe_box(index: int) -> tuple[int, int, int, int]:
    """Return the non-overlapping editable quadrant for one 606 source."""
    boxes = (
        (30, 135, 375, 420),
        (375, 135, 720, 420),
        (30, 420, 375, 720),
        (375, 420, 720, 720),
    )
    return boxes[index] if 0 <= index < len(boxes) else boxes[0]


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
        clip_box = _multi_angle_safe_box(index) if _has_manual_layout_adjustment(adjustment) else None
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
        allow_free_position=_has_manual_layout_adjustment(adjustment),
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
            auto_offset_y=-0.03,
            minimum_rendered_top=(
                162
                if size == (800, 800)
                else 175
            ),
            tall_handle_minimum_rendered_top=(
                162
                if size == (800, 800)
                else 175
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

    safe_left = round(width * 0.04)
    safe_top = round(height * 0.04)
    safe_right = round(width * 0.96)
    safe_bottom = round(height * 0.96)
    object_gap = max(16, round(width * 0.025))
    phone_default_shift_x = 8 if height > width else 12
    product_ruler_gap = max(28, round(width * 0.045))
    product_left_allowance = product_ruler_gap + max(24, round(width * 0.03))
    phone_ruler_gap = max(22, round(width * 0.035))
    phone_label_clearance = max(40, round(width * 0.05))
    group_left_bound = safe_left + product_left_allowance
    group_right_bound = safe_right - phone_ruler_gap - phone_label_clearance
    group_available_width = max(1, group_right_bound - group_left_bound)

    preferred_body_width = width * preferred_width_ratio * max(0.82, min(1.08, length_mm / 205.0))
    base_scale = min(
        width * max_width_ratio / body_width,
        height * max_height_ratio / body_height,
        width * 0.46 / max(1, cutout.width),
        height * 0.60 / max(1, cutout.height),
        preferred_body_width / body_width,
    )
    for _ in range(2):
        fitted_phone_height = max(
            height * 0.095,
            min(height * 0.46, body_height * base_scale * JD_PHONE_HEIGHT_MM / height_mm),
        )
        group_width = (
            body_width * base_scale
            + object_gap
            + phone_default_shift_x
            + fitted_phone_height * JD_PHONE_ASPECT_RATIO
        )
        base_scale *= min(1.0, group_available_width / max(1.0, group_width))
    scale = base_scale * normalized["zoom"]
    rendered_width = max(1, round(cutout.width * scale))
    rendered_height = max(1, round(cutout.height * scale))
    scaled_body = (
        round(body_left * scale),
        round(body_top * scale),
        round(body_right * scale),
        round(body_bottom * scale),
    )

    base_phone_height = max(
        height * 0.095,
        min(height * 0.46, body_height * base_scale * JD_PHONE_HEIGHT_MM / height_mm),
    )
    base_group_width = body_width * base_scale + object_gap + base_phone_height * JD_PHONE_ASPECT_RATIO
    centered_group_left = group_left_bound + max(0.0, group_available_width - base_group_width) / 2
    base_group_left = max(
        group_left_bound,
        min(centered_group_left, group_right_bound - base_group_width - phone_default_shift_x),
    )
    desired_body_center_x = (
        base_group_left
        + body_width * base_scale / 2
        + normalized["offset_x"] * width * 0.18
    )
    desired_body_bottom = height * (0.70 if height > width else 0.73) + normalized["offset_y"] * height * 0.18
    paste_x = round(desired_body_center_x - (scaled_body[0] + scaled_body[2]) / 2)
    paste_y = round(desired_body_bottom - scaled_body[3])

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
        "automatic_body_center_x": base_group_left + body_width * base_scale / 2,
        "safe_box": (safe_left, safe_top, safe_right, safe_bottom),
    }


def _jd_measure_font(size: tuple[int, int]) -> ImageFont.ImageFont:
    return _font(max(14, round(min(size) * 0.022)))


@lru_cache(maxsize=24)
def _jd_phone_medium_font(size: int) -> ImageFont.ImageFont:
    if BUNDLED_FONT_PATH.exists():
        font = ImageFont.truetype(str(BUNDLED_FONT_PATH), size=size)
        try:
            font.set_variation_by_name("Medium")
        except (AttributeError, OSError):
            pass
        return font
    return _font(size)


def _jd_phone_label_font(
    size: tuple[int, int],
    phone_height: int,
    label_scale: float = 1.0,
) -> ImageFont.ImageFont:
    regular_size = max(12, round(min(size) * 0.017))
    adaptive_size = max(10, min(regular_size, round(phone_height * 0.085)))
    return _jd_phone_medium_font(max(8, round(adaptive_size * label_scale)))


def _jd_phone_label_gap(size: tuple[int, int], phone_height: int) -> int:
    reference_height = min(size) * 0.22
    phone_scale = max(0.65, min(1.5, phone_height / reference_height))
    return max(6, round(min(size) * 0.015 * phone_scale))


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
    font = _jd_measure_font(canvas.size)
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
            fill=color,
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


def _jd_adaptive_phone_extra_gap(size: tuple[int, int], available_extra_gap: int) -> int:
    """Use spare right-side room to separate the phone without risking overflow."""
    width, height = size
    portrait_output = height > width
    available = max(0, available_extra_gap)
    minimum = 8 if portrait_output else 12
    maximum = 28 if portrait_output else 48
    spare_room_share = 0.4 if portrait_output else 0.55
    preferred = max(minimum, min(maximum, round(available * spare_room_share)))
    return min(available, preferred)


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
    baseline_shift_x = round(
        (base_body[0] + base_body[2]) / 2 - base_layout["automatic_body_center_x"]
    )
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
    phone_height = max(1, round(base_phone_height * normalized["phone_scale"]))
    reference = _jd_phone_reference_layer()
    reference_ratio = reference.width / reference.height if reference is not None else 0.83
    base_phone_width = max(42, round(base_phone_height * reference_ratio))
    phone_width = max(42, round(phone_height * reference_ratio))
    phone_ruler_gap = max(22, round(width * 0.035))
    phone_label_clearance = max(40, round(width * 0.05))
    phone_right_allowance = phone_ruler_gap + phone_label_clearance
    phone_bottom_allowance = max(28, round(height * 0.055))
    object_gap = max(16, round(width * 0.025))
    minimum_phone_left = base_layout["body_box"][2] + object_gap
    maximum_phone_left = safe_right - base_phone_width - phone_right_allowance
    available_extra_gap = max(0, maximum_phone_left - minimum_phone_left)
    base_phone_left = minimum_phone_left + _jd_adaptive_phone_extra_gap(size, available_extra_gap)
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
    phone_center_x = (
        base_phone_center_x
        + normalized["phone_offset_x"] * width * 0.18
    )
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
    phone_label_box = phone_box if normalized["phone_label_linked"] else (
        base_phone_left,
        base_phone_top,
        base_phone_left + base_phone_width,
        base_phone_top + base_phone_height,
    )
    label_font = _jd_phone_label_font(
        size,
        phone_label_box[3] - phone_label_box[1],
        normalized["phone_label_scale"],
    )
    phone_label = JD_PHONE_LABEL
    label_box = draw.textbbox((0, 0), phone_label, font=label_font)
    draw.text(
        (
            round(
                (phone_label_box[0] + phone_label_box[2] - (label_box[2] - label_box[0])) / 2
                + normalized["phone_label_offset_x"] * width * 0.18
            ),
            round(
                phone_label_box[3]
                + _jd_phone_label_gap(size, phone_label_box[3] - phone_label_box[1])
                + normalized["phone_label_offset_y"] * height * 0.18
            ),
        ),
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
            _crop_source(_rgb_on_white(source), adjustment),
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
        return _normalized_product_page(
            source,
            transparent=True,
            adjustment=adjustment,
            auto_handle_layout=True,
            auto_tall_handle_drop=True,
            auto_offset_y=-0.03,
            manual_padding_ratio=0.18,
        )
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
            "folder_logo_colors": {
                str(folder): color
                for folder, color in (
                    item.get("folder_logo_colors", {}).items()
                    if isinstance(item.get("folder_logo_colors"), dict)
                    else []
                )
                if folder in {"800", "750"} and color in {"black", "white"}
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


def _slot_logo_color_for_folder(slot: dict[str, Any], target_folder: str) -> str:
    folder_logo_colors = slot.get("folder_logo_colors")
    if isinstance(folder_logo_colors, dict):
        color = folder_logo_colors.get(target_folder)
        if color in {"black", "white"}:
            return color
    return "white" if slot.get("logo_color") == "white" else "black"


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
        _paste_layer(
            canvas,
            _crop_source(source.convert("RGB"), adjustment),
            (0, 0, 800, 800),
            adjustment,
            mode=_crop_aware_mode(adjustment, "cover"),
            allow_free_position=_has_manual_layout_adjustment(adjustment),
        )
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
        _paste_layer(
            canvas,
            _crop_source(source.convert("RGB"), adjustment),
            (0, 0, 950, 1200),
            adjustment,
            mode=_crop_aware_mode(adjustment, "cover"),
            allow_free_position=_has_manual_layout_adjustment(adjustment),
        )
        return canvas
    if file_name == "4.jpg":
        canvas = Image.new("RGB", (800, 800), "white")
        _paste_layer(
            canvas,
            _crop_source(_rgb_on_white(source), adjustment),
            (0, 0, 800, 800),
            adjustment,
            mode=_crop_aware_mode(adjustment, "cover"),
            allow_free_position=_has_manual_layout_adjustment(adjustment),
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
    if _fast_slot_preview_ready(slots, file_name, platform, target_folder):
        with _FAST_SLOT_PREVIEW_LOCK:
            try:
                return _render_slot_preview(
                    session_id,
                    slots,
                    product_info,
                    file_name,
                    platform,
                    target_folder,
                )
            finally:
                # The API process may serve many sessions on a 2GB host. Keep
                # the fast path bounded to one request and release decoded
                # source/layer images immediately after composing the slot.
                _cached_product_cutout.cache_clear()
                _load_image_file.cache_clear()
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


def _fast_slot_preview_ready(
    slots: list[dict[str, Any]],
    file_name: str,
    platform: str,
    target_folder: str,
) -> bool:
    """Use the resident Pillow renderer only when heavy layers are on disk."""
    if platform == "vip" and file_name in {"15.jpg", "604.jpg", "605.jpg"}:
        return False
    if platform == "jd" and file_name == "4.jpg":
        return False

    product_files = (
        {"2.jpg", "3.jpg", "30.png", "401.jpg", "606.jpg"}
        if platform == "vip"
        else {"2.jpg", "5.jpg", "透明.png"}
    )
    if file_name not in product_files:
        return True

    slot_map = _slot_map(slots, platform)
    slot = slot_map.get(file_name)
    if not slot:
        return False
    adjustments = _slot_adjustments_for_folder(slot, target_folder)
    for index, image_id in enumerate(slot.get("image_ids", [])):
        adjustment = adjustments[index] if index < len(adjustments) else None
        source_path = asset_original(image_id)
        cache_path = _organizer_layer_cache_path(
            image_id,
            source_path.stat().st_mtime_ns,
            _crop_cache_key(adjustment),
        )
        if not cache_path.is_file():
            return False
    return bool(slot.get("image_ids"))


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
        "logo_color": _slot_logo_color_for_folder(slot, target_folder),
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
            "logo_color": _slot_logo_color_for_folder(slot, target_folder),
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
                    _slot_logo_color_for_folder(slot, target),
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
