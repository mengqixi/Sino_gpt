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


def _refine_subject_mask(rgb: np.ndarray, coarse: np.ndarray) -> np.ndarray:
    cv2 = _require_cv2()
    binary = (coarse > 0).astype(np.uint8) * 255
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return binary

    main_contours = [contour for contour in contours if cv2.contourArea(contour) >= max(80, rgb.shape[0] * rgb.shape[1] * 0.002)]
    if not main_contours:
        return binary
    x, y, w, h = cv2.boundingRect(np.vstack(main_contours))
    pad = max(8, min(rgb.shape[:2]) // 80)
    x = max(1, x - pad)
    y = max(1, y - pad)
    w = min(rgb.shape[1] - x - 2, w + pad * 2)
    h = min(rgb.shape[0] - y - 2, h + pad * 2)
    if w <= 2 or h <= 2:
        return binary

    grabcut_mask = np.full(binary.shape, cv2.GC_BGD, dtype=np.uint8)
    dilate_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    erode_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    probable_fg = cv2.dilate(binary, dilate_kernel, iterations=1) > 0
    sure_fg = cv2.erode(binary, erode_kernel, iterations=1) > 0
    grabcut_mask[probable_fg] = cv2.GC_PR_FGD
    grabcut_mask[sure_fg] = cv2.GC_FGD
    grabcut_mask[:y, :] = cv2.GC_BGD
    grabcut_mask[y + h :, :] = cv2.GC_BGD
    grabcut_mask[:, :x] = cv2.GC_BGD
    grabcut_mask[:, x + w :] = cv2.GC_BGD

    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), grabcut_mask, (x, y, w, h), bgd_model, fgd_model, 3, cv2.GC_INIT_WITH_MASK)
    except Exception:
        return binary

    refined = np.where((grabcut_mask == cv2.GC_FGD) | (grabcut_mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    refined = cv2.bitwise_and(refined, cv2.dilate(binary, dilate_kernel, iterations=1))
    contours, hierarchy = cv2.findContours(refined, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    clean = np.zeros_like(refined)
    if hierarchy is not None:
        min_area = max(80, int(rgb.shape[0] * rgb.shape[1] * 0.002))
        for index, contour in enumerate(contours):
            area = cv2.contourArea(contour)
            if area < min_area:
                continue
            parent = hierarchy[0][index][3]
            cv2.drawContours(clean, [contour], -1, 0 if parent >= 0 else 255, thickness=-1)
    if not np.any(clean):
        return binary
    edge_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, edge_kernel, iterations=1)
    clean = cv2.medianBlur(clean, 3)
    return clean


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
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    clean = np.zeros_like(mask)
    min_area = max(80, int(width * height * 0.002))
    if hierarchy is not None:
        for index, contour in enumerate(contours):
            area = cv2.contourArea(contour)
            if area < min_area:
                continue
            parent = hierarchy[0][index][3]
            cv2.drawContours(clean, [contour], -1, 0 if parent >= 0 else 255, thickness=-1)
    clean = _refine_subject_mask(rgb, clean)
    return Image.fromarray(clean, mode="L").filter(ImageFilter.GaussianBlur(0.7))


def _hardware_mask(image: Image.Image, subject: Image.Image) -> Image.Image:
    cv2 = _require_cv2()
    rgb = np.array(image)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue = hsv[:, :, 0]
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    subject_raw = (np.array(subject) > 40).astype(np.uint8) * 255
    subject_core = subject_raw > 0
    focus_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31))
    subject_arr = cv2.dilate(subject_raw, focus_kernel, iterations=1) > 0
    interior_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    subject_interior = cv2.erode(subject_raw, interior_kernel, iterations=1) > 0

    ys, xs = np.where(subject_core)
    logo_region = np.zeros_like(subject_core, dtype=bool)
    if len(xs):
        min_x, max_x = int(xs.min()), int(xs.max())
        min_y, max_y = int(ys.min()), int(ys.max())
        logo_region[
            int(min_y + (max_y - min_y) * 0.5) : int(min_y + (max_y - min_y) * 0.9) + 1,
            int(min_x + (max_x - min_x) * 0.2) : int(min_x + (max_x - min_x) * 0.8) + 1,
        ] = True

    gold_hue = (
        (hue >= 6)
        & (hue <= 52)
        & (saturation >= 65)
        & (value >= 70)
        & (rgb[:, :, 1] >= rgb[:, :, 2] + 8)
        & (rgb[:, :, 0] <= rgb[:, :, 1].astype(np.int16) * 1.7)
    )
    warm_highlight = (
        (rgb[:, :, 0] > 135)
        & (rgb[:, :, 1] > 105)
        & (rgb[:, :, 2] < 115)
        & (rgb[:, :, 1] >= rgb[:, :, 2] + 8)
        & (rgb[:, :, 0] <= rgb[:, :, 1].astype(np.int16) * 1.7)
    )
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 55, 150)
    local_gray = cv2.GaussianBlur(gray, (0, 0), sigmaX=9)
    bright_detail = (
        subject_interior
        & logo_region
        & (value >= 145)
        & (saturation <= 150)
        & (gray.astype(np.int16) - local_gray.astype(np.int16) >= 28)
    )

    zipper_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 2))
    horizontal_edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, zipper_kernel)
    metal_line = subject_arr & (horizontal_edges > 0) & gold_hue

    small_metal_mask = (subject_arr & (gold_hue | warm_highlight)).astype(np.uint8) * 255
    logo_mask = (bright_detail.astype(np.uint8) * 255)
    zipper_mask = (metal_line.astype(np.uint8) * 255)

    contours, _ = cv2.findContours(small_metal_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    clean = np.zeros_like(small_metal_mask)
    image_area = rgb.shape[0] * rgb.shape[1]
    for contour in contours:
        area = cv2.contourArea(contour)
        if 2 <= area <= image_area * 0.015:
            cv2.drawContours(clean, [contour], -1, 255, thickness=-1)

    # Join nearby bright glyphs into compact Logo/metal-badge candidates without merging stitch lines.
    logo_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
    logo_mask = cv2.morphologyEx(logo_mask, cv2.MORPH_CLOSE, logo_kernel)
    contours, _ = cv2.findContours(logo_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        area = cv2.contourArea(contour)
        x, y, w, h = cv2.boundingRect(contour)
        compact_logo = (
            3 <= area <= image_area * 0.004
            and w >= max(8, h * 1.6)
            and w <= rgb.shape[1] * 0.14
            and h <= rgb.shape[0] * 0.05
        )
        if compact_logo:
            cv2.drawContours(clean, [contour], -1, 255, thickness=-1)

    contours, _ = cv2.findContours(zipper_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        area = cv2.contourArea(contour)
        x, y, w, h = cv2.boundingRect(contour)
        is_zipper_like = w >= h * 5 and 8 <= w and h <= max(8, rgb.shape[0] * 0.025)
        if is_zipper_like and area <= image_area * 0.03:
            cv2.drawContours(clean, [contour], -1, 255, thickness=-1)
    kernel = np.ones((3, 3), np.uint8)
    clean = cv2.dilate(clean, kernel, iterations=2)
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
    subject_alpha = np.array(_data_url_to_mask(subject_mask, image.size)).astype(np.float32) / 255.0
    protect_alpha = np.array(_data_url_to_mask(protect_mask, image.size)).astype(np.float32) / 255.0
    recolor_alpha = np.clip(subject_alpha * (1.0 - protect_alpha), 0.0, 1.0)
    if not np.any(recolor_alpha > 0.08):
        raise ValueError("没有可调色区域，请先识别主体或用画笔修正遮罩。")

    rgb = np.array(image).astype(np.float32)
    luminance = (0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]) / 255.0
    shade = 0.28 + luminance[..., None] * 0.95
    recolored = np.clip(target[None, None, :] * shade, 0, 255)
    texture = rgb - luminance[..., None] * 255.0
    recolored = np.clip(recolored + texture * 0.18, 0, 255)

    result = rgb.copy()
    alpha = (recolor_alpha * 0.86)[..., None]
    result = rgb * (1 - alpha) + recolored * alpha
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
