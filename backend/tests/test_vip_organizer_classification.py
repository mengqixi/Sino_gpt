import unittest
from unittest.mock import patch

from PIL import Image, ImageChops

from backend.services.vip_organizer_service import (
    BUNDLED_FONT_PATH,
    _catalog_product_page,
    _classify_product_metrics,
    _detail_showcase_page,
    _api_analysis_prompt,
    _font,
    _model_showcase_page,
    _paste_product,
    _refine_product_classifications,
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
    @staticmethod
    def _small_catalog_product() -> Image.Image:
        image = Image.new("RGB", (800, 800), "white")
        for x in range(300, 500):
            for y in range(340, 460):
                image.putpixel((x, y), (190, 115, 140))
        return image

    @staticmethod
    def _large_catalog_product() -> Image.Image:
        image = Image.new("RGB", (800, 800), "white")
        for x in range(40, 760):
            for y in range(184, 616):
                image.putpixel((x, y), (190, 115, 140))
        return image

    def test_catalog_product_is_cropped_upscaled_and_visually_aligned(self):
        rendered = _catalog_product_page(self._small_catalog_product())
        foreground = ImageChops.difference(rendered, Image.new("RGB", rendered.size, "white")).getbbox()

        self.assertIsNotNone(foreground)
        assert foreground is not None
        self.assertGreaterEqual(foreground[2] - foreground[0], 530)
        self.assertAlmostEqual((foreground[0] + foreground[2]) / 2, 400, delta=2)
        self.assertAlmostEqual((foreground[1] + foreground[3]) / 2, 440, delta=2)

    def test_catalog_product_normalizes_small_and_large_source_scale(self):
        white = Image.new("RGB", (800, 800), "white")
        small_bbox = ImageChops.difference(_catalog_product_page(self._small_catalog_product()), white).getbbox()
        large_bbox = ImageChops.difference(_catalog_product_page(self._large_catalog_product()), white).getbbox()

        self.assertIsNotNone(small_bbox)
        self.assertIsNotNone(large_bbox)
        assert small_bbox is not None and large_bbox is not None
        for small_edge, large_edge in zip(small_bbox, large_bbox):
            self.assertAlmostEqual(small_edge, large_edge, delta=3)
        self.assertGreaterEqual(large_bbox[0], 120)
        self.assertLessEqual(large_bbox[2], 680)
        self.assertGreaterEqual(large_bbox[1], 170)
        self.assertLessEqual(large_bbox[3], 710)

    def test_template_product_box_allows_upscaling(self):
        canvas = Image.new("RGB", (750, 665), "white")
        _paste_product(canvas, self._small_catalog_product(), (378, 270, 665, 470))
        foreground = ImageChops.difference(canvas, Image.new("RGB", canvas.size, "white")).getbbox()

        self.assertIsNotNone(foreground)
        assert foreground is not None
        self.assertGreaterEqual(foreground[2] - foreground[0], 270)
        self.assertAlmostEqual((foreground[0] + foreground[2]) / 2, 521.5, delta=2)

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

    def test_full_views_use_logo_hint_but_keep_unknown_face_low_confidence(self):
        front_role, front_tags, _, _ = _classify_product_metrics(metrics(object_ratio=1.31, center_gold_ratio=0.02))
        unknown_role, _, unknown_confidence, _ = _classify_product_metrics(metrics(object_ratio=1.31, center_gold_ratio=0.002))
        self.assertEqual(front_role, "front")
        self.assertIn("logo", front_tags)
        self.assertEqual(unknown_role, "front")
        self.assertLess(unknown_confidence, 60)

    def test_batch_refinement_uses_depth_edges_without_mistaking_front_accessories(self):
        samples = [
            {
                "id": 1,
                "suggested_role": "front",
                "suggested_tags": [],
                "role_confidence": 76,
                "main_component_ratio": 1.03,
                "main_component_fill_ratio": 0.60,
                "main_body_side_edge_ratio": 11.5,
                "strict_center_gold_ratio": 0.002,
                "bbox_ratio": 0.41,
            },
            {
                "id": 2,
                "suggested_role": "front",
                "suggested_tags": [],
                "role_confidence": 76,
                "main_component_ratio": 0.94,
                "main_component_fill_ratio": 0.61,
                "main_body_side_edge_ratio": 2.15,
                "strict_center_gold_ratio": 0.004,
                "bbox_ratio": 0.44,
            },
            {
                "id": 3,
                "suggested_role": "front",
                "suggested_tags": ["logo", "hardware"],
                "role_confidence": 76,
                "main_component_ratio": 1.03,
                "main_component_fill_ratio": 0.60,
                "main_body_side_edge_ratio": 0.85,
                "strict_center_gold_ratio": 0.009,
                "bbox_ratio": 0.42,
            },
            {
                "id": 4,
                "suggested_role": "front",
                "suggested_tags": [],
                "role_confidence": 58,
                "main_component_ratio": 1.04,
                "main_component_fill_ratio": 0.61,
                "main_body_side_edge_ratio": 99.0,
                "strict_center_gold_ratio": 0.0,
                "bbox_ratio": 0.40,
            },
        ]

        _refine_product_classifications(samples)

        roles = {sample["id"]: sample["suggested_role"] for sample in samples}
        self.assertEqual(roles[2], "semi_side")
        self.assertEqual(roles[3], "front")
        self.assertEqual(roles[4], "back")

    def test_api_analysis_prompt_matches_local_roles_and_detail_tags(self):
        prompt = _api_analysis_prompt()

        for role in ("front", "semi_side", "side", "back", "top", "bottom", "transparent", "strap", "detail"):
            self.assertIn(role, prompt)
        for tag in (
            "logo",
            "hardware",
            "strap_chain",
            "zipper_opening",
            "interior",
            "inner_pocket_label",
            "material_texture",
            "bottom_detail",
        ):
            self.assertIn(tag, prompt)
        self.assertIn("同批相对校正", prompt)
        self.assertIn("不得改为detail", prompt)
        self.assertIn("ELLE金属Logo面料近景", prompt)

    def test_export_templates_have_bundled_chinese_font_and_fixed_white_frames(self):
        self.assertTrue(BUNDLED_FONT_PATH.exists())
        self.assertEqual(getattr(_font(24), "path", ""), str(BUNDLED_FONT_PATH))
        self.assertEqual(_font(24).getname()[1], "Regular")
        self.assertEqual(_font(24, True).getname()[1], "Bold")

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
        self.assertEqual(slots["606.jpg"], [2, 1, 3, 4])


if __name__ == "__main__":
    unittest.main()
