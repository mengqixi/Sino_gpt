import base64
import io
import mimetypes
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import UploadFile
from PIL import Image

from ..config import ALLOWED_IMAGE_EXTENSIONS, ALLOWED_MIME_TYPES, RESULT_DIR, UPLOAD_DIR
from ..database import db_session, now_iso


def public_url_for(path: str | Path) -> str:
    p = Path(path)
    if p.parent.name == "uploads":
        return f"/uploads/{p.name}"
    if p.parent.name == "results":
        return f"/results/{p.name}"
    return str(path)


def validate_image_name_and_type(filename: str, content_type: str | None) -> None:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("图片格式不支持，请上传 jpg、jpeg、png 或 webp")
    if content_type and content_type not in ALLOWED_MIME_TYPES:
        guessed = mimetypes.guess_type(filename)[0]
        if guessed not in ALLOWED_MIME_TYPES:
            raise ValueError("图片 MIME 类型不支持")


def _insert_uploaded_record(file_name: str, path: Path, mime_type: str | None, width: int, height: int) -> int:
    stat = path.stat()
    with db_session() as conn:
        cursor = conn.execute(
            """
            INSERT INTO uploaded_images (file_name, file_path, file_size, mime_type, width, height, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (file_name, str(path), stat.st_size, mime_type, width, height, now_iso()),
        )
        return int(cursor.lastrowid)


def save_upload(file: UploadFile) -> dict:
    validate_image_name_and_type(file.filename or "", file.content_type)
    ext = Path(file.filename or "image.png").suffix.lower()
    safe_name = f"{uuid.uuid4().hex}{ext}"
    target = UPLOAD_DIR / safe_name
    with target.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        with Image.open(target) as img:
            width, height = img.size
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise ValueError("图片文件无法读取，请确认文件未损坏") from exc
    image_id = _insert_uploaded_record(file.filename or safe_name, target, file.content_type, width, height)
    return {
        "image_id": image_id,
        "file_name": file.filename,
        "file_path": str(target),
        "preview_url": public_url_for(target),
        "width": width,
        "height": height,
    }


def save_existing_image_as_upload(source_path: str, file_name: str | None = None) -> dict:
    source = Path(source_path)
    if not source.exists():
        raise ValueError("源图片不存在")
    ext = source.suffix.lower() if source.suffix.lower() in ALLOWED_IMAGE_EXTENSIONS else ".png"
    safe_name = f"{uuid.uuid4().hex}{ext}"
    target = UPLOAD_DIR / safe_name
    shutil.copyfile(source, target)
    try:
        with Image.open(target) as img:
            width, height = img.size
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise ValueError("源图片无法读取") from exc
    mime_type = mimetypes.guess_type(str(target))[0] or "image/png"
    original_name = file_name or source.name
    image_id = _insert_uploaded_record(original_name, target, mime_type, width, height)
    return {
        "image_id": image_id,
        "file_name": original_name,
        "file_path": str(target),
        "preview_url": public_url_for(target),
        "width": width,
        "height": height,
    }


def encode_image_as_data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/png"
    data = Path(path).read_bytes()
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def strip_data_url_prefix(value: str) -> str:
    return re.sub(r"^data:image/[^;]+;base64,", "", value.strip())


def save_result_bytes(job_id: int, index: int, image_bytes: bytes, suffix: str = ".png") -> str:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULT_DIR / f"job_{job_id}_{index + 1}_{uuid.uuid4().hex[:8]}{suffix}"
    path.write_bytes(image_bytes)
    return str(path)


def _trim_light_border(image: Image.Image, tolerance: int = 18, padding: int = 8) -> Image.Image:
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    sample_points = [
        (0, 0),
        (width - 1, 0),
        (0, height - 1),
        (width - 1, height - 1),
        (width // 2, 0),
        (width // 2, height - 1),
        (0, height // 2),
        (width - 1, height // 2),
    ]
    bg = tuple(sum(pixels[x, y][i] for x, y in sample_points) // len(sample_points) for i in range(3))

    def is_background(pixel: tuple[int, int, int]) -> bool:
        return all(abs(pixel[i] - bg[i]) <= tolerance for i in range(3)) or all(channel >= 238 for channel in pixel)

    left = 0
    while left < width and all(is_background(pixels[left, y]) for y in range(height)):
        left += 1
    right = width - 1
    while right > left and all(is_background(pixels[right, y]) for y in range(height)):
        right -= 1
    top = 0
    while top < height and all(is_background(pixels[x, top]) for x in range(width)):
        top += 1
    bottom = height - 1
    while bottom > top and all(is_background(pixels[x, bottom]) for x in range(width)):
        bottom -= 1

    if right <= left or bottom <= top:
        return image
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(width - 1, right + padding)
    bottom = min(height - 1, bottom + padding)
    crop_box = (left, top, right + 1, bottom + 1)
    if crop_box == (0, 0, width, height):
        return image
    return image.crop(crop_box)


def split_image_grid(source_path: str, job_id: int) -> list[str]:
    source = Path(source_path)
    if not source.exists():
        raise ValueError("待分割图片不存在")
    with Image.open(source) as img:
        width, height = img.size
        mid_x = width // 2
        mid_y = height // 2
        boxes = [
            (0, 0, mid_x, mid_y),
            (mid_x, 0, width, mid_y),
            (0, mid_y, mid_x, height),
            (mid_x, mid_y, width, height),
        ]
        paths: list[str] = []
        for index, box in enumerate(boxes):
            crop = _trim_light_border(img.crop(box))
            path = RESULT_DIR / f"job_{job_id}_split_{index + 1}_{uuid.uuid4().hex[:8]}.png"
            crop.save(path)
            paths.append(str(path))
        return paths


def crop_generated_image(source_path: str, job_id: int, left: float, top: float, right: float, bottom: float) -> str:
    source = Path(source_path)
    if not source.exists():
        raise ValueError("待裁剪图片不存在")
    left = max(0.0, min(1.0, float(left)))
    top = max(0.0, min(1.0, float(top)))
    right = max(0.0, min(1.0, float(right)))
    bottom = max(0.0, min(1.0, float(bottom)))
    if right - left < 0.02 or bottom - top < 0.02:
        raise ValueError("裁剪区域过小，请重新框选")
    with Image.open(source) as img:
        width, height = img.size
        box = (
            int(width * left),
            int(height * top),
            max(int(width * right), int(width * left) + 1),
            max(int(height * bottom), int(height * top) + 1),
        )
        output = RESULT_DIR / f"job_{job_id}_crop_{uuid.uuid4().hex[:8]}.png"
        img.crop(box).save(output)
    return str(output)


def decode_base64_image(value: str, max_bytes: int | None = None) -> bytes:
    encoded = strip_data_url_prefix(value)
    if max_bytes is not None and len(encoded) > ((int(max_bytes) + 2) // 3) * 4 + 8:
        raise ValueError("返回图片超过服务器允许的大小")
    try:
        decoded = base64.b64decode(encoded, validate=False)
    except Exception as exc:
        raise ValueError("base64 图片解码失败") from exc
    if max_bytes is not None and len(decoded) > int(max_bytes):
        raise ValueError("返回图片超过服务器允许的大小")
    return decoded


def download_image(url: str, timeout: int, max_bytes: int | None = None) -> bytes:
    try:
        with requests.get(url, timeout=timeout, stream=True) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if max_bytes is not None and content_length:
                try:
                    if int(content_length) > int(max_bytes):
                        raise ValueError("返回图片超过服务器允许的大小")
                except ValueError as exc:
                    if "超过服务器" in str(exc):
                        raise
            output = io.BytesIO()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if max_bytes is not None and output.tell() + len(chunk) > int(max_bytes):
                    raise ValueError("返回图片超过服务器允许的大小")
                output.write(chunk)
            return output.getvalue()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("图片 URL 下载失败") from exc


def suffix_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in ALLOWED_IMAGE_EXTENSIONS else ".png"
