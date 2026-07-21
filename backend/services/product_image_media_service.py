from __future__ import annotations

import base64
import binascii
import io
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Sequence

import cv2
import numpy as np
from fastapi import UploadFile
from PIL import Image, ImageCms, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


IMAGE_UPLOAD_LIMIT_BYTES = 30 * 1024 * 1024
VIDEO_UPLOAD_LIMIT_BYTES = 500 * 1024 * 1024
MAX_VIDEO_DURATION_SECONDS = 60.0
MAX_EXTRACTED_FRAMES = 12
STREAM_CHUNK_BYTES = 1024 * 1024
QUALITY_ANALYSIS_LONG_EDGE = 900
EDITED_IMAGE_LIMIT_BYTES = 100 * 1024 * 1024
MAX_DECODE_PIXELS = 60_000_000
MAX_SEGMENTATION_LONG_EDGE = 2048

# The production host has 2 GB RAM. OpenCV otherwise creates a native worker
# pool per request, which can multiply memory usage during concurrent uploads.
try:
    cv2.setNumThreads(1)
except Exception:
    pass

_HEIC_EXTENSIONS = {".heic", ".heif"}
_HEIC_LOCK = Lock()
_HEIC_REGISTERED = False
_HEIC_REGISTRATION_ERROR: Exception | None = None
_INVALID_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class MediaValidationError(ValueError):
    """Raised when uploaded media cannot be safely decoded or normalized."""


def register_heic_support() -> bool:
    """Register Pillow's HEIC/HEIF opener once, returning whether it is usable."""

    global _HEIC_REGISTERED, _HEIC_REGISTRATION_ERROR
    if _HEIC_REGISTERED:
        return True
    with _HEIC_LOCK:
        if _HEIC_REGISTERED:
            return True
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
            _HEIC_REGISTERED = True
            _HEIC_REGISTRATION_ERROR = None
        except Exception as exc:  # Optional locally; required by production requirements.
            _HEIC_REGISTRATION_ERROR = exc
    return _HEIC_REGISTERED


# Register eagerly when the dependency is installed, while keeping ordinary JPEG
# processing available in development environments that have not installed it yet.
register_heic_support()


def _as_path(value: str | Path) -> Path:
    return Path(value).expanduser()


def _temporary_sibling(target: Path, suffix: str = ".tmp") -> Path:
    return target.with_name(f".{target.name}.{uuid.uuid4().hex}{suffix}")


def stream_upload(upload_file: UploadFile, target: str | Path, max_bytes: int) -> int:
    """Stream an ``UploadFile`` to an atomic target while enforcing a hard limit.

    The destination is not replaced until the complete upload has been read. A
    partial file is always removed when the stream is empty, oversized, or fails.
    The caller remains responsible for validating media contents after saving.
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    destination = _as_path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_sibling(destination, ".part")
    total = 0
    try:
        try:
            upload_file.file.seek(0)
        except (AttributeError, OSError):
            pass
        with temporary.open("xb") as output:
            while True:
                chunk = upload_file.file.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise MediaValidationError(f"上传文件超过大小限制：{max_bytes} 字节")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total == 0:
            raise MediaValidationError("上传文件为空")
        os.replace(temporary, destination)
        return total
    finally:
        temporary.unlink(missing_ok=True)


@lru_cache(maxsize=1)
def _srgb_profile_bytes() -> bytes | None:
    try:
        return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    except Exception:
        return None


def _convert_oriented_to_srgb(image: Image.Image, *, preserve_alpha: bool) -> Image.Image:
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_DECODE_PIXELS:
        raise MediaValidationError("图片像素过大，最长边请控制在常规手机原图范围内")
    oriented = ImageOps.exif_transpose(image)
    oriented.load()
    icc_profile = oriented.info.get("icc_profile")

    alpha: Image.Image | None = None
    if preserve_alpha and ("A" in oriented.getbands() or "transparency" in oriented.info):
        alpha = oriented.convert("RGBA").getchannel("A")

    if preserve_alpha:
        color_source = oriented.convert("RGB")
    elif "A" in oriented.getbands() or "transparency" in oriented.info:
        rgba = oriented.convert("RGBA")
        color_source = Image.new("RGB", rgba.size, "white")
        color_source.paste(rgba.convert("RGB"), mask=rgba.getchannel("A"))
    elif oriented.mode in {"RGB", "CMYK", "LAB"}:
        color_source = oriented.copy()
    else:
        color_source = oriented.convert("RGB")

    converted: Image.Image
    if isinstance(icc_profile, bytes) and icc_profile:
        try:
            source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
            converted = ImageCms.profileToProfile(
                color_source,
                source_profile,
                ImageCms.createProfile("sRGB"),
                outputMode="RGB",
            )
        except Exception:
            converted = color_source.convert("RGB")
    else:
        converted = color_source.convert("RGB")

    if not preserve_alpha:
        return converted
    result = converted.convert("RGBA")
    if alpha is not None:
        result.putalpha(alpha)
    return result


def _open_srgb(path: str | Path, *, preserve_alpha: bool = False) -> Image.Image:
    source_path = _as_path(path)
    if source_path.suffix.lower() in _HEIC_EXTENSIONS and not register_heic_support():
        detail = f"：{_HEIC_REGISTRATION_ERROR}" if _HEIC_REGISTRATION_ERROR else ""
        raise MediaValidationError(f"当前环境不能读取 HEIC/HEIF，请安装 pillow-heif{detail}")
    try:
        with Image.open(source_path) as source:
            return _convert_oriented_to_srgb(source, preserve_alpha=preserve_alpha)
    except MediaValidationError:
        raise
    except Exception as exc:
        raise MediaValidationError(f"无法读取图片：{source_path.name}") from exc


def _image_bytes_to_srgb(payload: bytes, *, preserve_alpha: bool = False) -> tuple[Image.Image, str | None]:
    if not payload:
        raise MediaValidationError("图片数据为空")
    try:
        with Image.open(io.BytesIO(payload)) as source:
            source_format = source.format
            return _convert_oriented_to_srgb(source, preserve_alpha=preserve_alpha), source_format
    except Exception as exc:
        raise MediaValidationError("图片数据无法解码") from exc


def _atomic_save_image(image: Image.Image, target: str | Path, image_format: str, **options: Any) -> Path:
    destination = _as_path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_sibling(destination)
    try:
        image.save(temporary, format=image_format, **options)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _save_jpeg(image: Image.Image, target: str | Path, *, quality: int = 95) -> Path:
    options: dict[str, Any] = {
        "quality": quality,
        "subsampling": 0,
        "optimize": True,
    }
    profile = _srgb_profile_bytes()
    if profile:
        options["icc_profile"] = profile
    return _atomic_save_image(image.convert("RGB"), target, "JPEG", **options)


def _save_png(image: Image.Image, target: str | Path) -> Path:
    options: dict[str, Any] = {"optimize": True, "compress_level": 6}
    profile = _srgb_profile_bytes()
    if profile:
        options["icc_profile"] = profile
    return _atomic_save_image(image, target, "PNG", **options)


def normalize_photo(source: str | Path, target: str | Path) -> dict[str, Any]:
    """Normalize JPEG/PNG/WebP/HEIC to an oriented, EXIF-free sRGB JPEG."""

    image = _open_srgb(source)
    output = _save_jpeg(image, target)
    return {
        "path": str(output),
        "format": "JPEG",
        "file_size": output.stat().st_size,
        **_quality_metrics_for_image(image),
    }


def _quality_metrics_for_image(image: Image.Image) -> dict[str, Any]:
    original_width, original_height = image.size
    scale = QUALITY_ANALYSIS_LONG_EDGE / max(1, max(image.size))
    analysis_size = (
        max(1, int(round(image.width * scale))),
        max(1, int(round(image.height * scale))),
    )
    if analysis_size != image.size:
        image = image.resize(analysis_size, Image.Resampling.LANCZOS)
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean_luma = float(np.mean(gray))
    contrast = float(np.std(gray))
    return {
        "width": original_width,
        "height": original_height,
        "analysis_width": analysis_size[0],
        "analysis_height": analysis_size[1],
        "analysis_long_edge": max(analysis_size),
        "laplacian_variance_900": round(sharpness, 3),
        "mean_luminance": round(mean_luma, 3),
        "luminance_stddev": round(contrast, 3),
        "underexposed_ratio": round(float(np.mean(gray <= 18)), 6),
        "overexposed_ratio": round(float(np.mean(gray >= 245)), 6),
        "shadow_clipped_ratio": round(float(np.mean(gray <= 5)), 6),
        "highlight_clipped_ratio": round(float(np.mean(gray >= 250)), 6),
    }


def quality_metrics(path: str | Path) -> dict[str, Any]:
    """Measure sharpness and exposure after normalizing the long edge to 900px."""

    return _quality_metrics_for_image(_open_srgb(path))


def _parse_fraction(value: Any) -> float:
    text = str(value or "").strip()
    if not text or text in {"N/A", "0/0"}:
        return 0.0
    try:
        return float(Fraction(text))
    except (ValueError, ZeroDivisionError):
        try:
            return float(text)
        except ValueError:
            return 0.0


def _subprocess_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _probe_video_with_ffprobe(path: Path, executable: str) -> dict[str, Any]:
    command = [
        executable,
        "-v",
        "error",
        "-nostdin",
        "-show_entries",
        (
            "format=duration:"
            "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration,nb_frames:"
            "stream_tags=rotate:stream_side_data=rotation"
        ),
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
            **_subprocess_options(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaValidationError(f"ffprobe 无法读取视频：{exc}") from exc
    if completed.returncode != 0:
        detail = re.sub(r"\s+", " ", completed.stderr or "").strip()[:240]
        raise MediaValidationError(f"ffprobe 无法读取视频{f'：{detail}' if detail else ''}")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaValidationError("ffprobe 返回了无效 JSON") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    stream = next(
        (item for item in streams or [] if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    if not stream:
        raise MediaValidationError("文件中没有可读取的视频流")

    fps = _parse_fraction(stream.get("avg_frame_rate")) or _parse_fraction(stream.get("r_frame_rate"))
    durations = [payload.get("format", {}).get("duration"), stream.get("duration")]
    duration = next((_parse_fraction(item) for item in durations if _parse_fraction(item) > 0), 0.0)
    if duration <= 0 and fps > 0:
        try:
            duration = float(stream.get("nb_frames") or 0) / fps
        except (TypeError, ValueError):
            duration = 0.0

    rotation = 0
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    try:
        rotation = int(round(float(tags.get("rotate") or 0)))
    except (TypeError, ValueError):
        rotation = 0
    for item in stream.get("side_data_list") or []:
        if isinstance(item, dict) and item.get("rotation") is not None:
            try:
                rotation = int(round(float(item["rotation"])))
            except (TypeError, ValueError):
                pass

    return {
        "path": str(path),
        "duration_seconds": float(duration),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": round(float(fps), 6),
        "codec_name": str(stream.get("codec_name") or ""),
        "rotation_degrees": rotation,
        "backend": "ffprobe",
        "file_size": path.stat().st_size,
    }


def _probe_video_with_opencv(path: Path) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise MediaValidationError("OpenCV 无法打开视频")
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = frame_count / fps if fps > 0 and frame_count > 0 else 0.0
        if duration <= 0:
            capture.set(cv2.CAP_PROP_POS_AVI_RATIO, 1)
            duration = float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0) / 1000.0
        fourcc_value = int(capture.get(cv2.CAP_PROP_FOURCC) or 0)
        codec = "".join(chr((fourcc_value >> (8 * index)) & 0xFF) for index in range(4)).strip("\x00")
        rotation = 0
        orientation_property = getattr(cv2, "CAP_PROP_ORIENTATION_META", None)
        if orientation_property is not None:
            rotation = int(round(float(capture.get(orientation_property) or 0)))
        return {
            "path": str(path),
            "duration_seconds": duration,
            "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
            "fps": round(fps, 6),
            "codec_name": codec,
            "rotation_degrees": rotation,
            "backend": "opencv",
            "file_size": path.stat().st_size,
        }
    finally:
        capture.release()


def _validate_video_info(info: dict[str, Any], max_duration_seconds: float) -> dict[str, Any]:
    duration = float(info.get("duration_seconds") or 0)
    if duration <= 0:
        raise MediaValidationError("无法确定视频时长")
    if duration > max_duration_seconds + 0.05:
        raise MediaValidationError(
            f"视频时长 {duration:.2f} 秒，超过 {max_duration_seconds:g} 秒限制"
        )
    if int(info.get("width") or 0) <= 0 or int(info.get("height") or 0) <= 0:
        raise MediaValidationError("无法读取视频尺寸")
    return info


def probe_video(
    path: str | Path,
    max_duration_seconds: float = MAX_VIDEO_DURATION_SECONDS,
) -> dict[str, Any]:
    """Probe and duration-check a video, preferring ffprobe over OpenCV."""

    source = _as_path(path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise MediaValidationError("视频文件不存在或为空")
    if max_duration_seconds <= 0:
        raise ValueError("max_duration_seconds must be positive")

    errors: list[str] = []
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            info = _probe_video_with_ffprobe(source, ffprobe)
        except MediaValidationError as exc:
            errors.append(str(exc))
        else:
            return _validate_video_info(info, max_duration_seconds)
    try:
        return _validate_video_info(_probe_video_with_opencv(source), max_duration_seconds)
    except MediaValidationError as exc:
        errors.append(str(exc))
    raise MediaValidationError("；".join(errors) or "无法读取视频")


def _extract_frame_with_ffmpeg(
    video_path: Path,
    timestamp: float,
    target: Path,
    executable: str,
) -> None:
    temporary = _temporary_sibling(target, ".jpg")
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-threads",
        "1",
        "-y",
        str(temporary),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=35,
            check=False,
            **_subprocess_options(),
        )
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            detail = re.sub(rb"\s+", b" ", completed.stderr or b"").decode("utf-8", "replace")[:200]
            raise MediaValidationError(f"ffmpeg 抽帧失败{f'：{detail}' if detail else ''}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _extract_frame_with_opencv(video_path: Path, timestamp: float, target: Path) -> None:
    capture = cv2.VideoCapture(str(video_path))
    try:
        if not capture.isOpened():
            raise MediaValidationError("OpenCV 无法打开视频进行抽帧")
        orientation_auto = getattr(cv2, "CAP_PROP_ORIENTATION_AUTO", None)
        if orientation_auto is not None:
            capture.set(orientation_auto, 1)
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000.0)
        ok, frame = capture.read()
        if not ok or frame is None:
            raise MediaValidationError(f"无法提取 {timestamp:.2f} 秒的视频帧")
        encoded, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 94])
        if not encoded:
            raise MediaValidationError("视频帧 JPEG 编码失败")
        temporary = _temporary_sibling(target, ".jpg")
        try:
            temporary.write_bytes(data.tobytes())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        capture.release()


def _extract_frame_at_time(video_path: Path, timestamp: float, target: Path) -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            _extract_frame_with_ffmpeg(video_path, timestamp, target, ffmpeg)
            return "ffmpeg"
        except MediaValidationError:
            target.unlink(missing_ok=True)
    _extract_frame_with_opencv(video_path, timestamp, target)
    return "opencv"


def _normalize_extracted_frame(path: Path, max_long_edge: int) -> dict[str, Any]:
    image = _open_srgb(path)
    if max(image.size) > max_long_edge:
        image.thumbnail((max_long_edge, max_long_edge), Image.Resampling.LANCZOS)
    quality = _quality_metrics_for_image(image)
    _save_jpeg(image, path, quality=94)
    return quality


def extract_video_frames(
    path: str | Path,
    out_dir: str | Path,
    max_frames: int = MAX_EXTRACTED_FRAMES,
    *,
    max_long_edge: int = 2400,
) -> list[dict[str, Any]]:
    """Extract at most 12 temporally stratified, oriented JPEG frames."""

    if max_frames <= 0:
        raise ValueError("max_frames must be positive")
    if max_long_edge < QUALITY_ANALYSIS_LONG_EDGE:
        raise ValueError(f"max_long_edge must be at least {QUALITY_ANALYSIS_LONG_EDGE}")
    source = _as_path(path)
    info = probe_video(source)
    duration = float(info["duration_seconds"])
    frame_count = min(MAX_EXTRACTED_FRAMES, int(max_frames), max(1, int(math.ceil(duration))))
    interval = duration / frame_count
    timestamps = [min(duration - 0.001, interval * (index + 0.5)) for index in range(frame_count)]
    output_dir = _as_path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, timestamp in enumerate(timestamps, start=1):
        target = output_dir / f"frame_{index:02d}_{int(round(timestamp * 1000)):06d}.jpg"
        try:
            backend = _extract_frame_at_time(source, timestamp, target)
            quality = _normalize_extracted_frame(target, max_long_edge)
            frames.append(
                {
                    "index": index,
                    "timestamp_seconds": round(timestamp, 3),
                    "path": str(target),
                    "width": quality["width"],
                    "height": quality["height"],
                    "backend": backend,
                    "quality": quality,
                }
            )
        except Exception as exc:
            target.unlink(missing_ok=True)
            errors.append(f"{timestamp:.2f}s: {exc}")
    if not frames:
        raise MediaValidationError("视频抽帧全部失败：" + "；".join(errors[:3]))
    return frames


def _contact_font() -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", 17)
    except OSError:
        return ImageFont.load_default()


def make_contact_sheet(
    frames: Sequence[Mapping[str, Any] | str | Path],
    out_path: str | Path,
) -> Path:
    """Render numbered frame thumbnails into a compact four-column sheet."""

    if not frames:
        raise MediaValidationError("没有可用于联系表的视频帧")
    columns = min(4, len(frames))
    rows = int(math.ceil(len(frames) / columns))
    cell_width, cell_height, label_height = 320, 260, 34
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "#ecefed")
    draw = ImageDraw.Draw(sheet)
    font = _contact_font()
    for position, item in enumerate(frames):
        if isinstance(item, Mapping):
            frame_path = _as_path(str(item.get("path") or ""))
            frame_index = int(item.get("index") or position + 1)
            timestamp = float(item.get("timestamp_seconds") or 0)
        else:
            frame_path = _as_path(item)
            frame_index = position + 1
            timestamp = 0.0
        image = _open_srgb(frame_path)
        fitted = ImageOps.contain(
            image,
            (cell_width - 16, cell_height - label_height - 16),
            Image.Resampling.LANCZOS,
        )
        column, row = position % columns, position // columns
        left, top = column * cell_width, row * cell_height
        draw.rectangle((left + 4, top + 4, left + cell_width - 5, top + cell_height - 5), fill="white", outline="#c9cfcb")
        paste_x = left + (cell_width - fitted.width) // 2
        paste_y = top + 8 + (cell_height - label_height - 16 - fitted.height) // 2
        sheet.paste(fitted, (paste_x, paste_y))
        minutes = int(timestamp // 60)
        seconds = timestamp - minutes * 60
        label = f"F{frame_index:02d}  {minutes:02d}:{seconds:04.1f}"
        draw.text((left + 10, top + cell_height - label_height + 5), label, fill="#18221d", font=font)
    target = _as_path(out_path)
    if target.suffix.lower() == ".png":
        return _save_png(sheet, target)
    return _save_jpeg(sheet, target, quality=90)


def image_data_url(path: str | Path, max_long_edge: int | None = None) -> str:
    """Encode an image as a MIME-correct data URL, optionally downscaled."""

    source = _as_path(path)
    if max_long_edge is not None and int(max_long_edge) > 0:
        image = _open_srgb(source)
        limit = int(max_long_edge)
        if max(image.size) > limit:
            image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=86, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    mime_type = mimetypes.guess_type(source.name)[0]
    try:
        with Image.open(source) as image:
            format_to_mime = {
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
                "HEIF": "image/heif",
                "HEIC": "image/heic",
            }
            mime_type = format_to_mime.get(str(image.format or "").upper(), mime_type)
    except Exception:
        pass
    mime_type = mime_type if mime_type and mime_type.startswith("image/") else "application/octet-stream"
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _white_800(image: Image.Image) -> Image.Image:
    rendered = ImageOps.contain(image.convert("RGB"), (800, 800), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (800, 800), "white")
    canvas.paste(rendered, ((800 - rendered.width) // 2, (800 - rendered.height) // 2))
    return canvas


def save_api_product_result(
    image_bytes: bytes,
    high_path: str | Path,
    size800_path: str | Path,
) -> dict[str, Any]:
    """Decode an API result and save native-resolution JPG plus 800px contain JPG."""

    image, source_format = _image_bytes_to_srgb(image_bytes)
    high = _save_jpeg(image, high_path)
    size800 = _save_jpeg(_white_800(image), size800_path)
    return {
        "high_path": str(high),
        "size800_path": str(size800),
        "width": image.width,
        "height": image.height,
        "size800_width": 800,
        "size800_height": 800,
        "source_format": source_format,
        "format": "JPEG",
    }


def _initial_white_alpha(image: Image.Image) -> tuple[Image.Image, float]:
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    height, width = rgb.shape[:2]
    border_width = max(2, min(width, height) // 80)
    border = np.concatenate(
        [
            rgb[:border_width, :, :].reshape(-1, 3),
            rgb[-border_width:, :, :].reshape(-1, 3),
            rgb[:, :border_width, :].reshape(-1, 3),
            rgb[:, -border_width:, :].reshape(-1, 3),
        ],
        axis=0,
    )
    background_color = np.median(border, axis=0)
    if float(np.mean(background_color)) < 170:
        raise MediaValidationError("正面图边缘不是白色或浅色背景，无法安全自动抠图")

    distance = np.linalg.norm(rgb.astype(np.float32) - background_color.astype(np.float32), axis=2)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    background_candidate = (
        (distance <= 22)
        | ((distance <= 52) & (hsv[:, :, 1] <= 34) & (hsv[:, :, 2] >= 170))
    ).astype(np.uint8)
    _, labels = cv2.connectedComponents(background_candidate, connectivity=8)
    border_labels = np.unique(
        np.concatenate(
            [labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]
        )
    )
    border_labels = border_labels[border_labels > 0]
    if border_labels.size == 0:
        raise MediaValidationError("无法从图片边缘识别白色背景")
    background = np.isin(labels, border_labels)
    foreground = (~background).astype(np.uint8)
    foreground = cv2.morphologyEx(
        foreground,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=1,
    )

    count, component_labels, stats, _ = cv2.connectedComponentsWithStats(foreground, 8)
    minimum_area = max(6, int(width * height * 0.000002))
    cleaned = np.zeros_like(foreground)
    for index in range(1, count):
        if int(stats[index, cv2.CC_STAT_AREA]) >= minimum_area:
            cleaned[component_labels == index] = 1
    ratio = float(np.mean(cleaned))
    if ratio < 0.002:
        raise MediaValidationError("没有识别到可用的商品主体")
    if ratio > 0.95:
        raise MediaValidationError("背景与商品无法可靠分离，请改用更干净的白底正面图")

    alpha = cv2.GaussianBlur((cleaned * 255).astype(np.uint8), (0, 0), 0.65)
    rgba = Image.fromarray(rgb, "RGB").convert("RGBA")
    rgba.putalpha(Image.fromarray(alpha, "L"))
    return rgba, ratio


def _initial_grabcut_alpha(image: Image.Image) -> tuple[Image.Image, float]:
    """Build a conservative editable mask for ordinary phone-photo backgrounds.

    GrabCut is only a local first draft. If it cannot find a stable foreground,
    every real source pixel stays opaque so the manual editor can erase the
    background without losing or inventing any product detail.
    """

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    height, width = rgb.shape[:2]
    if width < 8 or height < 8:
        rgba = Image.fromarray(rgb, "RGB").convert("RGBA")
        return rgba, 1.0

    inset = max(1, min(width, height) // 100)
    rect_width = width - inset * 2
    rect_height = height - inset * 2
    if rect_width < 2 or rect_height < 2:
        rgba = Image.fromarray(rgb, "RGB").convert("RGBA")
        return rgba, 1.0

    mask = np.full((height, width), cv2.GC_BGD, dtype=np.uint8)
    background_model = np.zeros((1, 65), dtype=np.float64)
    foreground_model = np.zeros((1, 65), dtype=np.float64)
    try:
        cv2.grabCut(
            cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
            mask,
            (inset, inset, rect_width, rect_height),
            background_model,
            foreground_model,
            5,
            cv2.GC_INIT_WITH_RECT,
        )
        foreground = np.isin(mask, (cv2.GC_FGD, cv2.GC_PR_FGD)).astype(np.uint8)
        foreground = cv2.morphologyEx(
            foreground,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            iterations=1,
        )
        ratio = float(np.mean(foreground))
    except cv2.error:
        foreground = np.ones((height, width), dtype=np.uint8)
        ratio = 1.0

    # An unstable automatic mask must not destroy real straps, hardware or Logo
    # pixels. An opaque draft is safe and remains fully editable in the UI.
    if ratio < 0.002 or ratio > 0.98:
        foreground = np.ones((height, width), dtype=np.uint8)
        ratio = 1.0

    alpha = cv2.GaussianBlur((foreground * 255).astype(np.uint8), (0, 0), 0.65)
    rgba = Image.fromarray(rgb, "RGB").convert("RGBA")
    rgba.putalpha(Image.fromarray(alpha, "L"))
    return rgba, ratio


def _transparent_800(image: Image.Image) -> Image.Image:
    rendered = ImageOps.contain(image.convert("RGBA"), (800, 800), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
    canvas.alpha_composite(rendered, ((800 - rendered.width) // 2, (800 - rendered.height) // 2))
    return canvas


def _transparent_result(
    image: Image.Image,
    high_path: str | Path,
    size800_path: str | Path,
    *,
    foreground_ratio: float | None = None,
) -> dict[str, Any]:
    rgba = image.convert("RGBA")
    high = _save_png(rgba, high_path)
    size800 = _save_png(_transparent_800(rgba), size800_path)
    alpha = np.asarray(rgba.getchannel("A"), dtype=np.uint8)
    ratio = float(np.mean(alpha > 12)) if foreground_ratio is None else foreground_ratio
    return {
        "high_path": str(high),
        "size800_path": str(size800),
        "width": rgba.width,
        "height": rgba.height,
        "size800_width": 800,
        "size800_height": 800,
        "format": "PNG",
        "foreground_ratio": round(ratio, 6),
        "alpha_bbox": rgba.getchannel("A").getbbox(),
    }


def create_initial_transparent(
    source_path: str | Path,
    high_path: str | Path,
    size800_path: str | Path,
) -> dict[str, Any]:
    """Create an editable transparent PNG pair from a real front photo."""

    source = _open_srgb(source_path)
    mask_source = source.copy()
    if max(mask_source.size) > MAX_SEGMENTATION_LONG_EDGE:
        mask_source.thumbnail(
            (MAX_SEGMENTATION_LONG_EDGE, MAX_SEGMENTATION_LONG_EDGE),
            Image.Resampling.LANCZOS,
        )
    segmentation_mode = "white_background"
    try:
        transparent, ratio = _initial_white_alpha(mask_source)
    except MediaValidationError:
        transparent, ratio = _initial_grabcut_alpha(mask_source)
        segmentation_mode = "grabcut" if ratio < 0.98 else "manual_opaque"
    if transparent.size != source.size:
        alpha = transparent.getchannel("A").resize(source.size, Image.Resampling.LANCZOS)
        transparent = source.convert("RGBA")
        transparent.putalpha(alpha)
    result = _transparent_result(
        transparent,
        high_path,
        size800_path,
        foreground_ratio=ratio,
    )
    result["segmentation_mode"] = segmentation_mode
    result["manual_review_required"] = True
    return result


def _decode_image_data_url(data_url: str) -> bytes:
    match = re.fullmatch(
        r"data:image/[a-zA-Z0-9.+-]+(?:;[^,]*)?;base64,(.+)",
        data_url.strip(),
        flags=re.DOTALL,
    )
    if not match:
        raise MediaValidationError("透明修边结果必须是 base64 图片 data URL")
    encoded = re.sub(r"\s+", "", match.group(1))
    if len(encoded) > math.ceil(EDITED_IMAGE_LIMIT_BYTES * 4 / 3) + 8:
        raise MediaValidationError("透明修边结果过大")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MediaValidationError("透明修边结果 base64 无法解码") from exc
    if len(payload) > EDITED_IMAGE_LIMIT_BYTES:
        raise MediaValidationError("透明修边结果过大")
    return payload


def save_edited_transparent(
    data_url: str,
    high_path: str | Path,
    size800_path: str | Path,
) -> dict[str, Any]:
    """Persist a user's RGBA canvas result as native and 800px PNG variants."""

    payload = _decode_image_data_url(data_url)
    try:
        with Image.open(io.BytesIO(payload)) as source:
            has_alpha = "A" in source.getbands() or "transparency" in source.info
            if not has_alpha:
                raise MediaValidationError("透明修边结果不含 Alpha 通道")
            image = _convert_oriented_to_srgb(source, preserve_alpha=True)
    except MediaValidationError:
        raise
    except Exception as exc:
        raise MediaValidationError("透明修边结果不是可读取的图片") from exc
    return _transparent_result(image, high_path, size800_path)


def save_transparent_file(
    source_path: str | Path,
    high_path: str | Path,
    size800_path: str | Path,
) -> dict[str, Any]:
    """Persist a multipart-uploaded RGBA PNG without base64 request copies."""

    image = _open_srgb(source_path, preserve_alpha=True)
    if image.mode != "RGBA" or image.getchannel("A").getbbox() is None:
        raise MediaValidationError("透明 PNG 内容无效")
    return _transparent_result(image, high_path, size800_path)


def _normalized_crop(crop: Mapping[str, Any] | Sequence[float]) -> tuple[float, float, float, float]:
    if isinstance(crop, Mapping):
        left = crop.get("left", crop.get("x"))
        top = crop.get("top", crop.get("y"))
        if crop.get("right") is not None:
            right = crop.get("right")
        else:
            right = float(left or 0) + float(crop.get("width") or 0)
        if crop.get("bottom") is not None:
            bottom = crop.get("bottom")
        else:
            bottom = float(top or 0) + float(crop.get("height") or 0)
        values = (left, top, right, bottom)
    else:
        if len(crop) != 4:
            raise MediaValidationError("Logo 裁剪坐标必须包含四个值")
        values = tuple(crop)
    try:
        left, top, right, bottom = (float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise MediaValidationError("Logo 裁剪坐标无效") from exc
    left, top = max(0.0, min(1.0, left)), max(0.0, min(1.0, top))
    right, bottom = max(0.0, min(1.0, right)), max(0.0, min(1.0, bottom))
    if right - left < 0.01 or bottom - top < 0.01:
        raise MediaValidationError("Logo 裁剪区域过小或方向错误")
    return left, top, right, bottom


def _square_crop_box(
    image_size: tuple[int, int],
    normalized: tuple[float, float, float, float],
) -> tuple[int, int, int, int]:
    width, height = image_size
    left, top, right, bottom = normalized
    pixel_left = int(math.floor(left * width))
    pixel_top = int(math.floor(top * height))
    pixel_right = int(math.ceil(right * width))
    pixel_bottom = int(math.ceil(bottom * height))
    selection_width = max(1, pixel_right - pixel_left)
    selection_height = max(1, pixel_bottom - pixel_top)
    side = min(max(selection_width, selection_height), width, height)
    center_x = (pixel_left + pixel_right) / 2
    center_y = (pixel_top + pixel_bottom) / 2
    square_left = int(round(center_x - side / 2))
    square_top = int(round(center_y - side / 2))
    square_left = max(0, min(width - side, square_left))
    square_top = max(0, min(height - side, square_top))
    return square_left, square_top, square_left + side, square_top + side


def render_logo_crop(
    source_path: str | Path,
    crop: Mapping[str, Any] | Sequence[float],
    high_path: str | Path,
    size800_path: str | Path,
) -> dict[str, Any]:
    """Render a normalized square Logo crop with conservative non-generative enhancement."""

    source = _open_srgb(source_path)
    normalized = _normalized_crop(crop)
    crop_box = _square_crop_box(source.size, normalized)
    if crop_box[2] - crop_box[0] < 32:
        raise MediaValidationError("Logo 裁剪后的有效分辨率过低")
    logo = source.crop(crop_box).convert("RGB")
    logo = ImageEnhance.Contrast(logo).enhance(1.03)
    logo = logo.filter(ImageFilter.UnsharpMask(radius=1.1, percent=85, threshold=3))
    high = _save_jpeg(logo, high_path, quality=96)
    logo800 = logo.resize((800, 800), Image.Resampling.LANCZOS)
    size800 = _save_jpeg(logo800, size800_path, quality=96)
    return {
        "high_path": str(high),
        "size800_path": str(size800),
        "width": logo.width,
        "height": logo.height,
        "size800_width": 800,
        "size800_height": 800,
        "format": "JPEG",
        "crop_box": crop_box,
        "normalized_crop": normalized,
    }


def safe_path_component(value: str, max_length: int = 96) -> str:
    """Return a cross-platform-safe single path/ZIP component."""

    if max_length < 8:
        raise ValueError("max_length must be at least 8")
    cleaned = unicodedata.normalize("NFKC", str(value or ""))
    cleaned = _INVALID_COMPONENT.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    cleaned = re.sub(r"\.{2,}", "_", cleaned)
    cleaned = cleaned.strip(" ._")
    if not cleaned:
        cleaned = "未命名"
    cleaned = cleaned[:max_length].rstrip(" .") or "未命名"
    reserved_stem = cleaned.split(".", 1)[0].upper()
    if reserved_stem in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def safe_zip_arcname(*components: str) -> str:
    """Join sanitized components using the ZIP-standard forward slash."""

    return "/".join(safe_path_component(component) for component in components)


__all__ = [
    "EDITED_IMAGE_LIMIT_BYTES",
    "IMAGE_UPLOAD_LIMIT_BYTES",
    "MAX_EXTRACTED_FRAMES",
    "MAX_VIDEO_DURATION_SECONDS",
    "MediaValidationError",
    "VIDEO_UPLOAD_LIMIT_BYTES",
    "create_initial_transparent",
    "extract_video_frames",
    "image_data_url",
    "make_contact_sheet",
    "normalize_photo",
    "probe_video",
    "quality_metrics",
    "register_heic_support",
    "render_logo_crop",
    "safe_path_component",
    "safe_zip_arcname",
    "save_api_product_result",
    "save_edited_transparent",
    "save_transparent_file",
    "stream_upload",
]
