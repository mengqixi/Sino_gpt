from __future__ import annotations

import json
import math
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timedelta
from importlib import import_module
from io import BytesIO
from pathlib import Path
from threading import Semaphore
from typing import Any, Iterable

from fastapi import UploadFile
from PIL import Image

from ..config import PRODUCT_IMAGE_INPUT_DIR, PRODUCT_IMAGE_OUTPUT_DIR
from ..database import db_session, now_iso


REFERENCE_ROLES = ("front", "back", "semi_side", "top", "logo")
REFERENCE_LABELS = {
    "front": "正面",
    "back": "背面",
    "semi_side": "半侧面",
    "top": "顶部开口",
    "logo": "Logo 细节",
}
OUTPUT_SLOTS = (
    "front_transparent",
    "front_main",
    "back",
    "semi_side",
    "top",
    "logo_detail",
)
API_OUTPUT_SLOTS = ("front_main", "back", "semi_side", "top")
OUTPUT_VARIANTS = ("highres", "800")
SLOT_REFERENCE_ROLE = {
    "front_transparent": "front",
    "front_main": "front",
    "back": "back",
    "semi_side": "semi_side",
    "top": "top",
    "logo_detail": "logo",
}
ZIP_NAMES = {
    "front_transparent": "01_正面透明.png",
    "front_main": "02_正面主图.jpg",
    "back": "03_背面图.jpg",
    "semi_side": "04_半侧面图.jpg",
    "top": "05_顶部开口图.jpg",
    "logo_detail": "06_Logo细节.jpg",
}

MAX_HISTORY_GROUPS = 50
SOURCE_TTL_HOURS = 24
TASK_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_VIDEO_BYTES = 500 * 1024 * 1024
MAX_VIDEO_SECONDS = 60.0
MAX_BROWSER_FRAME_BYTES = 5 * 1024 * 1024
MAX_BROWSER_FRAMES = 12
MAX_TRANSPARENT_UPLOAD_BYTES = 40 * 1024 * 1024
MIN_FREE_DISK_BYTES = 3 * 1024 * 1024 * 1024
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
VIDEO_SUFFIXES = {".mp4", ".mov"}

# Pillow, HEIC and OpenCV can briefly allocate several copies of a full-size
# image. The production deployment uses one worker; this gate also protects
# that worker when several users upload at the same time.
MEDIA_DECODE_SEMAPHORE = Semaphore(1)


class ProductImageError(ValueError):
    """A user-correctable product-image workflow error."""


class ProductImageConflict(ProductImageError):
    """The requested state transition conflicts with an active task."""


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def validate_task_id(task_id: str) -> str:
    if not TASK_ID_PATTERN.fullmatch(task_id or ""):
        raise ProductImageError("商品图任务不存在")
    return task_id


def validate_reference_role(role: str) -> str:
    if role not in REFERENCE_ROLES:
        raise ProductImageError("素材入口不正确")
    return role


def validate_output_slot(slot: str) -> str:
    if slot not in OUTPUT_SLOTS:
        raise ProductImageError("商品图输出位置不存在")
    return slot


def _media() -> Any:
    """Resolve the media module lazily so database-only tests stay lightweight."""
    return import_module("backend.services.product_image_media_service")


def _disk_probe_path(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _ensure_disk_reserve(path: Path, incoming_bytes: int = 0) -> None:
    """Keep enough free space for SQLite and completed task outputs."""
    try:
        free = int(shutil.disk_usage(_disk_probe_path(path)).free)
    except OSError as exc:
        raise ProductImageError("无法检查服务器磁盘余量，请稍后再上传") from exc
    required = MIN_FREE_DISK_BYTES + max(0, int(incoming_bytes))
    if free < required:
        raise ProductImageError("服务器磁盘空间不足；为保证任务安全，至少需要保留 3GB 可用空间")


def _upload_size_budget(file: UploadFile, maximum_bytes: int, *, extra_bytes: int = 0) -> int:
    declared = getattr(file, "size", None)
    if isinstance(declared, int) and declared > maximum_bytes:
        raise ProductImageError(f"文件超过 {maximum_bytes // (1024 * 1024)}MB 限制")
    base = declared if isinstance(declared, int) and declared >= 0 else maximum_bytes
    return int(base) + max(0, int(extra_bytes))


def _stored_item(path: Path, original_name: str, metadata: dict[str, Any], mime_type: str | None = None) -> dict[str, Any]:
    quality = metadata.get("quality") if isinstance(metadata.get("quality"), dict) else {}
    return {
        "file_name": original_name,
        "file_path": str(path),
        "file_size": path.stat().st_size,
        "mime_type": metadata.get("mime_type") or mime_type,
        "width": metadata.get("width"),
        "height": metadata.get("height"),
        "duration_seconds": metadata.get("duration_seconds"),
        "sharpness": metadata.get(
            "sharpness",
            metadata.get("laplacian_variance_900", quality.get("laplacian_variance_900")),
        ),
        "frame_time_seconds": metadata.get("frame_time_seconds", metadata.get("timestamp_seconds")),
        "analysis_valid": metadata.get("analysis_valid"),
        "analysis_reason": metadata.get("analysis_reason"),
    }


def _with_local_quality(metadata: dict[str, Any]) -> dict[str, Any]:
    width = int(metadata.get("width") or 0)
    height = int(metadata.get("height") or 0)
    quality = metadata.get("quality") if isinstance(metadata.get("quality"), dict) else {}
    sharpness = float(
        metadata.get("laplacian_variance_900")
        or metadata.get("sharpness")
        or quality.get("laplacian_variance_900")
        or 0
    )
    reasons: list[str] = []
    if min(width, height) < 640:
        reasons.append("图片分辨率过低，短边至少需要 640 像素")
    if sharpness < 18:
        reasons.append("图片严重模糊，请重新拍摄")
    return {
        **metadata,
        "analysis_valid": 0 if reasons else 1,
        "analysis_reason": "；".join(reasons) if reasons else "已通过本地分辨率与清晰度检查",
    }


def _quality_if_missing(media: Any, metadata: dict[str, Any], path: Path) -> dict[str, Any]:
    quality = metadata.get("quality") if isinstance(metadata.get("quality"), dict) else {}
    if (
        metadata.get("laplacian_variance_900") is not None
        or metadata.get("sharpness") is not None
        or quality.get("laplacian_variance_900") is not None
    ):
        return metadata
    return {**metadata, **media.quality_metrics(path)}


def _save_image_upload(task_id: str, role: str, file: UploadFile) -> dict[str, Any]:
    media = _media()
    original_name = file.filename or "image.jpg"
    suffix = Path(original_name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise ProductImageError("图片格式不支持，请上传 JPG、PNG、WebP 或 HEIC")
    folder = PRODUCT_IMAGE_INPUT_DIR / task_id / "images" / role
    _ensure_disk_reserve(
        folder,
        _upload_size_budget(file, MAX_IMAGE_BYTES, extra_bytes=MAX_IMAGE_BYTES),
    )
    folder.mkdir(parents=True, exist_ok=True)
    raw_path = folder / f".{uuid.uuid4().hex}{suffix}.upload"
    target = folder / f"{uuid.uuid4().hex}.jpg"
    try:
        media.stream_upload(file, raw_path, MAX_IMAGE_BYTES)
        with MEDIA_DECODE_SEMAPHORE:
            metadata = media.normalize_photo(raw_path, target)
            metadata = _quality_if_missing(media, metadata, target)
        metadata = _with_local_quality(metadata)
        return _stored_item(target, original_name, metadata, "image/jpeg")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        raw_path.unlink(missing_ok=True)


def _save_video_upload(task_id: str, file: UploadFile) -> dict[str, Any]:
    media = _media()
    original_name = file.filename or "video.mp4"
    suffix = Path(original_name).suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        raise ProductImageError("视频格式不支持，请上传 MP4 或 MOV")
    video_dir = PRODUCT_IMAGE_INPUT_DIR / task_id / "videos"
    _ensure_disk_reserve(
        video_dir,
        _upload_size_budget(
            file,
            MAX_VIDEO_BYTES,
            extra_bytes=MAX_BROWSER_FRAMES * MAX_BROWSER_FRAME_BYTES,
        ),
    )
    video_dir.mkdir(parents=True, exist_ok=True)
    video_path = video_dir / f"{uuid.uuid4().hex}{suffix}"
    frame_dir = PRODUCT_IMAGE_INPUT_DIR / task_id / "frames" / video_path.stem
    try:
        media.stream_upload(file, video_path, MAX_VIDEO_BYTES)
        with MEDIA_DECODE_SEMAPHORE:
            video_metadata = media.probe_video(video_path)
            duration = float(video_metadata.get("duration_seconds") or 0)
            if duration <= 0:
                raise ProductImageError("无法读取视频时长，请确认视频未损坏")
            if duration > MAX_VIDEO_SECONDS + 0.01:
                raise ProductImageError("单个视频最长 60 秒")
            frames = media.extract_video_frames(
                video_path,
                frame_dir,
                max_frames=MAX_BROWSER_FRAMES,
            )
            if not frames:
                raise ProductImageError("视频中没有提取到可用画面")
            prepared_frames: list[dict[str, Any]] = []
            for frame in frames:
                path = Path(frame.get("file_path") or frame.get("path") or "")
                if not path.is_file():
                    continue
                quality = _with_local_quality(_quality_if_missing(media, frame, path))
                prepared_frames.append(
                    _stored_item(
                        path,
                        f"{Path(original_name).stem}_{float(frame.get('frame_time_seconds', frame.get('timestamp_seconds', 0))):.2f}s.jpg",
                        quality,
                        "image/jpeg",
                    )
                )
        if not prepared_frames:
            raise ProductImageError("视频中没有提取到可用画面")
        video_item = _stored_item(video_path, original_name, video_metadata, file.content_type)
        video_item["duration_seconds"] = duration
        return {"video": video_item, "frames": prepared_frames}
    except Exception:
        video_path.unlink(missing_ok=True)
        shutil.rmtree(frame_dir, ignore_errors=True)
        raise


def _save_browser_frame_upload(
    task_id: str,
    video_token: str,
    file: UploadFile,
    timestamp_seconds: float,
) -> dict[str, Any]:
    media = _media()
    original_name = file.filename or "frame.jpg"
    suffix = Path(original_name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise ProductImageError("候选帧格式不支持，请上传 JPG、PNG 或 WebP")
    folder = PRODUCT_IMAGE_INPUT_DIR / task_id / "frames" / video_token
    _ensure_disk_reserve(
        folder,
        _upload_size_budget(
            file,
            MAX_BROWSER_FRAME_BYTES,
            extra_bytes=MAX_BROWSER_FRAME_BYTES,
        ),
    )
    folder.mkdir(parents=True, exist_ok=True)
    raw_path = folder / f".{uuid.uuid4().hex}{suffix}.upload"
    target = folder / f"{uuid.uuid4().hex}.jpg"
    try:
        media.stream_upload(file, raw_path, MAX_BROWSER_FRAME_BYTES)
        with MEDIA_DECODE_SEMAPHORE:
            metadata = media.normalize_photo(raw_path, target)
            metadata = _quality_if_missing(media, metadata, target)
        metadata = _with_local_quality(metadata)
        metadata["frame_time_seconds"] = timestamp_seconds
        return _stored_item(target, original_name, metadata, "image/jpeg")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        raw_path.unlink(missing_ok=True)


def _safe_path(path_value: str | Path, root: Path) -> Path:
    path = Path(path_value).resolve()
    root = root.resolve()
    try:
        inside = os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        inside = False
    if not inside:
        raise ProductImageError("文件路径不属于当前商品图任务")
    return path


def _delete_file(path_value: str | Path | None, root: Path) -> None:
    if not path_value:
        return
    try:
        _safe_path(path_value, root).unlink(missing_ok=True)
    except (OSError, ProductImageError):
        return


def _asset_payload(row: Any) -> dict[str, Any]:
    task_id = str(row["task_id"])
    asset_id = int(row["id"])
    media_type = str(row["media_type"])
    kind = "video_frame" if media_type == "frame" else media_type
    browser_frame_video = (
        media_type == "video"
        and row["mime_type"] == "application/vnd.product-image.browser-video+json"
    )
    file_url = (
        None
        if browser_frame_video
        else f"/api/product-images/tasks/{task_id}/assets/{asset_id}/file"
    )
    analysis_valid = row["analysis_valid"]
    validation_status = "pending" if analysis_valid is None else ("valid" if analysis_valid else "invalid")
    frame_seconds = row["frame_time_seconds"]
    return {
        "id": asset_id,
        "slot": row["slot"],
        "role": row["slot"] if row["slot"] in REFERENCE_ROLES else None,
        "media_type": media_type,
        "kind": kind,
        "source_mode": "browser_frames" if browser_frame_video else "server_upload",
        "original_video_uploaded": False if browser_frame_video else media_type == "video",
        "file_name": row["file_name"],
        "file_size": row["file_size"],
        "mime_type": row["mime_type"],
        "width": row["width"],
        "height": row["height"],
        "duration_seconds": row["duration_seconds"],
        "sharpness": row["sharpness"],
        "parent_asset_id": row["parent_asset_id"],
        "video_id": row["parent_asset_id"],
        "frame_time_seconds": frame_seconds,
        "timestamp_ms": round(float(frame_seconds) * 1000) if frame_seconds is not None else None,
        "analysis_role": row["analysis_role"],
        "analysis_valid": None if row["analysis_valid"] is None else bool(row["analysis_valid"]),
        "analysis_confidence": row["analysis_confidence"],
        "analysis_reason": row["analysis_reason"],
        "file_url": file_url,
        "preview_url": file_url,
        "original_url": file_url,
        "quality_score": row["sharpness"],
        "validation_status": validation_status,
        "validation_message": row["analysis_reason"],
        "created_at": row["created_at"],
    }


def _public_task_status(value: str) -> str:
    return {
        "needs_material": "needs_input",
        "ready": "ready_to_generate",
        "analysis_failed": "failed",
        "analysis_unknown": "paused",
        "paused_failed": "paused",
        "paused_unknown": "paused",
        "paused_late_success": "paused",
    }.get(value, value)


def _public_output_status(value: str, has_source: bool = True) -> str:
    if value == "success":
        return "success"
    if not has_source:
        return "needs_source"
    if value == "stale":
        return "pending"
    return value


def _output_payload(row: Any) -> dict[str, Any]:
    task_id = str(row["task_id"])
    slot = str(row["slot"])
    variant = str(row["variant"])
    return {
        "id": int(row["id"]),
        "slot": slot,
        "variant": variant,
        "status": row["status"],
        "mime_type": row["mime_type"],
        "width": row["width"],
        "height": row["height"],
        "source_asset_id": row["source_asset_id"],
        "api_config_id": row["api_config_id"],
        "prompt": row["prompt"],
        "error_message": row["error_message"],
        "file_url": (
            f"/api/product-images/tasks/{task_id}/outputs/{slot}/{variant}/file"
            if row["file_path"]
            else None
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _call_payload(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "call_type": row["call_type"],
        "slot": row["slot"],
        "attempt_no": row["attempt_no"],
        "status": row["status"],
        "api_config_id": row["api_config_id"],
        "config_name": row["config_name"],
        # The UI uses the explicit API-prefixed name. Keep config_name for
        # backwards compatibility with early product-image clients.
        "api_config_name": row["config_name"],
        "model_name": row["model_name"],
        "endpoint_path": row["endpoint_path"],
        "error_message": row["error_message"],
        "started_at": row["started_at"],
        "unknown_at": row["unknown_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _require_task_row(conn: Any, task_id: str) -> Any:
    validate_task_id(task_id)
    row = conn.execute("SELECT * FROM product_image_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise ProductImageError("商品图任务不存在")
    return row


def _sync_reference_summary(conn: Any, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT role, selected_asset_id, status FROM product_image_references WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    selected = {
        row["role"]: int(row["selected_asset_id"])
        for row in rows
        if row["selected_asset_id"] is not None and row["status"] == "ready"
    }
    missing = [role for role in REFERENCE_ROLES if role not in selected]
    conn.execute(
        """
        UPDATE product_image_tasks
        SET selected_assets_json = ?, missing_roles_json = ?, last_activity_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (_json_dump(selected), _json_dump(missing), now_iso(), now_iso(), task_id),
    )
    return missing


def create_task(product_code: str, color: str, previous_task_id: str | None = None) -> dict[str, Any]:
    product_code = product_code.strip()
    color = color.strip()
    if not product_code:
        raise ProductImageError("商品款号不能为空")
    if not color:
        raise ProductImageError("商品颜色不能为空")
    if len(product_code) > 120 or len(color) > 120:
        raise ProductImageError("商品款号或颜色过长")

    if previous_task_id:
        with db_session() as conn:
            previous = _require_task_row(conn, previous_task_id)
            running_call = conn.execute(
                "SELECT id FROM product_image_calls WHERE task_id = ? AND status = 'running' LIMIT 1",
                (previous_task_id,),
            ).fetchone()
            if previous["generation_active"] or running_call:
                raise ProductImageConflict("上一轮仍在处理，暂时不能删除素材")

    task_id = uuid.uuid4().hex
    ts = now_iso()
    with db_session() as conn:
        version = int(
            conn.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1
                FROM product_image_tasks
                WHERE product_code = ? COLLATE NOCASE AND color = ? COLLATE NOCASE
                """,
                (product_code, color),
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO product_image_tasks (
                id, product_code, color, version, status, analysis_used,
                missing_roles_json, selected_assets_json, analysis_notes_json,
                generation_active, inputs_deleted, last_activity_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'draft', 0, ?, '{}', '{}', 0, 0, ?, ?, ?)
            """,
            (task_id, product_code, color, version, _json_dump(list(REFERENCE_ROLES)), ts, ts, ts),
        )
        conn.executemany(
            """
            INSERT INTO product_image_references
                (task_id, role, status, updated_at)
            VALUES (?, ?, 'missing', ?)
            """,
            [(task_id, role, ts) for role in REFERENCE_ROLES],
        )
        conn.executemany(
            """
            INSERT INTO product_image_outputs
                (task_id, slot, variant, status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            [
                (task_id, slot, variant, ts, ts)
                for slot in OUTPUT_SLOTS
                for variant in OUTPUT_VARIANTS
            ],
        )
    (PRODUCT_IMAGE_INPUT_DIR / task_id).mkdir(parents=True, exist_ok=True)
    (PRODUCT_IMAGE_OUTPUT_DIR / task_id).mkdir(parents=True, exist_ok=True)

    if previous_task_id:
        delete_task_sources(previous_task_id)
    prune_task_history()
    return get_task(task_id)


def get_task(task_id: str, include_calls: bool = True) -> dict[str, Any]:
    with db_session() as conn:
        task = _require_task_row(conn, task_id)
        references = conn.execute(
            """
            SELECT r.*, a.file_name, a.media_type, a.width, a.height, a.sharpness
            FROM product_image_references r
            LEFT JOIN product_image_assets a ON a.id = r.selected_asset_id
            WHERE r.task_id = ? ORDER BY r.rowid
            """,
            (task_id,),
        ).fetchall()
        assets = conn.execute(
            "SELECT * FROM product_image_assets WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        outputs = conn.execute(
            "SELECT * FROM product_image_outputs WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        calls = (
            conn.execute(
                "SELECT * FROM product_image_calls WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
            if include_calls
            else []
        )
        analysis_call = conn.execute(
            """
            SELECT started_at, created_at FROM product_image_calls
            WHERE task_id = ? AND call_type = 'analysis' ORDER BY id LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        generation_call_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM product_image_calls WHERE task_id = ? AND call_type = 'generation'",
                (task_id,),
            ).fetchone()[0]
        )
        analysis_name = None
        generation_name = None
        if task["analysis_config_id"]:
            row = conn.execute(
                "SELECT config_name FROM api_configs WHERE id = ?", (task["analysis_config_id"],)
            ).fetchone()
            analysis_name = row["config_name"] if row else None
        if task["image_config_id"]:
            row = conn.execute(
                "SELECT config_name FROM api_configs WHERE id = ?", (task["image_config_id"],)
            ).fetchone()
            generation_name = row["config_name"] if row else None

    asset_payloads = [_asset_payload(row) for row in assets]
    assets_by_id = {item["id"]: item for item in asset_payloads}
    calls_payload = [_call_payload(row) for row in calls]
    output_groups: dict[str, dict[str, Any]] = {
        slot: {"slot": slot, "reference_role": SLOT_REFERENCE_ROLE[slot], "variants": {}}
        for slot in OUTPUT_SLOTS
    }
    for row in outputs:
        output_groups[row["slot"]]["variants"][row["variant"]] = _output_payload(row)
    for group in output_groups.values():
        variants = list(group["variants"].values())
        role = group["reference_role"]
        reference = next((row for row in references if row["role"] == role), None)
        has_source = bool(reference and reference["selected_asset_id"])
        group["status"] = _public_output_status(_aggregate_output_status(variants), has_source)
        group["has_result"] = any(item.get("file_url") for item in variants)
        group["role"] = group["slot"]
        group["reference_asset_id"] = reference["selected_asset_id"] if reference else None
        highres = group["variants"].get("highres")
        size800 = group["variants"].get("800")
        if highres and highres.get("file_url") and size800 and size800.get("file_url"):
            group["current_result"] = {
                "id": highres["id"],
                "original_url": highres["file_url"],
                "size_800_url": size800["file_url"],
                "file_name": ZIP_NAMES[group["slot"]],
                "mime_type": highres.get("mime_type"),
                "width": highres.get("width"),
                "height": highres.get("height"),
                "created_at": highres.get("created_at"),
            }
        else:
            group["current_result"] = None
        slot_calls = [item for item in calls_payload if item["call_type"] == "generation" and item["slot"] == group["slot"]]
        group["attempts"] = [
            {
                "id": item["id"],
                "status": item["status"],
                "result": group["current_result"] if item["status"] == "success" and item is slot_calls[-1] else None,
                "api_config_name": item["config_name"],
                "error_message": item["error_message"],
                "created_at": item["created_at"],
            }
            for item in slot_calls
        ]
        group["error_message"] = next(
            (item.get("error_message") for item in variants if item.get("error_message")),
            None,
        )

    reference_payload = []
    sources = []
    for row in references:
        selected_id = row["selected_asset_id"]
        reference_item = {
                "role": row["role"],
                "selected_asset_id": selected_id,
                "status": row["status"],
                "selection_source": row["selection_source"],
                "confidence": row["confidence"],
                "reason": row["reason"],
                "selected_asset_url": (
                    f"/api/product-images/tasks/{task_id}/assets/{selected_id}/file"
                    if selected_id is not None
                    else None
                ),
                "file_name": row["file_name"],
                "media_type": row["media_type"],
                "width": row["width"],
                "height": row["height"],
                "sharpness": row["sharpness"],
            }
        reference_payload.append(reference_item)
        role_assets = [
            item
            for item in asset_payloads
            if item["kind"] == "image" and item.get("role") == row["role"]
        ]
        source_status = "ready" if row["selected_asset_id"] else ("empty" if not role_assets else "missing")
        sources.append(
            {
                "role": row["role"],
                "assets": role_assets,
                "selected_asset_id": row["selected_asset_id"],
                "status": source_status,
                "missing_reason": None if row["selected_asset_id"] else row["reason"],
            }
        )

    frames_by_parent: dict[int, list[dict[str, Any]]] = {}
    for item in asset_payloads:
        if item["kind"] == "video_frame" and item.get("video_id") is not None:
            frames_by_parent.setdefault(int(item["video_id"]), []).append(item)
    videos = []
    for item in asset_payloads:
        if item["kind"] != "video":
            continue
        videos.append({**item, "frames": frames_by_parent.get(int(item["id"]), [])})
    video_frames = [item for item in asset_payloads if item["kind"] == "video_frame"]
    plan = call_plan(task_id)
    zip_ready = all(
        output_groups[slot].get("current_result") and output_groups[slot].get("status") == "success"
        for slot in OUTPUT_SLOTS
    )

    return {
        "id": task["id"],
        "product_code": task["product_code"],
        "color": task["color"],
        "version": task["version"],
        "status": _public_task_status(task["status"]),
        "internal_status": task["status"],
        "analysis_config_id": task["analysis_config_id"],
        "image_config_id": task["image_config_id"],
        "analysis_api_config_id": task["analysis_config_id"],
        "analysis_api_config_name": analysis_name,
        "generation_api_config_id": task["image_config_id"],
        "generation_api_config_name": generation_name,
        "analysis_used": bool(task["analysis_used"]),
        "analysis_used_at": (
            (analysis_call["started_at"] or analysis_call["created_at"])
            if analysis_call
            else None
        ),
        "analysis_status": task["analysis_status"],
        "missing_roles": _json_load(task["missing_roles_json"], list(REFERENCE_ROLES)),
        "analysis_notes": _json_load(task["analysis_notes_json"], {}),
        "error_message": task["error_message"],
        "generation_active": bool(task["generation_active"]),
        "inputs_deleted": bool(task["inputs_deleted"]),
        "inputs_deleted_at": task["inputs_deleted_at"],
        "source_deleted_at": task["inputs_deleted_at"],
        "last_activity_at": task["last_activity_at"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "references": reference_payload,
        "sources": sources,
        "assets": asset_payloads,
        "videos": videos,
        "video_frames": video_frames,
        "outputs": [output_groups[slot] for slot in OUTPUT_SLOTS],
        "calls": calls_payload,
        "call_summary": {
            "analysis_calls_used": 1 if task["analysis_used"] else 0,
            "analysis_calls_total": 1,
            "generation_calls_used": generation_call_count,
            "generation_calls_total": 4,
            "remaining_generation_calls": plan["generation_calls"],
        },
        "zip_url": f"/api/product-images/tasks/{task_id}/download" if zip_ready else None,
        "download_url": f"/api/product-images/tasks/{task_id}/download" if zip_ready else None,
        "call_plan": plan,
    }


def _aggregate_output_status(variants: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "pending") for item in variants}
    for status in ("running", "unknown", "failed", "stale", "pending"):
        if status in statuses:
            return status
    return "success" if statuses == {"success"} else next(iter(statuses), "pending")


def list_task_history(limit: int = MAX_HISTORY_GROUPS) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), MAX_HISTORY_GROUPS))
    prune_task_history()
    with db_session() as conn:
        rows = conn.execute(
            """
            WITH recent_groups AS (
                SELECT product_code, color, MAX(created_at) AS group_created_at,
                       MAX(id) AS group_sort_id
                FROM product_image_tasks
                WHERE status != 'draft' OR analysis_used = 1 OR inputs_deleted = 1
                GROUP BY product_code COLLATE NOCASE, color COLLATE NOCASE
                ORDER BY group_created_at DESC, group_sort_id DESC
                LIMIT ?
            )
            SELECT t.*
            FROM product_image_tasks t
            JOIN recent_groups g
              ON t.product_code = g.product_code COLLATE NOCASE
             AND t.color = g.color COLLATE NOCASE
            WHERE t.status != 'draft' OR t.analysis_used = 1 OR t.inputs_deleted = 1
            ORDER BY g.group_created_at DESC, g.group_sort_id DESC,
                     t.version DESC, t.created_at DESC, t.id DESC
            """,
            (limit,),
        ).fetchall()
        task_ids = [str(row["id"]) for row in rows]
        output_rows = []
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            output_rows = conn.execute(
                f"""
                SELECT task_id, slot, variant FROM product_image_outputs
                WHERE task_id IN ({placeholders}) AND variant IN ('highres', '800')
                  AND status = 'success' AND file_path IS NOT NULL
                """,
                task_ids,
            ).fetchall()
        variants_by_task: dict[str, dict[str, set[str]]] = {}
        for output in output_rows:
            variants_by_task.setdefault(str(output["task_id"]), {}).setdefault(
                str(output["slot"]), set()
            ).add(str(output["variant"]))
        result = []
        for row in rows:
            task_variants = variants_by_task.get(str(row["id"]), {})
            completed_slots = {
                slot for slot, variants in task_variants.items() if {"highres", "800"} <= variants
            }
            preview_urls = {
                slot: f"/api/product-images/tasks/{row['id']}/outputs/{slot}/800/file"
                for slot in OUTPUT_SLOTS
                if slot in completed_slots
            }
            zip_url = (
                f"/api/product-images/tasks/{row['id']}/download"
                if all(slot in completed_slots for slot in OUTPUT_SLOTS)
                else None
            )
            result.append(
                {
                    "id": row["id"],
                    "product_code": row["product_code"],
                    "color": row["color"],
                    "version": row["version"],
                    "status": _public_task_status(row["status"]),
                    "internal_status": row["status"],
                    "analysis_status": row["analysis_status"],
                    "error_message": row["error_message"],
                    "inputs_deleted": bool(row["inputs_deleted"]),
                    "source_deleted_at": row["inputs_deleted_at"],
                    "generated_count": len(completed_slots),
                    "preview_urls": preview_urls,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "zip_url": zip_url,
                    "download_url": zip_url,
                }
            )
        return result


def _task_file_paths(conn: Any, task_id: str) -> tuple[list[str], list[str]]:
    assets = conn.execute(
        "SELECT file_path FROM product_image_assets WHERE task_id = ?", (task_id,)
    ).fetchall()
    outputs = conn.execute(
        "SELECT file_path FROM product_image_outputs WHERE task_id = ? AND file_path IS NOT NULL",
        (task_id,),
    ).fetchall()
    return (
        [str(row["file_path"]) for row in assets if row["file_path"]],
        [str(row["file_path"]) for row in outputs if row["file_path"]],
    )


def delete_task(task_id: str) -> None:
    with db_session() as conn:
        task = _require_task_row(conn, task_id)
        running = conn.execute(
            "SELECT id FROM product_image_calls WHERE task_id = ? AND status = 'running' LIMIT 1",
            (task_id,),
        ).fetchone()
        if task["generation_active"] or running:
            raise ProductImageConflict("任务仍在处理，暂时不能删除")
        input_paths, output_paths = _task_file_paths(conn, task_id)
        conn.execute("DELETE FROM product_image_tasks WHERE id = ?", (task_id,))
    for path in input_paths:
        _delete_file(path, PRODUCT_IMAGE_INPUT_DIR)
    for path in output_paths:
        _delete_file(path, PRODUCT_IMAGE_OUTPUT_DIR)
    shutil.rmtree(PRODUCT_IMAGE_INPUT_DIR / task_id, ignore_errors=True)
    shutil.rmtree(PRODUCT_IMAGE_OUTPUT_DIR / task_id, ignore_errors=True)


def delete_task_sources(task_id: str, allow_missing: bool = False) -> None:
    try:
        with db_session() as conn:
            task = _require_task_row(conn, task_id)
            running = conn.execute(
                "SELECT id FROM product_image_calls WHERE task_id = ? AND status = 'running' LIMIT 1",
                (task_id,),
            ).fetchone()
            if task["generation_active"] or running:
                raise ProductImageConflict("任务仍在处理，暂时不能删除原始素材")
            paths = [
                str(row["file_path"])
                for row in conn.execute(
                    "SELECT file_path FROM product_image_assets WHERE task_id = ?", (task_id,)
                ).fetchall()
                if row["file_path"]
            ]
            ts = now_iso()
            conn.execute("DELETE FROM product_image_assets WHERE task_id = ?", (task_id,))
            conn.execute(
                """
                UPDATE product_image_references
                SET selected_asset_id = NULL, status = 'source_deleted', updated_at = ?
                WHERE task_id = ?
                """,
                (ts, task_id),
            )
            conn.execute(
                """
                UPDATE product_image_tasks
                SET inputs_deleted = 1, inputs_deleted_at = ?, selected_assets_json = '{}',
                    missing_roles_json = ?, last_activity_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (ts, _json_dump(list(REFERENCE_ROLES)), ts, ts, task_id),
            )
        for path in paths:
            _delete_file(path, PRODUCT_IMAGE_INPUT_DIR)
        shutil.rmtree(PRODUCT_IMAGE_INPUT_DIR / task_id, ignore_errors=True)
    except ProductImageError:
        if allow_missing:
            return
        raise


def cleanup_expired_sources(hours: int = SOURCE_TTL_HOURS) -> int:
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT id FROM product_image_tasks
            WHERE inputs_deleted = 0 AND generation_active = 0
              AND COALESCE(last_activity_at, updated_at) < ?
              AND NOT EXISTS (
                  SELECT 1 FROM product_image_calls c
                  WHERE c.task_id = product_image_tasks.id AND c.status = 'running'
              )
            """,
            (cutoff,),
        ).fetchall()
    cleaned = 0
    for row in rows:
        try:
            delete_task_sources(str(row["id"]))
            cleaned += 1
        except ProductImageError:
            continue
    return cleaned


def prune_task_history(max_groups: int = MAX_HISTORY_GROUPS) -> int:
    max_groups = max(1, int(max_groups))
    with db_session() as conn:
        stale = conn.execute(
            """
            WITH ordered_groups AS (
                SELECT product_code, color, MAX(created_at) AS group_created_at,
                       MAX(id) AS group_sort_id
                FROM product_image_tasks
                WHERE status != 'draft' OR analysis_used = 1 OR inputs_deleted = 1
                GROUP BY product_code COLLATE NOCASE, color COLLATE NOCASE
            ),
            overflow_groups AS (
                SELECT product_code, color
                FROM ordered_groups
                ORDER BY group_created_at DESC, group_sort_id DESC
                LIMIT -1 OFFSET ?
            )
            SELECT t.id
            FROM product_image_tasks t
            JOIN overflow_groups old
              ON t.product_code = old.product_code COLLATE NOCASE
             AND t.color = old.color COLLATE NOCASE
            WHERE NOT EXISTS (
                SELECT 1 FROM product_image_tasks busy
                WHERE busy.product_code = t.product_code COLLATE NOCASE
                  AND busy.color = t.color COLLATE NOCASE
                  AND (
                      busy.generation_active = 1
                      OR busy.status IN ('analyzing', 'generating')
                      OR EXISTS (
                          SELECT 1 FROM product_image_calls c
                          WHERE c.task_id = busy.id AND c.status = 'running'
                      )
                  )
            )
            ORDER BY t.product_code COLLATE NOCASE, t.color COLLATE NOCASE,
                     t.version DESC, t.id DESC
            """,
            (max_groups,),
        ).fetchall()
    removed = 0
    for row in stale:
        try:
            delete_task(str(row["id"]))
            removed += 1
        except ProductImageError:
            continue
    return removed


def _insert_asset(conn: Any, task_id: str, slot: str, media_type: str, item: dict[str, Any], parent_id: int | None = None) -> int:
    path = str(item.get("file_path") or item.get("path") or "")
    if not path:
        raise ProductImageError("素材保存失败")
    cursor = conn.execute(
        """
        INSERT INTO product_image_assets (
            task_id, slot, media_type, file_name, file_path, file_size, mime_type,
            width, height, duration_seconds, sharpness, parent_asset_id,
            frame_time_seconds, created_at, analysis_valid, analysis_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            slot,
            media_type,
            item.get("file_name"),
            path,
            item.get("file_size"),
            item.get("mime_type"),
            item.get("width"),
            item.get("height"),
            item.get("duration_seconds"),
            item.get("sharpness"),
            parent_id,
            item.get("frame_time_seconds"),
            now_iso(),
            item.get("analysis_valid"),
            item.get("analysis_reason"),
        ),
    )
    return int(cursor.lastrowid)


def save_image_assets(task_id: str, role: str, files: list[UploadFile]) -> list[dict[str, Any]]:
    validate_reference_role(role)
    if not files:
        raise ProductImageError("请选择要上传的图片")
    with db_session() as conn:
        task = _require_task_row(conn, task_id)
        if task["inputs_deleted"]:
            raise ProductImageConflict("本轮原始素材已删除，请开始下一轮")
        if task["generation_active"]:
            raise ProductImageConflict("正在生成商品图，暂时不能更改素材")
        current = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM product_image_assets
                WHERE task_id = ? AND slot = ? AND media_type = 'image'
                """,
                (task_id, role),
            ).fetchone()[0]
        )
    if current + len(files) > 5:
        raise ProductImageError("每个图片入口最多上传 5 张")

    saved_items: list[dict[str, Any]] = []
    try:
        for file in files:
            saved_items.append(_save_image_upload(task_id, role, file))
        ids: list[int] = []
        with db_session() as conn:
            task = _require_task_row(conn, task_id)
            if task["inputs_deleted"] or task["generation_active"]:
                raise ProductImageConflict("任务状态已变化，请重新上传")
            for item in saved_items:
                ids.append(_insert_asset(conn, task_id, role, "image", item))
            ts = now_iso()
            conn.execute(
                "UPDATE product_image_tasks SET status = 'draft', last_activity_at = ?, updated_at = ? WHERE id = ?",
                (ts, ts, task_id),
            )
            if task["analysis_used"]:
                reference = conn.execute(
                    "SELECT selected_asset_id FROM product_image_references WHERE task_id = ? AND role = ?",
                    (task_id, role),
                ).fetchone()
                valid_ids = [
                    item_id
                    for item, item_id in zip(saved_items, ids)
                    if item.get("analysis_valid") != 0
                ]
                if reference and reference["selected_asset_id"] is None and valid_ids:
                    best_id = max(
                        valid_ids,
                        key=lambda asset_id: float(
                            next(
                                (item.get("sharpness") or 0 for item, item_id in zip(saved_items, ids) if item_id == asset_id),
                                0,
                            )
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE product_image_references
                        SET selected_asset_id = ?, status = 'ready', selection_source = 'supplemental',
                            confidence = NULL, reason = '分析后补充到指定入口，并已通过本地文件与清晰度检查', updated_at = ?
                        WHERE task_id = ? AND role = ?
                        """,
                        (best_id, ts, task_id, role),
                    )
                missing = _sync_reference_summary(conn, task_id)
                conn.execute(
                    "UPDATE product_image_tasks SET status = ? WHERE id = ?",
                    ("ready" if not missing else "needs_material", task_id),
                )
        return [get_asset(task_id, asset_id) for asset_id in ids]
    except Exception:
        for item in saved_items:
            _delete_file(item.get("file_path") or item.get("path"), PRODUCT_IMAGE_INPUT_DIR)
        raise


def save_video_assets(task_id: str, files: list[UploadFile]) -> dict[str, Any]:
    if not files:
        raise ProductImageError("请选择要上传的视频")
    if len(files) != 1:
        raise ProductImageError("为保护服务器磁盘，每次只能上传 1 个视频，请逐个上传")
    with db_session() as conn:
        task = _require_task_row(conn, task_id)
        if task["inputs_deleted"]:
            raise ProductImageConflict("本轮原始素材已删除，请开始下一轮")
        if task["generation_active"]:
            raise ProductImageConflict("正在生成商品图，暂时不能更改素材")
        current = int(
            conn.execute(
                "SELECT COUNT(*) FROM product_image_assets WHERE task_id = ? AND media_type = 'video'",
                (task_id,),
            ).fetchone()[0]
        )
    if current + len(files) > 3:
        raise ProductImageError("每个任务最多上传 3 个视频")

    prepared: list[dict[str, Any]] = []
    try:
        for file in files:
            prepared.append(_save_video_upload(task_id, file))
        video_ids: list[int] = []
        frame_ids: list[int] = []
        with db_session() as conn:
            task = _require_task_row(conn, task_id)
            if task["inputs_deleted"] or task["generation_active"]:
                raise ProductImageConflict("任务状态已变化，请重新上传")
            for bundle in prepared:
                video = bundle.get("video") or bundle
                video_id = _insert_asset(conn, task_id, "video", "video", video)
                video_ids.append(video_id)
                for frame in bundle.get("frames") or []:
                    frame_ids.append(_insert_asset(conn, task_id, "video", "frame", frame, video_id))
            ts = now_iso()
            if task["analysis_used"]:
                missing = _sync_reference_summary(conn, task_id)
                status = "ready" if not missing else "needs_material"
            else:
                status = "draft"
            conn.execute(
                "UPDATE product_image_tasks SET status = ?, last_activity_at = ?, updated_at = ? WHERE id = ?",
                (status, ts, ts, task_id),
            )
        return {
            "videos": [get_asset(task_id, asset_id) for asset_id in video_ids],
            "frames": [get_asset(task_id, asset_id) for asset_id in frame_ids],
        }
    except Exception:
        for bundle in prepared:
            video = bundle.get("video") or bundle
            _delete_file(video.get("file_path") or video.get("path"), PRODUCT_IMAGE_INPUT_DIR)
            for frame in bundle.get("frames") or []:
                _delete_file(frame.get("file_path") or frame.get("path"), PRODUCT_IMAGE_INPUT_DIR)
        raise


def save_browser_video_frames(
    task_id: str,
    original_video_name: str,
    duration_seconds: float,
    files: list[UploadFile],
    timestamps_ms: list[float] | None = None,
) -> dict[str, Any]:
    """Store browser-extracted frames without retaining the original video."""
    if not files:
        raise ProductImageError("浏览器没有提取到候选帧，请改用原视频上传")
    if len(files) > MAX_BROWSER_FRAMES:
        raise ProductImageError(f"每个视频最多上传 {MAX_BROWSER_FRAMES} 张候选帧")
    clean_name = Path(str(original_video_name or "")).name.strip()
    if not clean_name or len(clean_name) > 255:
        raise ProductImageError("原视频文件名无效")
    if Path(clean_name).suffix.lower() not in VIDEO_SUFFIXES:
        raise ProductImageError("原视频格式不支持，请选择 MP4 或 MOV")
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError) as exc:
        raise ProductImageError("原视频时长无效") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ProductImageError("原视频时长无效")
    if duration > MAX_VIDEO_SECONDS + 0.01:
        raise ProductImageError("单个视频最长 60 秒")

    if timestamps_ms is None:
        frame_seconds = [
            max(
                0.0,
                min(max(0.0, duration - 0.04), duration * (index + 0.5) / len(files)),
            )
            for index in range(len(files))
        ]
    else:
        if len(timestamps_ms) != len(files):
            raise ProductImageError("候选帧时间戳数量与图片数量不一致")
        frame_seconds = []
        for value in timestamps_ms:
            try:
                seconds = float(value) / 1000.0
            except (TypeError, ValueError) as exc:
                raise ProductImageError("候选帧时间戳无效") from exc
            if not math.isfinite(seconds) or seconds < 0 or seconds > duration + 0.05:
                raise ProductImageError("候选帧时间戳超出视频时长")
            frame_seconds.append(seconds)

    with db_session() as conn:
        task = _require_task_row(conn, task_id)
        if task["inputs_deleted"]:
            raise ProductImageConflict("本轮原始素材已删除，请开始下一轮")
        if task["generation_active"]:
            raise ProductImageConflict("正在生成商品图，暂时不能更改素材")
        current = int(
            conn.execute(
                "SELECT COUNT(*) FROM product_image_assets WHERE task_id = ? AND media_type = 'video'",
                (task_id,),
            ).fetchone()[0]
        )
    if current >= 3:
        raise ProductImageError("每个任务最多上传 3 个视频")

    video_token = f"browser_{uuid.uuid4().hex}"
    video_dir = PRODUCT_IMAGE_INPUT_DIR / task_id / "videos"
    frame_dir = PRODUCT_IMAGE_INPUT_DIR / task_id / "frames" / video_token
    metadata_path = video_dir / f"{video_token}.json"
    prepared_frames: list[dict[str, Any]] = []
    try:
        for file, timestamp in zip(files, frame_seconds):
            prepared_frames.append(
                _save_browser_frame_upload(task_id, video_token, file, timestamp)
            )

        _ensure_disk_reserve(video_dir, 4096)
        video_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "source_mode": "browser_frames",
            "original_video_name": clean_name,
            "duration_seconds": duration,
            "frame_count": len(prepared_frames),
            "original_video_uploaded": False,
        }
        temporary = video_dir / f".{video_token}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(_json_dump(metadata), encoding="utf-8")
            os.replace(temporary, metadata_path)
        finally:
            temporary.unlink(missing_ok=True)
        video_item = {
            "file_name": clean_name,
            "file_path": str(metadata_path),
            "file_size": metadata_path.stat().st_size,
            "mime_type": "application/vnd.product-image.browser-video+json",
            "duration_seconds": duration,
        }

        with db_session() as conn:
            task = _require_task_row(conn, task_id)
            if task["inputs_deleted"] or task["generation_active"]:
                raise ProductImageConflict("任务状态已变化，请重新上传")
            current = int(
                conn.execute(
                    "SELECT COUNT(*) FROM product_image_assets WHERE task_id = ? AND media_type = 'video'",
                    (task_id,),
                ).fetchone()[0]
            )
            if current >= 3:
                raise ProductImageError("每个任务最多上传 3 个视频")
            video_id = _insert_asset(conn, task_id, "video", "video", video_item)
            frame_ids = [
                _insert_asset(conn, task_id, "video", "frame", frame, video_id)
                for frame in prepared_frames
            ]
            ts = now_iso()
            if task["analysis_used"]:
                missing = _sync_reference_summary(conn, task_id)
                status = "ready" if not missing else "needs_material"
            else:
                status = "draft"
            conn.execute(
                "UPDATE product_image_tasks SET status = ?, last_activity_at = ?, updated_at = ? WHERE id = ?",
                (status, ts, ts, task_id),
            )
        return {
            "videos": [get_asset(task_id, video_id)],
            "frames": [get_asset(task_id, frame_id) for frame_id in frame_ids],
        }
    except Exception:
        metadata_path.unlink(missing_ok=True)
        for frame in prepared_frames:
            _delete_file(frame.get("file_path") or frame.get("path"), PRODUCT_IMAGE_INPUT_DIR)
        shutil.rmtree(frame_dir, ignore_errors=True)
        raise


def get_asset(task_id: str, asset_id: int) -> dict[str, Any]:
    with db_session() as conn:
        _require_task_row(conn, task_id)
        row = conn.execute(
            "SELECT * FROM product_image_assets WHERE task_id = ? AND id = ?",
            (task_id, asset_id),
        ).fetchone()
        if not row:
            raise ProductImageError("素材不存在")
        return _asset_payload(row)


def asset_file(task_id: str, asset_id: int) -> tuple[Path, str | None, str | None]:
    with db_session() as conn:
        _require_task_row(conn, task_id)
        row = conn.execute(
            "SELECT file_path, mime_type, file_name FROM product_image_assets WHERE task_id = ? AND id = ?",
            (task_id, asset_id),
        ).fetchone()
    if not row:
        raise ProductImageError("素材不存在")
    path = _safe_path(row["file_path"], PRODUCT_IMAGE_INPUT_DIR)
    if not path.is_file():
        raise ProductImageError("素材文件不存在")
    return path, row["mime_type"], row["file_name"]


def delete_asset(task_id: str, asset_id: int) -> None:
    with db_session() as conn:
        task = _require_task_row(conn, task_id)
        if task["generation_active"]:
            raise ProductImageConflict("正在生成商品图，暂时不能删除素材")
        row = conn.execute(
            "SELECT * FROM product_image_assets WHERE task_id = ? AND id = ?",
            (task_id, asset_id),
        ).fetchone()
        if not row:
            raise ProductImageError("素材不存在")
        children = conn.execute(
            "SELECT id, file_path FROM product_image_assets WHERE task_id = ? AND parent_asset_id = ?",
            (task_id, asset_id),
        ).fetchall()
        removed_ids = [asset_id, *[int(child["id"]) for child in children]]
        paths = [row["file_path"], *[child["file_path"] for child in children]]
        placeholders = ",".join("?" for _ in removed_ids)
        ts = now_iso()
        conn.execute(
            f"""
            UPDATE product_image_references
            SET selected_asset_id = NULL, status = 'missing', selection_source = NULL,
                confidence = NULL, reason = NULL, updated_at = ?
            WHERE task_id = ? AND selected_asset_id IN ({placeholders})
            """,
            [ts, task_id, *removed_ids],
        )
        conn.execute("DELETE FROM product_image_assets WHERE task_id = ? AND id = ?", (task_id, asset_id))
        missing = _sync_reference_summary(conn, task_id)
        if task["status"] not in {"completed", "paused_failed", "paused_unknown", "paused_late_success"}:
            conn.execute(
                "UPDATE product_image_tasks SET status = ? WHERE id = ?",
                ("needs_material" if task["analysis_used"] and missing else "draft", task_id),
            )
    for path in paths:
        _delete_file(path, PRODUCT_IMAGE_INPUT_DIR)


def select_reference(task_id: str, role: str, asset_id: int) -> dict[str, Any]:
    validate_reference_role(role)
    with db_session() as conn:
        task = _require_task_row(conn, task_id)
        if task["inputs_deleted"]:
            raise ProductImageConflict("本轮原始素材已删除，请开始下一轮")
        if task["generation_active"]:
            raise ProductImageConflict("正在生成商品图，暂时不能更换参考素材")
        asset = conn.execute(
            """
            SELECT * FROM product_image_assets
            WHERE task_id = ? AND id = ? AND media_type IN ('image', 'frame')
            """,
            (task_id, asset_id),
        ).fetchone()
        if not asset:
            raise ProductImageError("所选参考素材不存在")
        if asset["analysis_valid"] == 0:
            raise ProductImageError(asset["analysis_reason"] or "所选素材未通过质量检查，请补拍")
        duplicate = conn.execute(
            """
            SELECT role FROM product_image_references
            WHERE task_id = ? AND role <> ? AND selected_asset_id = ?
            LIMIT 1
            """,
            (task_id, role, asset_id),
        ).fetchone()
        if duplicate:
            raise ProductImageConflict(
                f"这张素材已被选为{REFERENCE_LABELS.get(duplicate['role'], duplicate['role'])}，"
                "同一张素材不能同时代表两个角度，请选择另一张或补拍"
            )
        previous = conn.execute(
            "SELECT selected_asset_id FROM product_image_references WHERE task_id = ? AND role = ?",
            (task_id, role),
        ).fetchone()
        ts = now_iso()
        conn.execute(
            """
            UPDATE product_image_references
            SET selected_asset_id = ?, status = 'ready', selection_source = 'user',
                confidence = NULL, reason = '用户手工选择参考素材', updated_at = ?
            WHERE task_id = ? AND role = ?
            """,
            (asset_id, ts, task_id, role),
        )
        missing = _sync_reference_summary(conn, task_id)
        conn.execute(
            "UPDATE product_image_tasks SET status = ?, error_message = NULL WHERE id = ?",
            ("ready" if not missing else "needs_material", task_id),
        )
        if previous and previous["selected_asset_id"] != asset_id:
            affected_slots = [slot for slot, source_role in SLOT_REFERENCE_ROLE.items() if source_role == role]
            placeholders = ",".join("?" for _ in affected_slots)
            conn.execute(
                f"""
                UPDATE product_image_outputs SET status = CASE WHEN file_path IS NULL THEN 'pending' ELSE 'stale' END,
                    source_asset_id = ?, updated_at = ?
                WHERE task_id = ? AND slot IN ({placeholders})
                """,
                [asset_id, ts, task_id, *affected_slots],
            )
    return get_task(task_id)


def reference_rows(task_id: str) -> dict[str, dict[str, Any]]:
    with db_session() as conn:
        task = _require_task_row(conn, task_id)
        if task["inputs_deleted"]:
            raise ProductImageConflict("本轮原始素材已删除，请开始下一轮")
        rows = conn.execute(
            """
            SELECT r.*, a.file_path, a.file_name, a.media_type, a.width, a.height, a.sharpness
            FROM product_image_references r
            LEFT JOIN product_image_assets a ON a.id = r.selected_asset_id AND a.task_id = r.task_id
            WHERE r.task_id = ?
            """,
            (task_id,),
        ).fetchall()
    return {row["role"]: dict(row) for row in rows}


def analysis_candidates(task_id: str, max_candidates: int = 40) -> list[dict[str, Any]]:
    with db_session() as conn:
        task = _require_task_row(conn, task_id)
        if task["inputs_deleted"]:
            raise ProductImageConflict("本轮原始素材已删除，请开始下一轮")
        rows = conn.execute(
            """
            SELECT * FROM product_image_assets
            WHERE task_id = ? AND media_type IN ('image', 'frame')
            ORDER BY CASE WHEN media_type = 'image' THEN 0 ELSE 1 END,
                     COALESCE(sharpness, 0) DESC, id
            """,
            (task_id,),
        ).fetchall()
    # Keep all explicitly uploaded photos first, then the sharpest video frames.
    photos = [dict(row) for row in rows if row["media_type"] == "image"]
    frames = [dict(row) for row in rows if row["media_type"] == "frame"]
    selected = photos[:max_candidates]
    remaining = max(0, max_candidates - len(selected))
    if remaining <= 0 or not frames:
        return selected
    by_video: dict[int, list[dict[str, Any]]] = {}
    for frame in frames:
        by_video.setdefault(int(frame.get("parent_asset_id") or 0), []).append(frame)
    # Round-robin prevents the sharpest single video from consuming every slot
    # in the task's one and only analysis request.
    groups = [items for _, items in sorted(by_video.items())]
    while remaining and any(groups):
        for group in groups:
            if not group or not remaining:
                continue
            selected.append(group.pop(0))
            remaining -= 1
    return selected


def apply_analysis_result(task_id: str, items: list[dict[str, Any]], notes: dict[str, Any] | None = None) -> list[str]:
    candidates = {int(item["id"]): item for item in analysis_candidates(task_id, max_candidates=1000)}
    best_by_role: dict[str, tuple[int, int, str]] = {}
    with db_session() as conn:
        _require_task_row(conn, task_id)
        for result in items:
            try:
                asset_id = int(result.get("asset_id") or 0)
            except (TypeError, ValueError):
                continue
            role = str(result.get("role") or "")
            if asset_id not in candidates or role not in REFERENCE_ROLES:
                continue
            valid = bool(result.get("valid", True))
            confidence = max(0, min(100, int(result.get("confidence") or 0)))
            reason = str(result.get("reason") or "")[:300]
            conn.execute(
                """
                UPDATE product_image_assets
                SET analysis_role = ?, analysis_valid = ?, analysis_confidence = ?, analysis_reason = ?
                WHERE task_id = ? AND id = ?
                """,
                (role, 1 if valid else 0, confidence, reason, task_id, asset_id),
            )
            if not valid:
                continue
            previous = best_by_role.get(role)
            score = confidence * 1_000_000 + int(float(candidates[asset_id].get("sharpness") or 0))
            if previous is None or score > previous[0]:
                best_by_role[role] = (score, asset_id, reason)

        ts = now_iso()
        for role in REFERENCE_ROLES:
            selected = best_by_role.get(role)
            if selected:
                conn.execute(
                    """
                    UPDATE product_image_references
                    SET selected_asset_id = ?, status = 'ready', selection_source = 'analysis',
                        confidence = ?, reason = ?, updated_at = ?
                    WHERE task_id = ? AND role = ?
                    """,
                    (
                        selected[1],
                        selected[0] // 1_000_000,
                        selected[2],
                        ts,
                        task_id,
                        role,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE product_image_references
                    SET selected_asset_id = NULL, status = 'missing', selection_source = 'analysis',
                        confidence = NULL, reason = '没有找到清晰且可信的对应角度，请补拍', updated_at = ?
                    WHERE task_id = ? AND role = ?
                    """,
                    (ts, task_id, role),
                )
        missing = _sync_reference_summary(conn, task_id)
        conn.execute(
            """
            UPDATE product_image_tasks
            SET status = ?, analysis_status = 'success', analysis_notes_json = ?,
                error_message = NULL, last_activity_at = ?, updated_at = ?
            WHERE id = ?
            """,
            ("ready" if not missing else "needs_material", _json_dump(notes or {}), ts, ts, task_id),
        )
    return missing


def upsert_output_variants(
    task_id: str,
    slot: str,
    variants: dict[str, dict[str, Any]],
    *,
    source_asset_id: int | None,
    api_config_id: int | None = None,
    prompt: str | None = None,
) -> None:
    validate_output_slot(slot)
    if not variants:
        raise ProductImageError("商品图处理结果为空")
    ts = now_iso()
    replaced_paths: list[str] = []
    with db_session() as conn:
        _require_task_row(conn, task_id)
        for variant, item in variants.items():
            if variant not in {"raw", *OUTPUT_VARIANTS}:
                continue
            path = str(item.get("file_path") or item.get("path") or "")
            if not path:
                continue
            existing = conn.execute(
                "SELECT id, file_path FROM product_image_outputs WHERE task_id = ? AND slot = ? AND variant = ?",
                (task_id, slot, variant),
            ).fetchone()
            if existing:
                if existing["file_path"] and str(existing["file_path"]) != path:
                    replaced_paths.append(str(existing["file_path"]))
                conn.execute(
                    """
                    UPDATE product_image_outputs
                    SET status = 'success', file_path = ?, mime_type = ?, width = ?, height = ?,
                        source_asset_id = ?, api_config_id = ?, prompt = ?, error_message = NULL,
                        updated_at = ? WHERE id = ?
                    """,
                    (
                        path,
                        item.get("mime_type"),
                        item.get("width"),
                        item.get("height"),
                        source_asset_id,
                        api_config_id,
                        prompt,
                        ts,
                        existing["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO product_image_outputs (
                        task_id, slot, variant, status, file_path, mime_type, width, height,
                        source_asset_id, api_config_id, prompt, created_at, updated_at
                    ) VALUES (?, ?, ?, 'success', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        slot,
                        variant,
                        path,
                        item.get("mime_type"),
                        item.get("width"),
                        item.get("height"),
                        source_asset_id,
                        api_config_id,
                        prompt,
                        ts,
                        ts,
                    ),
                )
        conn.execute(
            "UPDATE product_image_tasks SET last_activity_at = ?, updated_at = ? WHERE id = ?",
            (ts, ts, task_id),
        )
        complete_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT slot FROM product_image_outputs
                    WHERE task_id = ? AND variant IN ('highres', '800')
                      AND status = 'success' AND file_path IS NOT NULL
                    GROUP BY slot HAVING COUNT(DISTINCT variant) = 2
                )
                """,
                (task_id,),
            ).fetchone()[0]
        )
        active = conn.execute(
            "SELECT generation_active FROM product_image_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if complete_count == len(OUTPUT_SLOTS) and active and not active["generation_active"]:
            conn.execute(
                "UPDATE product_image_tasks SET status = 'completed', error_message = NULL WHERE id = ?",
                (task_id,),
            )
    for replaced in replaced_paths:
        _delete_file(replaced, PRODUCT_IMAGE_OUTPUT_DIR)


def set_output_status(task_id: str, slot: str, status: str, error_message: str | None = None) -> None:
    validate_output_slot(slot)
    with db_session() as conn:
        _require_task_row(conn, task_id)
        conn.execute(
            """
            UPDATE product_image_outputs SET status = ?, error_message = ?, updated_at = ?
            WHERE task_id = ? AND slot = ? AND variant IN ('highres', '800')
            """,
            (status, error_message, now_iso(), task_id, slot),
        )


def save_transparent_data_url(task_id: str, data_url: str) -> dict[str, Any]:
    max_encoded_chars = math.ceil(MAX_TRANSPARENT_UPLOAD_BYTES * 4 / 3) + 1024
    if len(data_url) > max_encoded_chars:
        raise ProductImageError("透明修边结果超过 40MB 限制，请使用 PNG 文件上传")
    references = reference_rows(task_id)
    source = references.get("front")
    if not source or not source.get("selected_asset_id"):
        raise ProductImageError("请先选择清晰的正面参考素材")
    task_dir = PRODUCT_IMAGE_OUTPUT_DIR / task_id / "front_transparent"
    _ensure_disk_reserve(task_dir, len(data_url.encode("utf-8")) * 2)
    task_dir.mkdir(parents=True, exist_ok=True)
    revision = uuid.uuid4().hex[:12]
    high_path = task_dir / f"highres_{revision}.png"
    size800_path = task_dir / f"800_{revision}.png"
    with MEDIA_DECODE_SEMAPHORE:
        result = _media().save_edited_transparent(data_url, high_path, size800_path)
    variants = _media_variants(result, high_path, size800_path)
    upsert_output_variants(
        task_id,
        "front_transparent",
        variants,
        source_asset_id=int(source["selected_asset_id"]),
    )
    return get_task(task_id)


def save_transparent_upload(task_id: str, file: UploadFile) -> dict[str, Any]:
    references = reference_rows(task_id)
    source = references.get("front")
    if not source or not source.get("selected_asset_id"):
        raise ProductImageError("请先选择清晰的正面参考素材")
    suffix = Path(file.filename or "transparent.png").suffix.lower()
    if suffix not in {".png", ".webp"}:
        raise ProductImageError("透明图请上传带 Alpha 通道的 PNG 或 WebP")
    task_dir = PRODUCT_IMAGE_OUTPUT_DIR / task_id / "front_transparent"
    _ensure_disk_reserve(
        task_dir,
        _upload_size_budget(
            file,
            MAX_TRANSPARENT_UPLOAD_BYTES,
            extra_bytes=MAX_TRANSPARENT_UPLOAD_BYTES * 2,
        ),
    )
    task_dir.mkdir(parents=True, exist_ok=True)
    revision = uuid.uuid4().hex[:12]
    temporary = task_dir / f".transparent_{revision}{suffix}.upload"
    high_path = task_dir / f"highres_{revision}.png"
    size800_path = task_dir / f"800_{revision}.png"
    committed = False
    try:
        _media().stream_upload(file, temporary, MAX_TRANSPARENT_UPLOAD_BYTES)
        with MEDIA_DECODE_SEMAPHORE:
            result = _media().save_transparent_file(temporary, high_path, size800_path)
        variants = _media_variants(result, high_path, size800_path)
        upsert_output_variants(
            task_id,
            "front_transparent",
            variants,
            source_asset_id=int(source["selected_asset_id"]),
        )
        committed = True
        return get_task(task_id)
    finally:
        temporary.unlink(missing_ok=True)
        if not committed:
            high_path.unlink(missing_ok=True)
            size800_path.unlink(missing_ok=True)


def render_default_transparent(task_id: str) -> None:
    references = reference_rows(task_id)
    source = references.get("front")
    if not source or not source.get("file_path"):
        raise ProductImageError("请先选择清晰的正面参考素材")
    task_dir = PRODUCT_IMAGE_OUTPUT_DIR / task_id / "front_transparent"
    source_path = _safe_path(source["file_path"], PRODUCT_IMAGE_INPUT_DIR)
    _ensure_disk_reserve(task_dir, source_path.stat().st_size * 2)
    task_dir.mkdir(parents=True, exist_ok=True)
    revision = uuid.uuid4().hex[:12]
    high_path = task_dir / f"highres_{revision}.png"
    size800_path = task_dir / f"800_{revision}.png"
    with MEDIA_DECODE_SEMAPHORE:
        result = _media().create_initial_transparent(source_path, high_path, size800_path)
    variants = _media_variants(result, high_path, size800_path)
    upsert_output_variants(
        task_id,
        "front_transparent",
        variants,
        source_asset_id=int(source["selected_asset_id"]),
    )


def render_logo_crop(task_id: str, left: float, top: float, right: float, bottom: float) -> dict[str, Any]:
    values = [float(left), float(top), float(right), float(bottom)]
    if any(value < 0 or value > 1 for value in values) or right - left < 0.02 or bottom - top < 0.02:
        raise ProductImageError("Logo 裁剪范围无效")
    references = reference_rows(task_id)
    source = references.get("logo")
    if not source or not source.get("file_path"):
        raise ProductImageError("请先选择清晰的 Logo 近照")
    task_dir = PRODUCT_IMAGE_OUTPUT_DIR / task_id / "logo_detail"
    source_path = _safe_path(source["file_path"], PRODUCT_IMAGE_INPUT_DIR)
    _ensure_disk_reserve(task_dir, source_path.stat().st_size * 2)
    task_dir.mkdir(parents=True, exist_ok=True)
    revision = uuid.uuid4().hex[:12]
    high_path = task_dir / f"highres_{revision}.jpg"
    size800_path = task_dir / f"800_{revision}.jpg"
    with MEDIA_DECODE_SEMAPHORE:
        result = _media().render_logo_crop(
            source_path,
            {"left": left, "top": top, "right": right, "bottom": bottom},
            high_path,
            size800_path,
        )
    variants = _media_variants(result, high_path, size800_path)
    upsert_output_variants(
        task_id,
        "logo_detail",
        variants,
        source_asset_id=int(source["selected_asset_id"]),
    )
    return get_task(task_id)


def ensure_local_outputs(task_id: str) -> None:
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT slot, variant, file_path, status FROM product_image_outputs
            WHERE task_id = ? AND slot = 'logo_detail'
            """,
            (task_id,),
        ).fetchall()
    available = {(row["slot"], row["variant"]) for row in rows if row["file_path"] and row["status"] == "success"}
    if not all(("logo_detail", variant) in available for variant in OUTPUT_VARIANTS):
        render_logo_crop(task_id, 0.0, 0.0, 1.0, 1.0)


def save_generated_response(task_id: str, slot: str, response: dict[str, Any], config: dict[str, Any], source_asset_id: int, prompt: str) -> None:
    from .file_service import decode_base64_image, download_image
    from .json_path_service import json_path_get

    try:
        values = [value for value in json_path_get(response, config.get("response_image_path") or "") if value]
    except ValueError as exc:
        raise ProductImageError(
            f"生图 API 返回结果中没有找到 {config.get('response_image_path') or '图片字段'}"
        ) from exc
    if not values:
        raise ProductImageError("生图 API 返回结果为空")
    image_type = str(config.get("response_image_type") or "base64").lower()
    if image_type == "base64":
        image_bytes = decode_base64_image(str(values[0]), max_bytes=40 * 1024 * 1024)
    elif image_type == "url":
        image_bytes = download_image(
            str(values[0]),
            int(config.get("timeout_seconds") or 300),
            max_bytes=40 * 1024 * 1024,
        )
    else:
        raise ProductImageError("生图 API 返回图片类型不受支持")
    task_dir = PRODUCT_IMAGE_OUTPUT_DIR / task_id / slot
    _ensure_disk_reserve(task_dir, len(image_bytes) * 3)
    task_dir.mkdir(parents=True, exist_ok=True)
    revision = uuid.uuid4().hex[:12]
    try:
        with MEDIA_DECODE_SEMAPHORE:
            with Image.open(BytesIO(image_bytes)) as source:
                source_format = str(source.format or "PNG").upper()
                raw_width, raw_height = source.size
    except Exception as exc:
        raise ProductImageError("生图 API 返回的图片无法读取") from exc
    raw_suffix, raw_mime = {
        "JPEG": (".jpg", "image/jpeg"),
        "JPG": (".jpg", "image/jpeg"),
        "PNG": (".png", "image/png"),
        "WEBP": (".webp", "image/webp"),
    }.get(source_format, (".img", "application/octet-stream"))
    raw_path = task_dir / f"api_original_{revision}{raw_suffix}"
    raw_temp = task_dir / f".{raw_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        raw_temp.write_bytes(image_bytes)
        os.replace(raw_temp, raw_path)
    finally:
        raw_temp.unlink(missing_ok=True)
    high_path = task_dir / f"highres_{revision}.jpg"
    size800_path = task_dir / f"800_{revision}.jpg"
    try:
        with MEDIA_DECODE_SEMAPHORE:
            result = _media().save_api_product_result(image_bytes, high_path, size800_path)
    except Exception:
        raw_path.unlink(missing_ok=True)
        high_path.unlink(missing_ok=True)
        size800_path.unlink(missing_ok=True)
        raise
    variants = _media_variants(result, high_path, size800_path)
    variants["raw"] = {
        "file_path": str(raw_path),
        "mime_type": raw_mime,
        "width": raw_width,
        "height": raw_height,
    }
    upsert_output_variants(
        task_id,
        slot,
        variants,
        source_asset_id=source_asset_id,
        api_config_id=int(config["id"]),
        prompt=prompt,
    )


def _media_variants(result: dict[str, Any] | None, high_path: Path, size800_path: Path) -> dict[str, dict[str, Any]]:
    """Normalize low-level media return values to database output variants."""
    result = result or {}
    if isinstance(result.get("highres"), dict) and isinstance(result.get("800"), dict):
        return {"highres": result["highres"], "800": result["800"]}
    high_meta = result.get("highres") if isinstance(result.get("highres"), dict) else {}
    small_meta = result.get("size800") if isinstance(result.get("size800"), dict) else {}
    return {
        "highres": {
            "file_path": str(high_path),
            "mime_type": high_meta.get("mime_type") or result.get("highres_mime_type") or ("image/png" if high_path.suffix.lower() == ".png" else "image/jpeg"),
            "width": high_meta.get("width") or result.get("highres_width") or result.get("width"),
            "height": high_meta.get("height") or result.get("highres_height") or result.get("height"),
        },
        "800": {
            "file_path": str(size800_path),
            "mime_type": small_meta.get("mime_type") or result.get("size800_mime_type") or ("image/png" if size800_path.suffix.lower() == ".png" else "image/jpeg"),
            "width": small_meta.get("width") or result.get("size800_width") or 800,
            "height": small_meta.get("height") or result.get("size800_height") or 800,
        },
    }


def analysis_image_data_url(path: str | Path) -> str:
    # Angle/clarity classification does not need full phone-camera resolution.
    # Bounding each image keeps a single multi-image JSON call memory-safe.
    with MEDIA_DECODE_SEMAPHORE:
        return str(_media().image_data_url(Path(path), max_long_edge=1280))


def output_file(task_id: str, slot: str, variant: str) -> tuple[Path, str | None, str]:
    validate_output_slot(slot)
    if variant not in {"raw", *OUTPUT_VARIANTS}:
        raise ProductImageError("商品图文件不存在")
    with db_session() as conn:
        _require_task_row(conn, task_id)
        row = conn.execute(
            """
            SELECT file_path, mime_type FROM product_image_outputs
            WHERE task_id = ? AND slot = ? AND variant = ?
            """,
            (task_id, slot, variant),
        ).fetchone()
    if not row or not row["file_path"]:
        raise ProductImageError("商品图文件不存在")
    path = _safe_path(row["file_path"], PRODUCT_IMAGE_OUTPUT_DIR)
    if not path.is_file():
        raise ProductImageError("商品图文件不存在")
    return path, row["mime_type"], path.name if variant == "raw" else ZIP_NAMES[slot]


def build_task_zip(task_id: str) -> Path:
    with db_session() as conn:
        task = _require_task_row(conn, task_id)
        rows = conn.execute(
            """
            SELECT slot, variant, file_path FROM product_image_outputs
            WHERE task_id = ? AND variant IN ('highres', '800')
              AND status = 'success' AND file_path IS NOT NULL
            """,
            (task_id,),
        ).fetchall()
    lookup = {(row["slot"], row["variant"]): row["file_path"] for row in rows}
    missing = [
        f"{slot}/{variant}"
        for slot in OUTPUT_SLOTS
        for variant in OUTPUT_VARIANTS
        if not lookup.get((slot, variant))
    ]
    if missing:
        raise ProductImageError("商品图尚未生成完整，暂时不能打包下载")

    root_name = (
        f"{_media().safe_path_component(str(task['product_code']), max_length=80)}_"
        f"{_media().safe_path_component(str(task['color']), max_length=80)}"
    )
    task_dir = PRODUCT_IMAGE_OUTPUT_DIR / task_id
    source_bytes = 0
    for path_value in lookup.values():
        source_path = _safe_path(path_value, PRODUCT_IMAGE_OUTPUT_DIR)
        if not source_path.is_file():
            raise ProductImageError("商品图文件缺失，请重新生成对应图片")
        source_bytes += source_path.stat().st_size
    _ensure_disk_reserve(task_dir, source_bytes)
    task_dir.mkdir(parents=True, exist_ok=True)
    target = task_dir / f"{root_name}.zip"
    temporary = task_dir / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for variant, folder in (("highres", "高清"), ("800", "800x800")):
                for slot in OUTPUT_SLOTS:
                    source = _safe_path(lookup[(slot, variant)], PRODUCT_IMAGE_OUTPUT_DIR)
                    if not source.is_file():
                        raise ProductImageError("商品图文件缺失，请重新生成对应图片")
                    archive.write(source, arcname=f"{root_name}/{folder}/{ZIP_NAMES[slot]}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def call_plan(task_id: str) -> dict[str, Any]:
    with db_session() as conn:
        task = _require_task_row(conn, task_id)
        successful = {
            row["slot"]
            for row in conn.execute(
                """
                SELECT slot FROM product_image_outputs
                WHERE task_id = ? AND variant = 'highres' AND file_path IS NOT NULL AND status = 'success'
                """,
                (task_id,),
            ).fetchall()
        }
        last_unknown = conn.execute(
            """
            SELECT u.id, u.slot, u.finished_at FROM product_image_calls u
            WHERE u.task_id = ? AND u.call_type = 'generation' AND u.status = 'unknown'
              AND NOT EXISTS (
                  SELECT 1 FROM product_image_calls later
                  WHERE later.task_id = u.task_id AND later.call_type = 'generation'
                    AND later.slot = u.slot AND later.id > u.id AND later.status = 'success'
              )
            ORDER BY u.id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
    pending = [slot for slot in API_OUTPUT_SLOTS if slot not in successful]
    return {
        "analysis_calls": 0 if task["analysis_used"] else 1,
        "generation_calls": len(pending),
        "maximum_total_calls": (0 if task["analysis_used"] else 1) + len(pending),
        "pending_generation_slots": pending,
        "unknown_retry_warning": bool(last_unknown),
        "unknown_request_still_running": bool(last_unknown and not last_unknown["finished_at"]),
    }


def mark_task_activity(task_id: str) -> None:
    with db_session() as conn:
        _require_task_row(conn, task_id)
        ts = now_iso()
        conn.execute(
            "UPDATE product_image_tasks SET last_activity_at = ?, updated_at = ? WHERE id = ?",
            (ts, ts, task_id),
        )
