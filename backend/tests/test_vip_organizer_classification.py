import unittest
from unittest.mock import patch

from PIL import Image, ImageChops

from backend.services.vip_organizer_service import (
    BUNDLED_FONT_PATH,
    INFO_LENGTH_LINE_Y,
    INFO_PRODUCT_BOX,
    _catalog_product_page,
    _classify_product_metrics,
    _detail_showcase_page,
    _api_analysis_prompt,
    _font,
    _model_showcase_page,
    _normalized_product_page,
    _paste_product,
    _refine_product_classifications,
    _render_slot_image,
    _slot_map,
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

    def test_all_non_model_page_sizes_use_normalized_safe_areas(self):
        source = self._small_catalog_product()
        white_800 = Image.new("RGB", (800, 800), "white")
        white_750 = Image.new("RGB", (750, 750), "white")

        standard_bbox = ImageChops.difference(_normalized_product_page(source), white_800).getbbox()
        tag_bbox = ImageChops.difference(
            _normalized_product_page(source, size=(750, 750), box=(90, 105, 660, 665)),
            white_750,
        ).getbbox()
        transparent = _normalized_product_page(source, transparent=True)

        self.assertIsNotNone(standard_bbox)
        self.assertIsNotNone(tag_bbox)
        assert standard_bbox is not None and tag_bbox is not None
        self.assertGreaterEqual(standard_bbox[0], 120)
        self.assertLessEqual(standard_bbox[2], 680)
        self.assertGreaterEqual(tag_bbox[0], 90)
        self.assertLessEqual(tag_bbox[2], 660)
        self.assertEqual(transparent.mode, "RGBA")
        self.assertEqual(transparent.getpixel((0, 0))[3], 0)

    def test_detail_page_fills_reference_window_without_changing_source_color(self):
        source = Image.new("RGB", (320, 480), "#b52226")
        rendered = _detail_showcase_page(source)

        self.assertEqual(rendered.getpixel((51, 400)), (255, 255, 255))
        self.assertEqual(rendered.getpixel((52, 181)), (181, 34, 38))
        self.assertEqual(rendered.getpixel((694, 703)), (181, 34, 38))
        self.assertEqual(rendered.getpixel((695, 400)), (255, 255, 255))

    def test_template_product_box_allows_upscaling(self):
        canvas = Image.new("RGB", (750, 665), "white")
        _paste_product(canvas, self._small_catalog_product(), (378, 270, 665, 470))
        foreground = ImageChops.difference(canvas, Image.new("RGB", canvas.size, "white")).getbbox()

        self.assertIsNotNone(foreground)
        assert foreground is not None
        self.assertGreaterEqual(foreground[2] - foreground[0], 270)
        self.assertAlmostEqual((foreground[0] + foreground[2]) / 2, 521.5, delta=2)

    def test_info_page_keeps_product_clear_of_lower_dimension_line(self):
        page = Image.new("RGB", (750, 665), "white")
        _paste_product(page, self._small_catalog_product(), INFO_PRODUCT_BOX)
        foreground = ImageChops.difference(page, Image.new("RGB", page.size, "white")).getbbox()

        self.assertIsNotNone(foreground)
        assert foreground is not None
        self.assertLessEqual(foreground[3], INFO_PRODUCT_BOX[3])
        self.assertGreaterEqual(INFO_LENGTH_LINE_Y - foreground[3], 28)

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

    def test_batch_refinement_uses_lowest_confidence_as_back_fallback(self):
        samples = [
            {
                "id": image_id,
                "suggested_role": "front",
                "suggested_tags": [],
                "role_confidence": confidence,
                "main_component_ratio": ratio,
                "main_component_fill_ratio": 0.61,
                "main_body_side_edge_ratio": 1.0 + image_id,
                "main_symmetry_error": 0.01 * image_id,
                "strict_center_gold_ratio": 0.001,
                "bbox_ratio": 0.40,
                "sharpness": 1000 + image_id,
            }
            for image_id, confidence, ratio in [(1, 76, 1.20), (2, 58, 1.18), (3, 70, 1.12)]
        ]

        _refine_product_classifications(samples)

        fallback = next(item for item in samples if item["suggested_role"] == "back")
        self.assertEqual(fallback["id"], 2)
        self.assertEqual(fallback["role_confidence"], 58)
        self.assertIn("置信度最低", fallback["role_reason"])

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
        self.assertIn("同批必备视图约束", prompt)
        self.assertIn("五个不同index", prompt)

    def test_batch_guarantee_recovers_required_views_from_detail_candidates(self):
        samples = [
            {
                "id": 1,
                "file_name": "半侧.jpg",
                **metrics(
                    foreground_ratio=0.16,
                    bbox_ratio=0.21,
                    main_component_ratio=1.19,
                    main_component_fill_ratio=0.76,
                    main_symmetry_error=0.07,
                    main_body_side_edge_ratio=1.32,
                ),
                "suggested_role": "semi_side",
                "suggested_tags": [],
                "role_confidence": 84,
                "role_reason": "",
            },
            {
                "id": 2,
                "file_name": "背面.jpg",
                **metrics(
                    foreground_ratio=0.15,
                    bbox_ratio=0.21,
                    main_component_ratio=1.26,
                    main_component_fill_ratio=0.73,
                    main_symmetry_error=0.03,
                    strict_center_gold_ratio=0.0,
                    main_body_side_edge_ratio=0.90,
                ),
                "suggested_role": "back",
                "suggested_tags": [],
                "role_confidence": 82,
                "role_reason": "",
            },
            {
                "id": 3,
                "file_name": "全侧.jpg",
                **metrics(
                    foreground_ratio=0.06,
                    bbox_ratio=0.08,
                    main_component_ratio=0.44,
                    main_component_fill_ratio=0.72,
                    main_symmetry_error=0.07,
                    strict_center_gold_ratio=0.0,
                    main_body_side_edge_ratio=0.76,
                ),
                "suggested_role": "side",
                "suggested_tags": [],
                "role_confidence": 90,
                "role_reason": "",
            },
            {
                "id": 4,
                "file_name": "正面.jpg",
                **metrics(
                    foreground_ratio=0.16,
                    bbox_ratio=0.23,
                    main_component_ratio=1.31,
                    main_component_fill_ratio=0.72,
                    main_symmetry_error=0.09,
                    strict_center_gold_ratio=0.0028,
                    main_body_side_edge_ratio=1.05,
                ),
                "suggested_role": "front",
                "suggested_tags": ["logo", "hardware"],
                "role_confidence": 58,
                "role_reason": "",
            },
            {
                "id": 5,
                "file_name": "开口.jpg",
                **metrics(
                    foreground_ratio=0.10,
                    bbox_ratio=0.19,
                    main_component_ratio=0.80,
                    main_component_fill_ratio=0.51,
                    main_symmetry_error=0.50,
                    main_body_side_edge_ratio=0.07,
                    center_gold_ratio=0.08,
                ),
                "suggested_role": "detail",
                "suggested_tags": ["hardware"],
                "role_confidence": 74,
                "role_reason": "",
            },
            {
                "id": 6,
                "file_name": "透明.png",
                **metrics(
                    alpha_ratio=0.4,
                    foreground_ratio=0.39,
                    bbox_ratio=0.68,
                    main_component_ratio=1.31,
                ),
                "suggested_role": "transparent",
                "suggested_tags": [],
                "role_confidence": 99,
                "role_reason": "",
            },
        ]

        _refine_product_classifications(samples)

        roles = {sample["id"]: sample["suggested_role"] for sample in samples}
        self.assertEqual(roles[1], "semi_side")
        self.assertEqual(roles[3], "side")
        self.assertEqual(roles[4], "front")
        self.assertEqual(roles[5], "top")
        self.assertEqual(roles[6], "transparent")

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
        self.assertEqual(detail_page.getpixel((375, 300)), (181, 34, 38))
        self.assertEqual(detail_page.getpixel((700, 300)), (255, 255, 255))

    def test_slot_renderer_keeps_model_layouts_separate_from_product_normalization(self):
        source = Image.new("RGB", (320, 480), "#b52226")
        with patch("backend.services.vip_organizer_service._load_image", return_value=source):
            square_model = _render_slot_image("1.jpg", [11], {})
            portrait_model = _render_slot_image("50.jpg", [11], {})
            product = _render_slot_image("2.jpg", [12], {})

        self.assertIsNotNone(square_model)
        self.assertIsNotNone(portrait_model)
        self.assertIsNotNone(product)
        assert square_model is not None and portrait_model is not None and product is not None
        self.assertEqual(square_model.size, (800, 800))
        self.assertEqual(portrait_model.size, (950, 1200))
        self.assertEqual(product.size, (800, 800))
        self.assertEqual(square_model.getpixel((20, 20)), (181, 34, 38))
        self.assertEqual(product.getpixel((20, 20)), (255, 255, 255))

    def test_detail_slots_keep_the_uploaded_detail_frame(self):
        source = Image.new("RGB", (320, 480), "#b52226")
        with patch("backend.services.vip_organizer_service._load_image", return_value=source):
            logo_detail = _render_slot_image("4.jpg", [11], {})
            interior_detail = _render_slot_image("15.jpg", [12], {})
            detail_showcase = _render_slot_image("604.jpg", [12], {})
            hardware_showcase = _render_slot_image("605.jpg", [11], {})

        self.assertIsNotNone(logo_detail)
        self.assertIsNotNone(interior_detail)
        self.assertIsNotNone(detail_showcase)
        self.assertIsNotNone(hardware_showcase)
        assert (
            logo_detail is not None
            and interior_detail is not None
            and detail_showcase is not None
            and hardware_showcase is not None
        )
        self.assertEqual(logo_detail.size, (800, 800))
        self.assertEqual(interior_detail.size, (800, 800))
        self.assertEqual(logo_detail.getpixel((20, 400)), (255, 255, 255))
        self.assertEqual(logo_detail.getpixel((400, 400)), (181, 34, 38))
        self.assertEqual(interior_detail.getpixel((780, 400)), (255, 255, 255))
        self.assertEqual(interior_detail.getpixel((400, 400)), (181, 34, 38))
        self.assertEqual(detail_showcase.size, (750, 750))
        self.assertEqual(hardware_showcase.size, (750, 750))
        self.assertEqual(detail_showcase.getpixel((375, 400)), (181, 34, 38))
        self.assertEqual(hardware_showcase.getpixel((375, 400)), (181, 34, 38))

    def test_slot_map_links_the_two_model_output_sizes(self):
        mapped = _slot_map([
            {
                "file_name": "1.jpg",
                "image_ids": [7],
                "adjustments": [{"zoom": 1.2, "offset_x": 0.1}],
            },
            {
                "file_name": "50.jpg",
                "image_ids": [9],
                "adjustments": [{"zoom": 0.8, "offset_y": -0.1}],
            },
            {"file_name": "2.jpg", "image_ids": [3]},
        ])

        self.assertEqual(mapped["1.jpg"]["image_ids"], [7])
        self.assertEqual(mapped["50.jpg"]["image_ids"], [7])
        self.assertEqual(mapped["2.jpg"]["image_ids"], [3])
        self.assertEqual(mapped["1.jpg"]["adjustments"][0]["zoom"], 1.2)
        self.assertEqual(mapped["50.jpg"]["adjustments"][0]["zoom"], 0.8)

    def test_manual_adjustment_moves_and_scales_without_changing_source_color(self):
        source = Image.new("RGB", (320, 480), "#b52226")
        adjustment = {
            "zoom": 1.35,
            "offset_x": 0.16,
            "offset_y": -0.08,
            "crop_x": 0,
            "crop_y": 0,
            "crop_width": 1,
            "crop_height": 1,
        }

        with patch("backend.services.vip_organizer_service._load_image", return_value=source):
            automatic = _render_slot_image("4.jpg", [11], {})
            adjusted = _render_slot_image("4.jpg", [11], {}, [adjustment])

        self.assertIsNotNone(automatic)
        self.assertIsNotNone(adjusted)
        assert automatic is not None and adjusted is not None
        white = Image.new("RGB", adjusted.size, "white")
        automatic_bbox = ImageChops.difference(automatic, white).getbbox()
        adjusted_bbox = ImageChops.difference(adjusted, white).getbbox()
        self.assertIsNotNone(automatic_bbox)
        self.assertIsNotNone(adjusted_bbox)
        assert automatic_bbox is not None and adjusted_bbox is not None
        self.assertGreater(adjusted_bbox[2] - adjusted_bbox[0], automatic_bbox[2] - automatic_bbox[0])
        self.assertGreater((adjusted_bbox[0] + adjusted_bbox[2]) / 2, (automatic_bbox[0] + automatic_bbox[2]) / 2)
        self.assertEqual(adjusted.getpixel((400, 400)), (181, 34, 38))

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
