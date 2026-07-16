import unittest
from unittest.mock import patch

from PIL import Image

from backend.services.vip_organizer_service import (
    BUNDLED_FONT_PATH,
    _classify_product_metrics,
    _detail_showcase_page,
    _font,
    _model_showcase_page,
    analyze_assets,
)


def metrics(**overrides):
    values = {
        "alpha_ratio": 0.0,
        "foreground_ratio": 0.15,
        "foreground_fill_ratio": 0.7,
        "bbox_ratio": 0.22,
        "object_ratio": 1.2,
        "sharpness": 1000.0,
        "edge_ratio": 0.05,
        "center_gold_ratio": 0.02,
    }
    values.update(overrides)
    return values


class VipOrganizerClassificationTests(unittest.TestCase):
    def test_high_confidence_primary_roles(self):
        cases = [
            (metrics(alpha_ratio=0.4), "transparent"),
            (metrics(object_ratio=2.8, bbox_ratio=0.2), "bottom"),
            (metrics(object_ratio=0.28, foreground_ratio=0.05), "strap"),
            (metrics(object_ratio=0.45, foreground_ratio=0.07, bbox_ratio=0.1), "side"),
            (metrics(object_ratio=0.82, foreground_ratio=0.1, bbox_ratio=0.2), "top"),
            (metrics(object_ratio=1.18, foreground_fill_ratio=0.72, bbox_ratio=0.22), "semi_side"),
        ]
        for sample, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_classify_product_metrics(sample)[0], expected)

    def test_closeups_receive_multiple_detail_tags(self):
        role, tags, _, _ = _classify_product_metrics(metrics(
            foreground_ratio=0.75,
            foreground_fill_ratio=0.75,
            bbox_ratio=1.0,
            sharpness=800,
            center_gold_ratio=0.3,
        ))
        self.assertEqual(role, "detail")
        self.assertIn("interior", tags)
        self.assertIn("inner_pocket_label", tags)
        self.assertNotIn("hardware", tags)

    def test_full_views_use_logo_hint_but_keep_low_back_confidence(self):
        front_role, front_tags, _, _ = _classify_product_metrics(metrics(object_ratio=1.31, center_gold_ratio=0.02))
        back_role, _, back_confidence, _ = _classify_product_metrics(metrics(object_ratio=1.31, center_gold_ratio=0.002))
        self.assertEqual(front_role, "front")
        self.assertIn("logo", front_tags)
        self.assertEqual(back_role, "back")
        self.assertLess(back_confidence, 60)

    def test_export_templates_have_bundled_chinese_font_and_fixed_white_frames(self):
        self.assertTrue(BUNDLED_FONT_PATH.exists())
        self.assertEqual(getattr(_font(24), "path", ""), str(BUNDLED_FONT_PATH))

        source = Image.new("RGB", (320, 480), "#b52226")
        model_page = _model_showcase_page(source)
        detail_page = _detail_showcase_page(source)
        self.assertEqual(model_page.size, (750, 750))
        self.assertEqual(detail_page.size, (750, 750))
        self.assertEqual(model_page.getpixel((20, 20)), (255, 255, 255))
        self.assertEqual(model_page.getpixel((100, 100)), (181, 34, 38))
        self.assertEqual(detail_page.getpixel((20, 300)), (255, 255, 255))
        self.assertEqual(detail_page.getpixel((100, 300)), (181, 34, 38))

    def test_slot_selection_keeps_semi_side_separate_from_front(self):
        samples = [
            {"id": 1, **metrics(object_ratio=1.19, foreground_ratio=0.16, foreground_fill_ratio=0.75, bbox_ratio=0.215)},
            {"id": 2, **metrics(object_ratio=1.31, foreground_ratio=0.16, foreground_fill_ratio=0.72, bbox_ratio=0.229)},
            {"id": 3, **metrics(object_ratio=1.26, foreground_ratio=0.15, foreground_fill_ratio=0.73, bbox_ratio=0.212, center_gold_ratio=0.006)},
            {"id": 4, **metrics(object_ratio=0.80, foreground_ratio=0.10, foreground_fill_ratio=0.51, bbox_ratio=0.193, center_gold_ratio=0.14)},
            {"id": 5, **metrics(alpha_ratio=0.4, object_ratio=1.31, bbox_ratio=0.67)},
        ]
        for sample in samples:
            sample.update({"file_name": f"{sample['id']}.jpg", "file_path": f"{sample['id']}.jpg"})
        lookup = {sample["id"]: sample for sample in samples}

        with (
            patch("backend.services.vip_organizer_service._validate_session_assets"),
            patch("backend.services.vip_organizer_service._uploaded_rows", side_effect=lambda ids: [lookup[item] for item in ids]),
            patch("backend.services.vip_organizer_service._image_metrics", side_effect=lambda row: dict(row)),
        ):
            result = analyze_assets("session", [1, 2, 3, 4, 5], [], [])

        slots = {slot["file_name"]: slot["image_ids"] for slot in result["slots"]}
        self.assertEqual(slots["2.jpg"], [1])
        self.assertEqual(slots["401.jpg"], [1])
        self.assertEqual(slots["606.jpg"], [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
