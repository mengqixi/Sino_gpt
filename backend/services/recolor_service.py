import base64
import re
import uuid
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from ..config import RESULT_DIR
from .file_service import public_url_for


SEGMENTATION_BACKEND = "opencv"


def _require_cv2():
    try:
        import cv2  # type: ignore

        return cv2
    except Exception as exc:
        raise ValueError("本地调色需要安装 opencv-python，请先安装依赖或使用 Docker 运行。") from exc


def _load_rgb(path: str) -> Image.Image:
    return Image.open(path).convert("RGB")


def _mask_to_data_url(mask: Image.Image) -> str:
    buffer = BytesIO()
    mask.convert("L").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _image_to_data_url(image: Image.Image) -> str:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _data_url_to_mask(value: str, size: tuple[int, int]) -> Image.Image:
    payload = re.sub(r"^data:image/[^;]+;base64,", "", value.strip())
    data = base64.b64decode(payload)
    return Image.open(BytesIO(data)).convert("L").resize(size)


def _hex_to_rgb(value: str) -> np.ndarray:
    color = value.strip().lstrip("#")
    if len(color) != 6:
        raise ValueError("目标颜色必须是 6 位 HEX，例如 #d63a2f")
    return np.array([int(color[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def _subject_mask(image: Image.Image) -> Image.Image:
    cv2 = _require_cv2()
    rgb = np.array(image)
    height, width = rgb.shape[:2]
    border = max(6, min(width, height) // 40)
    samples = np.concatenate(
        [
            rgb[:border, :, :].reshape(-1, 3),
            rgb[-border:, :, :].reshape(-1, 3),
            rgb[:, :border, :].reshape(-1, 3),
            rgb[:, -border:, :].reshape(-1, 3),
        ],
        axis=0,
    )
    bg = np.median(samples, axis=0)
    distance = np.linalg.norm(rgb.astype(np.float32) - bg.astype(np.float32), axis=2)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    saturation = hsv[:, :, 1]
    mask = ((distance > 24) | (saturation > 34) | (gray < 225)).astype(np.uint8) * 255
    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    clean = np.zeros_like(mask)
    min_area = max(80, int(width * height * 0.002))
    for contour in contours:
        if cv2.contourArea(contour) >= min_area:
            cv2.drawContours(clean, [contour], -1, 255, thickness=-1)
    return Image.fromarray(clean, mode="L").filter(ImageFilter.GaussianBlur(1.2))


def _hardware_mask(image: Image.Image, subject: Image.Image) -> Image.Image:
    cv2 = _require_cv2()
    rgb = np.array(image)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue = hsv[:, :, 0]
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    subject_arr = np.array(subject) > 40

    gold_hue = ((hue >= 10) & (hue <= 42) & (saturation >= 45) & (value >= 95))
    warm_highlight = (rgb[:, :, 0] > 135) & (rgb[:, :, 1] > 105) & (rgb[:, :, 2] < 115)
    mask = (subject_arr & (gold_hue | warm_highlight)).astype(np.uint8) * 255
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.dilate(mask, kernel, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    clean = np.zeros_like(mask)
    image_area = rgb.shape[0] * rgb.shape[1]
    for contour in contours:
        area = cv2.contourArea(contour)
        if 4 <= area <= image_area * 0.08:
            cv2.drawContours(clean, [contour], -1, 255, thickness=-1)
    return Image.fromarray(clean, mode="L").filter(ImageFilter.GaussianBlur(0.8))


def analyze_recolor_masks(image_path: str) -> dict:
    image = _load_rgb(image_path)
    subject = _subject_mask(image)
    hardware = _hardware_mask(image, subject)
    overlay = image.convert("RGBA")
    subject_overlay = Image.new("RGBA", image.size, (37, 120, 90, 80))
    hardware_overlay = Image.new("RGBA", image.size, (244, 183, 64, 135))
    overlay = Image.composite(subject_overlay, overlay, subject)
    overlay = Image.composite(hardware_overlay, overlay, hardware)
    return {
        "segmentation_backend": SEGMENTATION_BACKEND,
        "subject_mask": _mask_to_data_url(subject),
        "protect_mask": _mask_to_data_url(hardware),
        "overlay_preview": _image_to_data_url(overlay),
    }


def render_recolor_image(image_path: str, target_color: str, subject_mask: str, protect_mask: str) -> Image.Image:
    image = _load_rgb(image_path)
    target = _hex_to_rgb(target_color)
    subject = np.array(_data_url_to_mask(subject_mask, image.size)) > 24
    protect = np.array(_data_url_to_mask(protect_mask, image.size)) > 24
    recolor_area = subject & ~protect
    if not np.any(recolor_area):
        raise ValueError("没有可调色区域，请先识别主体或用画笔修正遮罩。")

    rgb = np.array(image).astype(np.float32)
    luminance = (0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]) / 255.0
    shade = 0.28 + luminance[..., None] * 0.95
    recolored = np.clip(target[None, None, :] * shade, 0, 255)
    texture = rgb - luminance[..., None] * 255.0
    recolored = np.clip(recolored + texture * 0.18, 0, 255)

    result = rgb.copy()
    alpha = 0.86
    result[recolor_area] = rgb[recolor_area] * (1 - alpha) + recolored[recolor_area] * alpha
    return Image.fromarray(np.clip(result, 0, 255).astype(np.uint8), mode="RGB")


def preview_recolor(image_path: str, target_color: str, subject_mask: str, protect_mask: str) -> dict:
    result_image = render_recolor_image(image_path, target_color, subject_mask, protect_mask)
    return {"preview_image": _image_to_data_url(result_image)}


def apply_recolor(image_path: str, target_color: str, subject_mask: str, protect_mask: str) -> str:
    result_image = render_recolor_image(image_path, target_color, subject_mask, protect_mask)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    output = RESULT_DIR / f"recolor_{uuid.uuid4().hex[:12]}.png"
    result_image.save(output)
    return str(output)


def result_payload(path: str) -> dict:
    return {"image_path": path, "image_url": public_url_for(Path(path))}
