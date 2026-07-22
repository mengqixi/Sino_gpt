import zipfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

from backend.services import vip_organizer_service as service


def test_jd_export_uses_separate_800_and_750_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_session_result_dir", lambda _session_id: tmp_path)
    monkeypatch.setattr(service, "_validate_slot_map", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service,
        "_render_slot_image",
        lambda *_args, **_kwargs: Image.new("RGB", (32, 32), "white"),
    )

    slots = [
        {"file_name": file_name, "image_ids": [1], "adjustments": []}
        for file_name, *_ in service.JD_SLOT_DEFINITIONS
    ]
    session_id = "a" * 32
    result = service.export_package(session_id, slots, {}, "jd")
    export_id = result["download_url"].split("/")[-2]
    zip_path = service.export_zip(session_id, export_id)

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())

    expected_800 = {
        "800/0-无logo.jpg",
        "800/1.jpg",
        "800/2.jpg",
        "800/3.jpg",
        "800/4.jpg",
        "800/5.jpg",
        "800/透明.png",
    }
    expected_750 = {f"750/{index}.jpg" for index in range(1, 6)}

    assert names == expected_800 | expected_750
    assert all(name.startswith(("800/", "750/")) for name in names)


def _dark_pixel_bbox(image: Image.Image) -> tuple[int, int, int, int]:
    pixels = np.asarray(image.convert("RGB"))
    mask = pixels.mean(axis=2) < 245
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _bright_pixel_bbox(image: Image.Image) -> tuple[int, int, int, int]:
    pixels = np.asarray(image.convert("RGB"))
    mask = pixels.mean(axis=2) > 50
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def test_jd_logo_matches_example_geometry():
    canvas_800 = Image.new("RGB", (800, 800), "white")
    service._draw_jd_elle_logo(canvas_800, canvas_800.size)
    assert _dark_pixel_bbox(canvas_800) == (32, 38, 219, 98)

    canvas_750 = Image.new("RGB", (750, 1000), "white")
    service._draw_jd_elle_logo(canvas_750, canvas_750.size)
    assert _dark_pixel_bbox(canvas_750) == (56, 45, 243, 105)


def test_jd_white_logo_uses_the_same_geometry():
    canvas_800 = Image.new("RGB", (800, 800), "#222222")
    service._draw_jd_elle_logo(canvas_800, canvas_800.size, "white")
    assert _bright_pixel_bbox(canvas_800) == (32, 38, 219, 98)

    canvas_750 = Image.new("RGB", (750, 1000), "#222222")
    service._draw_jd_elle_logo(canvas_750, canvas_750.size, "white")
    assert _bright_pixel_bbox(canvas_750) == (56, 45, 243, 105)


def test_jd_logo_color_is_stored_per_output_slot():
    slot_map = service._slot_map(
        [
            {"file_name": "0-无logo.jpg", "image_ids": [1], "adjustments": []},
            {"file_name": "1.jpg", "image_ids": [1], "adjustments": [], "logo_color": "white"},
            {"file_name": "2.jpg", "image_ids": [2], "adjustments": []},
        ],
        "jd",
    )

    assert slot_map["1.jpg"]["logo_color"] == "white"
    assert slot_map["2.jpg"]["logo_color"] == "black"


def test_jd_single_model_preview_does_not_create_vip_model_slot():
    slot_map = service._slot_map(
        [{"file_name": "1.jpg", "image_ids": [7], "adjustments": [], "logo_color": "white"}],
        "jd",
    )

    assert slot_map["1.jpg"]["image_ids"] == [7]
    assert "50.jpg" not in slot_map


def test_manual_crop_switches_cover_templates_to_contain():
    assert service._crop_aware_mode(None, "cover") == "cover"
    assert service._crop_aware_mode({"crop_y": 0.1, "crop_height": 0.8}, "cover") == "contain"


def test_401_manual_product_layer_can_move_outside_original_box():
    source = Image.new("RGBA", (20, 20), (210, 20, 20, 255))
    clipped = Image.new("RGB", (100, 100), "white")
    floating = Image.new("RGB", (100, 100), "white")
    adjustment = {"offset_y": 1.0}

    service._paste_product(clipped, source, (40, 40, 60, 60), adjustment)
    service._paste_product_floating(floating, source, (40, 40, 60, 60), adjustment)

    assert clipped.getpixel((50, 70)) == (255, 255, 255)
    assert floating.getpixel((50, 70))[0] > floating.getpixel((50, 70))[1]


def test_expanded_safe_boxes_match_editor_padding_rules():
    assert service._expanded_safe_box((120, 170, 680, 710), (800, 800)) == (76, 126, 724, 754)
    assert service._expanded_safe_box((78, 195, 323, 365), (750, 750), padding_ratio=0.035) == (52, 169, 349, 391)


def test_jd_shape_profiles_cover_extreme_and_common_handbag_proportions():
    assert service._jd_product_shape_profile(80, 200)[0] == "very_tall"
    assert service._jd_product_shape_profile(130, 200)[0] == "tall"
    assert service._jd_product_shape_profile(200, 200)[0] == "balanced"
    assert service._jd_product_shape_profile(320, 200)[0] == "wide"
    assert service._jd_product_shape_profile(430, 200)[0] == "very_wide"
    assert service._jd_product_shape_profile(200, 200, physical_ratio=3.0)[0] == "very_wide"
    assert service._jd_product_shape_profile(300, 160, physical_ratio=0.45)[0] == "tall"


def test_jd_phone_comparison_waits_for_length_and_height():
    assert not service._jd_size_dimensions_ready({})
    assert not service._jd_size_dimensions_ready({"product_length": "20"})
    assert service._jd_size_dimensions_ready({"product_length": "20", "product_height": "14"})


def test_jd_product_zoom_keeps_one_baseline_transform_for_every_shape():
    cases = [
        ((140, 360), (35, 70, 105, 310)),
        ((260, 320), (45, 70, 215, 270)),
        ((360, 250), (35, 55, 325, 205)),
        ((500, 220), (35, 60, 465, 180)),
    ]
    for size, body_box in cases:
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        base = service._jd_size_product_layout(layer, body_box, (800, 800), {"product_length": "20", "product_height": "14"}, None)
        zoomed = service._jd_size_product_layout(
            layer,
            body_box,
            (800, 800),
            {"product_length": "20", "product_height": "14"},
            {"zoom": 1.1},
        )

        assert abs(zoomed["scale"] / base["scale"] - 1.1) < 0.001
        assert zoomed["base_body_height"] == base["base_body_height"]
        assert zoomed["reference_body_bottom"] == base["reference_body_bottom"]
        assert zoomed["body_box"][2] - zoomed["body_box"][0] > base["body_box"][2] - base["body_box"][0]
        assert zoomed["body_box"][3] - zoomed["body_box"][1] > base["body_box"][3] - base["body_box"][1]


def test_product_cutout_removes_connected_light_gradient_without_losing_white_bag():
    source = Image.new("RGB", (500, 500), "white")
    pixels = np.asarray(source).copy()
    yy, xx = np.indices((500, 500))
    gradient = np.clip(250 - (1 - np.minimum(1, np.hypot(xx - 250, yy - 250) / 360)) * 15, 232, 250).astype(np.uint8)
    pixels[:, :, 0] = gradient
    pixels[:, :, 1] = gradient
    pixels[:, :, 2] = gradient
    source = Image.fromarray(pixels, "RGB")
    draw = ImageDraw.Draw(source)
    draw.rounded_rectangle((145, 180, 355, 360), radius=20, fill="#f7f7f5", outline="#252525", width=8)
    draw.rectangle((225, 155, 275, 190), fill="#252525")

    cutout = service._product_cutout(source)

    assert cutout.width < 280
    assert cutout.height < 260
    assert np.asarray(cutout.getchannel("A")).mean() > 80


def test_jd_body_measurement_excludes_sparse_handle_and_chain():
    layer = Image.new("RGBA", (360, 420), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.arc((105, 20, 255, 230), 180, 360, fill=(30, 30, 30, 255), width=12)
    draw.rectangle((65, 175, 295, 385), fill=(70, 70, 70, 255))

    left, top, right, bottom = service._jd_product_body_bbox(layer)

    assert top >= 160
    assert bottom >= 380
    assert left <= 70
    assert right >= 290


def test_jd_size_slot_keeps_labeled_front_even_for_narrow_bag():
    metrics = {
        1: {
            "id": 1, "alpha_ratio": 0.0, "foreground_ratio": 0.15,
            "bbox_ratio": 0.22, "main_component_ratio": 0.42,
            "foreground_fill_ratio": 0.82, "center_gold_ratio": 0.25,
            "sharpness": 80.0,
        },
        2: {
            "id": 2, "alpha_ratio": 0.0, "foreground_ratio": 0.15,
            "bbox_ratio": 0.22, "main_component_ratio": 1.12,
            "foreground_fill_ratio": 0.80, "center_gold_ratio": 0.05,
            "sharpness": 90.0,
        },
    }

    def rows(image_ids):
        return [metrics[image_id].copy() for image_id in image_ids]

    def classify(item):
        return ("front", [], 86, "front") if item["id"] == 1 else ("side", [], 90, "side")

    with (
        patch.object(service, "_validate_session_assets"),
        patch.object(service, "_uploaded_rows", side_effect=rows),
        patch.object(service, "_image_metrics", side_effect=lambda item: item),
        patch.object(service, "_classify_product_metrics", side_effect=classify),
        patch.object(service, "_refine_product_classifications"),
    ):
        result = service.analyze_assets("a" * 32, [1, 2], [], [], platform="jd")

    size_slot = next(slot for slot in result["slots"] if slot["file_name"] == "5.jpg")
    assert size_slot["image_ids"] == [1]


class JdOrganizerGeometryTests(unittest.TestCase):
    def test_shape_profiles(self):
        test_jd_shape_profiles_cover_extreme_and_common_handbag_proportions()

    def test_zoom_uses_one_baseline_transform(self):
        test_jd_product_zoom_keeps_one_baseline_transform_for_every_shape()

    def test_phone_comparison_dimension_gate(self):
        test_jd_phone_comparison_waits_for_length_and_height()

    def test_connected_background_cutout(self):
        test_product_cutout_removes_connected_light_gradient_without_losing_white_bag()

    def test_body_measurement_excludes_handle(self):
        test_jd_body_measurement_excludes_sparse_handle_and_chain()

    def test_narrow_front_is_not_replaced_by_side(self):
        test_jd_size_slot_keeps_labeled_front_even_for_narrow_bag()
