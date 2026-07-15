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
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests
from fastapi import UploadFile
from PIL import Image, ImageDraw, ImageFont, ImageOps

from ..config import ALLOWED_IMAGE_EXTENSIONS, RESULT_DIR, UPLOAD_DIR
from ..database import db_session, now_iso


ORGANIZER_UPLOAD_DIR = UPLOAD_DIR / "vip_organizer"
UPLOAD_COPY_BUFFER_SIZE = 1024 * 1024


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
    ("606.jpg", "正面、侧面、背面、顶视图", "750×750", "composite"),
    ("801.jpg", "吊牌信息", "750×750", "tag"),
]

PRODUCT_ROLES = {"auto", "front", "side", "back", "top", "transparent", "logo", "interior", "detail", "ignore"}
API_ANALYSIS_ROLES = PRODUCT_ROLES - {"auto", "ignore"}


def _analysis_config() -> dict[str, str]:
    env_key = os.getenv("VIP_ANALYSIS_API_KEY", "").strip()
    if env_key:
        return {
            "api_base_url": os.getenv("VIP_ANALYSIS_BASE_URL", "https://n.tokeness.io/v1").strip(),
            "api_key": env_key,
            "model_name": os.getenv("VIP_ANALYSIS_MODEL", "gpt-5.6-sol").strip(),
        }
    with db_session() as conn:
        row = conn.execute("SELECT api_base_url, api_key, model_name FROM vip_analysis_config WHERE id = 1").fetchone()
    if not row:
        raise ValueError("尚未配置素材分析 API")
    return dict(row)


def save_analysis_config(api_base_url: str, api_key: str, model_name: str) -> dict[str, Any]:
    base_url = api_base_url.strip().rstrip("/")
    model = model_name.strip()
    key = api_key.strip()
    if not base_url or not key or not model:
        raise ValueError("API Base URL、API Key 和模型名称不能为空")
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO vip_analysis_config (id, api_base_url, api_key, model_name, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                api_base_url = excluded.api_base_url,
                api_key = excluded.api_key,
                model_name = excluded.model_name,
                updated_at = excluded.updated_at
            """,
            (base_url, key, model, now_iso()),
        )
    return {"configured": True, "api_base_url": base_url, "model_name": model}


def analysis_config_status() -> dict[str, Any]:
    try:
        config = _analysis_config()
    except ValueError:
        return {"configured": False}
    key = config["api_key"]
    masked = f"{key[:4]}****{key[-4:]}" if len(key) > 8 else "****"
    return {
        "configured": True,
        "api_base_url": config["api_base_url"],
        "model_name": config["model_name"],
        "api_key_masked": masked,
    }


def _analysis_data_url(path: Path) -> str:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((900, 900), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=78, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def analyze_assets_with_api(session_id: str, product_image_ids: list[int]) -> dict[str, Any]:
    _validate_session_assets(session_id, {"product": product_image_ids})
    rows = _uploaded_rows(product_image_ids)
    if not rows:
        raise ValueError("请至少上传一张商品原图")
    config = _analysis_config()
    role_text = "front正面, side侧面, back背面, top顶视图, transparent透明底正面, logo ELLE Logo或五金近景, interior内里, detail其他细节"
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            "你是ELLE女包电商素材分类员。请逐张判断图片用途，只能从固定角色中选择："
            f"{role_text}。每张图只能有一个角色；相似图片允许同角色。"
            "重点区分完整产品图与链条/拉链近景。只有专门拍摄外部ELLE金属字标或主Logo的近景才标为logo；"
            "链条调节扣、拉链和普通五金即使带ELLE小字也标为detail；出现内衬、内袋或包内空间的照片一律标为interior，"
            "即使画面中有ELLE内里皮标。俯拍包口形状但不重点展示内袋时标为top。"
            "仅返回JSON对象，不要Markdown，格式："
            '{"items":[{"index":1,"role":"front","confidence":90,"reason":"简短中文理由"}]}。'
        ),
    }]
    for index, row in enumerate(rows, start=1):
        content.append({"type": "text", "text": f"图片 {index}，文件名：{row['file_name']}"})
        content.append({"type": "image_url", "image_url": {"url": _analysis_data_url(Path(row["file_path"])), "detail": "low"}})
    url = config["api_base_url"].rstrip("/") + "/chat/completions"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
        json={
            "model": config["model_name"],
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
        },
        timeout=350,
    )
    if not response.ok:
        raise ValueError(f"素材分析 API 返回 HTTP {response.status_code}：{response.text[:300]}")
    try:
        raw = response.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", raw, re.S)
        parsed = json.loads(match.group(0) if match else raw)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("素材分析 API 未返回可读取的固定标签 JSON") from exc
    results: list[dict[str, Any]] = []
    roles: dict[int, str] = {}
    for item in parsed.get("items", []):
        index = int(item.get("index", 0))
        role = str(item.get("role", "detail"))
        if 1 <= index <= len(rows) and role in API_ANALYSIS_ROLES:
            image_id = int(rows[index - 1]["id"])
            roles[image_id] = role
            results.append({
                "image_id": image_id,
                "file_name": rows[index - 1]["file_name"],
                "role": role,
                "confidence": max(0, min(100, int(item.get("confidence", 0)))),
                "reason": str(item.get("reason", ""))[:160],
            })
    if not results:
        raise ValueError("素材分析 API 没有返回有效分类")
    return {"asset_roles": roles, "items": results}


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
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
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
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return {
        **row,
        "preview_url": f"/api/vip-organizer/assets/{row['id']}/thumbnail",
        "original_url": f"/uploads/vip_organizer/{Path(row['file_path']).name}",
        "alpha_ratio": round(alpha_ratio, 4),
        "foreground_ratio": round(float(foreground.mean()), 4),
        "bbox_ratio": round(float(bbox_ratio), 4),
        "object_ratio": round(float(object_ratio), 4),
        "sharpness": round(sharpness, 2),
    }


def start_session() -> dict[str, str]:
    """Keep organizer storage bounded to one current session."""
    ORGANIZER_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for path in ORGANIZER_UPLOAD_DIR.iterdir():
        if path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
    for path in RESULT_DIR.glob("vip_*"):
        if path.is_dir():
            shutil.rmtree(path)
    for path in RESULT_DIR.glob("唯品会套图_*.zip"):
        path.unlink(missing_ok=True)
    with db_session() as conn:
        conn.execute("DELETE FROM vip_organizer_assets")
        conn.execute("DELETE FROM vip_organizer_sessions")
        session_id = uuid.uuid4().hex
        conn.execute("INSERT INTO vip_organizer_sessions (id, created_at) VALUES (?, ?)", (session_id, now_iso()))
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
    ORGANIZER_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    prepared: list[dict[str, Any]] = []
    for file in files:
        original_name = file.filename or "image.png"
        suffix = Path(original_name).suffix.lower()
        if suffix not in ALLOWED_IMAGE_EXTENSIONS:
            continue
        path = ORGANIZER_UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
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
                    "original_url": f"/uploads/vip_organizer/{item['path'].name}",
                    "width": item["width"],
                    "height": item["height"],
                })
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
    thumbnail_path = ORGANIZER_UPLOAD_DIR / f"thumb_{image_id}.jpg"
    if thumbnail_path.exists() and thumbnail_path.stat().st_mtime >= source_path.stat().st_mtime:
        return thumbnail_path

    temp_path = ORGANIZER_UPLOAD_DIR / f".{thumbnail_path.name}.{uuid.uuid4().hex}.tmp"
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

    role_overrides = {
        int(image_id): role
        for image_id, role in (asset_roles or {}).items()
        if role in PRODUCT_ROLES and role != "auto"
    }
    usable_products = [item for item in products if role_overrides.get(item["id"]) != "ignore"]
    if not usable_products:
        raise ValueError("所有商品图都被标记为忽略，请至少保留一张")

    def assigned(role: str) -> dict[str, Any] | None:
        return next((item for item in usable_products if role_overrides.get(item["id"]) == role), None)

    def assigned_all(role: str) -> list[dict[str, Any]]:
        return [item for item in usable_products if role_overrides.get(item["id"]) == role]

    transparent = assigned("transparent") or max(usable_products, key=lambda item: item["alpha_ratio"])
    has_transparent = role_overrides.get(transparent["id"]) == "transparent" or transparent["alpha_ratio"] > 0.02
    non_transparent = [item for item in usable_products if item["id"] != transparent["id"]] if has_transparent else usable_products[:]
    # Product folders normally mix clean full views with extreme close-ups. Keep
    # those groups separate before ranking angles; otherwise a chain or zipper
    # detail can look statistically closer to a full bag than an actual side view.
    full_views = [
        item
        for item in non_transparent
        if 0.04 <= item["foreground_ratio"] <= 0.26 and item["bbox_ratio"] <= 0.36
    ]
    if len(full_views) < 3:
        full_views = sorted(
            non_transparent,
            key=lambda item: (abs(item["foreground_ratio"] - 0.14), abs(item["bbox_ratio"] - 0.22)),
        )[: max(3, min(6, len(non_transparent)))]

    extreme_closeups = [
        item
        for item in non_transparent
        if item["bbox_ratio"] >= 0.72 and item["foreground_ratio"] >= 0.30
    ]
    closeups = sorted(
        extreme_closeups or non_transparent,
        key=lambda item: (item["bbox_ratio"], item["foreground_ratio"], item["sharpness"]),
        reverse=True,
    )

    def product_at(items: list[dict[str, Any]], index: int) -> int:
        if not items:
            return products[0]["id"]
        return items[min(index, len(items) - 1)]["id"]

    regular_views = [item for item in full_views if 0.9 <= item["object_ratio"] <= 1.8]
    regular_views.sort(key=lambda item: item["id"])
    front_view = assigned("front") or (regular_views[0] if regular_views else full_views[0])
    back_view = assigned("back") or (regular_views[1] if len(regular_views) > 1 else next(
        (item for item in full_views if item["id"] != front_view["id"]),
        front_view,
    ))
    front_id = transparent["id"] if has_transparent else front_view["id"]
    side_candidates = [item for item in full_views if item["object_ratio"] < 0.75 and item["foreground_ratio"] >= 0.055]
    side_candidates.sort(key=lambda item: (abs(item["object_ratio"] - 0.45), -item["sharpness"]))
    side_view = assigned("side") or (side_candidates[0] if side_candidates else min(full_views, key=lambda item: item["object_ratio"]))
    side_id = side_view["id"]
    top_candidates = [
        item for item in full_views
        if item["id"] not in {front_view["id"], back_view["id"], side_id}
        and 0.72 <= item["object_ratio"] <= 0.95
    ]
    top_view = assigned("top") or (min(top_candidates, key=lambda item: item["sharpness"]) if top_candidates else next(
        (item for item in full_views if item["id"] not in {front_view["id"], back_view["id"], side_id}),
        back_view,
    ))
    back_id = back_view["id"]
    top_id = top_view["id"]

    # A metal wordmark close-up usually has much stronger fine edges than an
    # interior-lining photo. This remains a draft; both selections stay editable.
    assigned_logos = assigned_all("logo")
    logo_view = max(assigned_logos, key=lambda item: item["sharpness"]) if assigned_logos else max(closeups, key=lambda item: item["sharpness"])
    interior_views = sorted(
        (item for item in closeups if item["id"] != logo_view["id"]),
        key=lambda item: item["id"],
    )
    assigned_interiors = assigned_all("interior")
    close_interiors = [
        item for item in assigned_interiors
        if item["bbox_ratio"] >= 0.72 and item["foreground_ratio"] >= 0.60
    ]
    if close_interiors:
        assigned_interiors = close_interiors
    interior_view = assigned_interiors[0] if assigned_interiors else (interior_views[0] if interior_views else logo_view)
    if len(assigned_interiors) > 1:
        secondary_detail = assigned_interiors[1]
    else:
        secondary_detail = assigned("detail") or (interior_views[1] if len(interior_views) > 1 else interior_view)
    logo_id = logo_view["id"]
    interior_id = interior_view["id"]

    model_ids = [item["id"] for item in sorted(models, key=lambda item: item["sharpness"], reverse=True)]
    model_pick = lambda index: [model_ids[index % len(model_ids)]] if model_ids else []
    tag_ids = [tags[0]["id"]] if tags else []

    slot_values = {
        "1.jpg": (model_pick(0), 88 if models else 0, "来自独立模特图区，优先选择清晰度较高的照片"),
        "2.jpg": ([front_view["id"]], 72, "从完整产品图中按上传顺序选择首张标准展示图，建议确认是否符合渠道要求"),
        "3.jpg": ([back_id], 58, "从完整产品图中选择第二张正背面候选，正背面仍需人工确认"),
        "4.jpg": ([logo_id], 76, "从超近景中选择细节边缘最清晰的图片，建议确认是否为ELLE Logo"),
        "15.jpg": ([interior_id], 70, "排除Logo后，从超近景中选择首张内里候选"),
        "30.png": ([front_id], 98 if has_transparent else 35, "检测到透明通道" if has_transparent else "未检测到透明图，暂用正面候选图"),
        "50.jpg": (model_pick(0), 88 if models else 0, "与1.jpg使用同一张模特图，仅按竖版规格重新排版"),
        "401.jpg": ([front_id], 100, "根据正面产品图和商品资料套用信息页模板"),
        "601.jpg": (model_pick(0), 90 if models else 0, "模特图自动留白排版"),
        "602.jpg": (model_pick(1), 90 if models else 0, "模特图自动留白排版"),
        "603.jpg": (model_pick(2), 90 if models else 0, "模特图自动留白排版"),
        "604.jpg": ([secondary_detail["id"]], 65, "使用另一张内里或结构细节候选，保留手动调整"),
        "605.jpg": ([logo_id], 76, "复用ELLE Logo清晰近景候选"),
        "606.jpg": ([front_view["id"], side_id, back_id, top_id], 62, "按完整正面、窄侧面、背面和顶视轮廓生成四角度初稿"),
        "801.jpg": (tag_ids, 95 if tags else 0, "使用独立上传的吊牌图片" if tags else "尚未上传吊牌图片"),
    }
    slots = [
        _slot(file_name, title, size, kind, *slot_values[file_name])
        for file_name, title, size, kind in SLOT_DEFINITIONS
    ]
    suggested_roles: dict[int, str] = {
        front_view["id"]: "front",
        side_id: "side",
        back_id: "back",
        top_id: "top",
        logo_id: "logo",
        interior_id: "interior",
        secondary_detail["id"]: "detail",
    }
    if has_transparent:
        suggested_roles[transparent["id"]] = "transparent"
    for item in products:
        item["selected_role"] = role_overrides.get(item["id"], "auto")
        item["suggested_role"] = suggested_roles.get(item["id"], "detail")
    for item in models:
        item["selected_role"] = "model"
        item["suggested_role"] = "model"
    for item in tags:
        item["selected_role"] = "tag"
        item["suggested_role"] = "tag"
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


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
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
    padding = max(3, int(max(width, height) * 0.008))
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
    cutout.thumbnail((right - left, bottom - top), Image.Resampling.LANCZOS)
    x = left + (right - left - cutout.width) // 2
    y = top + (bottom - top - cutout.height) // 2
    canvas.paste(cutout.convert("RGB"), (x, y), cutout.getchannel("A"))


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
        _paste_product(image, product_image, (378, 270, 665, 470))

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

    disclaimer = info.get("disclaimer") or "商品尺寸均为手工测量，存在少量误差属于正常情况。"
    notes = [line.strip() for line in disclaimer.splitlines() if line.strip()]
    if len(notes) < 2:
        notes = textwrap.wrap(disclaimer.replace("\n", " "), width=31)[:2]
    for index, line in enumerate(notes):
        draw.text((329, 580 + index * 27), f"* {line[:34]}", font=_font(15), fill="#222222")
    return image


def _multi_angle_page(image_ids: list[int]) -> Image.Image:
    canvas = Image.new("RGB", (750, 750), "white")
    draw = ImageDraw.Draw(canvas)
    title = "多角度展示"
    title_font = _font(35, True)
    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((750 - (title_box[2] - title_box[0])) / 2, 62), title, font=title_font, fill="#111111")
    boxes = [
        (65, 180, 305, 355),
        (445, 180, 685, 355),
        (65, 500, 305, 675),
        (445, 500, 685, 675),
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
    folder = RESULT_DIR / f"vip_{export_id}"
    folder.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []

    for file_name, _, _, kind in SLOT_DEFINITIONS:
        ids = slot_map.get(file_name, [])
        output = folder / file_name
        if file_name == "401.jpg":
            source = _load_image(ids[0]) if ids else None
            _info_page(product_info, source).save(output, quality=94)
        elif file_name == "606.jpg":
            if len(ids) < 4:
                missing.append(file_name)
                continue
            _multi_angle_page(ids).save(output, quality=94)
        elif not ids:
            missing.append(file_name)
        elif file_name == "30.png":
            _save_png_30(_load_image(ids[0]), output)
        elif file_name == "50.jpg":
            _fit(_load_image(ids[0]), (950, 1200)).save(output, quality=94)
        elif file_name.startswith("60") or file_name == "801.jpg":
            _fit(_load_image(ids[0]), (750, 750), margin=40, contain=True).save(output, quality=94)
        else:
            _fit(_load_image(ids[0]), (800, 800)).save(output, quality=94)

    zip_path = RESULT_DIR / f"唯品会套图_{export_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.iterdir()):
            archive.write(path, arcname=path.name)
    previews = [f"/results/{folder.name}/{path.name}" for path in sorted(folder.iterdir()) if path.suffix.lower() in {".jpg", ".png"}]
    return {
        "download_url": f"/results/{zip_path.name}",
        "previews": previews,
        "generated_count": len(previews),
        "missing": missing,
    }
