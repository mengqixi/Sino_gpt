import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np
from PIL import Image, ImageDraw

from backend.services import vip_organizer_service as service


class PreparedProductCutoutTests(unittest.TestCase):
    def test_export_cleanup_clears_opaque_pale_tail_after_alpha_collapse(self):
        rgba = np.zeros((180, 180, 4), dtype=np.uint8)
        rgba[35:138, 30:150, :3] = (205, 201, 194)
        rgba[35:132, 30:150, 3] = 255
        rgba[132, 30:150, 3] = 205
        rgba[133, 34:146, 3] = 145
        rgba[134, 42:138, 3] = 80
        rgba[135, 55:125, 3] = 35
        for column in range(18, 30):
            rgba[132:140, column, :3] = (220, 165, 35)
            rgba[132:140, column, 3] = 255
        rgba[130:133, 28:32, :3] = (220, 165, 35)
        rgba[130:133, 28:32, 3] = 255

        cleaned = service._remove_detached_floor_fragments(
            Image.fromarray(rgba, "RGBA")
        )
        alpha = np.asarray(cleaned.getchannel("A"))

        self.assertEqual(int(alpha[131, 90]), 255)
        self.assertEqual(int(alpha[132, 90]), 0)
        self.assertEqual(int(alpha[134, 90]), 0)
        self.assertEqual(int(alpha[136, 22]), 255)

    def test_model_dominant_pale_silver_keeps_body_and_opens_hardware_gap(self):
        shape = (120, 120)
        model_matte = np.zeros(shape, dtype=np.float32)
        model_matte[45:105, 20:100] = 0.99
        cv2.circle(model_matte, (82, 28), 13, 0.86, -1)
        strict_hardware = np.zeros(shape, dtype=np.uint8)
        cv2.circle(strict_hardware, (82, 28), 13, 1, 3)
        strict_hardware = strict_hardware.astype(bool)
        current_alpha = np.zeros(shape, dtype=np.uint8)
        current_alpha[strict_hardware] = 255

        alpha = service._model_dominant_pale_silver_alpha(
            model_matte,
            connected_background=np.ones(shape, dtype=bool),
            lab_distance=np.zeros(shape, dtype=np.float32),
            saturation=np.zeros(shape, dtype=np.uint8),
            value=np.full(shape, 255, dtype=np.uint8),
            current_alpha=current_alpha,
            strict_hardware=strict_hardware,
        )

        self.assertEqual(int(alpha[70, 60]), 255)
        self.assertEqual(int(alpha[28, 82]), 0)
        self.assertEqual(int(alpha[28, 69]), 255)
        self.assertEqual(int(alpha[5, 5]), 0)

    def test_model_body_fringe_cleanup_keeps_upper_structure_only(self):
        alpha = np.zeros((120, 120), dtype=np.uint8)
        alpha[52:110, 18:102] = 255
        alpha[48:52, 18:38] = 230
        alpha[48:52, 60:90] = 230
        cv2.line(alpha, (82, 8), (82, 52), 255, 5)

        cleaned = service._clear_shallow_model_body_fringe(alpha)

        self.assertEqual(int(cleaned[49, 30]), 0)
        self.assertEqual(int(cleaned[49, 82]), 255)
        self.assertEqual(int(cleaned[52, 30]), 255)

    def test_white_background_and_soft_floor_shadow_are_removed(self):
        source = Image.new("RGB", (520, 420), "white")
        pixels = np.asarray(source).copy()
        yy, xx = np.indices((420, 520))
        shadow = np.exp(-(((xx - 265) / 145) ** 2 + ((yy - 344) / 16) ** 2))
        pixels = np.clip(pixels - shadow[:, :, None] * 48, 0, 255).astype(np.uint8)
        source = Image.fromarray(pixels, "RGB")
        draw = ImageDraw.Draw(source)
        draw.rounded_rectangle((105, 130, 415, 335), radius=40, fill="#7999b2")
        draw.arc((160, 32, 360, 225), 180, 360, fill="#33556f", width=10)

        cutout = service._prepared_product_cutout(source)
        alpha = np.asarray(cutout.getchannel("A"))

        self.assertEqual(int(alpha[0, 0]), 0)
        self.assertGreater(int(alpha[alpha.shape[0] // 2, alpha.shape[1] // 2]), 245)
        self.assertLess(float(np.mean(alpha[-12:, :])), 3.0)

    def test_edge_matte_does_not_leave_a_white_halo(self):
        source = Image.new("RGB", (360, 320), "white")
        draw = ImageDraw.Draw(source)
        draw.ellipse((55, 40, 305, 285), fill="#7599b7")

        cutout = service._prepared_product_cutout(source)
        rgba = np.asarray(cutout)
        alpha = rgba[:, :, 3]
        semi = (alpha >= 20) & (alpha <= 235)

        self.assertGreater(int(np.count_nonzero(semi)), 0)
        self.assertLess(float(np.mean(rgba[:, :, :3][semi].min(axis=1))), 220.0)

    def test_pale_handle_is_preserved_without_filling_its_center(self):
        source = Image.new("RGB", (480, 480), "white")
        draw = ImageDraw.Draw(source)
        draw.rounded_rectangle((95, 205, 385, 405), radius=35, fill="#ddd8c7")
        draw.arc((150, 55, 330, 300), 180, 360, fill="#e7e2d2", width=18)

        cutout = service._prepared_product_cutout(source)
        alpha = np.asarray(cutout.getchannel("A"))

        self.assertGreater(int(alpha[35:130, :].max()), 220)
        self.assertEqual(int(alpha[0, alpha.shape[1] // 2]), 0)

    def test_near_white_body_is_not_erased_with_its_handle_opening(self):
        source = Image.new("RGB", (520, 460), "white")
        draw = ImageDraw.Draw(source)
        draw.polygon(
            ((105, 190), (415, 190), (385, 390), (135, 390)),
            fill="#f7f5ed",
        )
        draw.arc((165, 45, 355, 285), 180, 360, fill="#eeeade", width=18)

        model_matte = np.zeros((460, 520), dtype=np.float32)
        cv2.fillPoly(
            model_matte,
            [np.array(((105, 190), (415, 190), (385, 390), (135, 390)))],
            0.995,
        )
        cv2.ellipse(model_matte, (260, 165), (95, 120), 0, 180, 360, 0.995, 18)
        # Deliberately over-confident model fill in the white handle opening.
        # Regional cleanup must still clear it without deleting the pale body.
        cv2.ellipse(model_matte, (260, 175), (72, 88), 0, 180, 360, 0.90, -1)

        rgba = np.asarray(service._prepared_product_cutout(source, model_matte))
        rgb = rgba[:, :, :3]
        alpha = rgba[:, :, 3]
        body_pixels = np.max(np.abs(rgb.astype(np.int16) - np.array([247, 245, 237])), axis=2) <= 12
        white_pixels = np.min(rgb, axis=2) >= 252

        self.assertGreater(float(np.median(alpha[body_pixels])), 245.0)
        self.assertLess(float(np.mean(alpha[white_pixels])), 5.0)

    def test_pale_narrow_details_outside_body_run_are_preserved(self):
        source = Image.new("RGB", (560, 520), "white")
        draw = ImageDraw.Draw(source)
        draw.rounded_rectangle((105, 205, 455, 430), radius=38, fill="#f2efe3")
        draw.arc((150, 35, 410, 335), 180, 360, fill="#eee9dc", width=16)
        draw.line((95, 250, 58, 390), fill="#ece7da", width=14)
        draw.ellipse((126, 424, 158, 452), fill="#575550")
        draw.ellipse((402, 424, 434, 452), fill="#575550")

        model_matte = np.zeros((520, 560), dtype=np.float32)
        cv2.rectangle(model_matte, (105, 205), (455, 430), 0.995, -1)
        cv2.ellipse(model_matte, (280, 185), (130, 150), 0, 180, 360, 0.995, 16)
        cv2.line(model_matte, (95, 250), (58, 390), 0.995, 14)
        cv2.ellipse(model_matte, (142, 438), (16, 14), 0, 0, 360, 0.995, -1)
        cv2.ellipse(model_matte, (418, 438), (16, 14), 0, 0, 360, 0.995, -1)

        rgba = np.asarray(service._prepared_product_cutout(source, model_matte))
        rgb = rgba[:, :, :3]
        alpha = rgba[:, :, 3]
        pale_details = (
            (rgb[:, :, 0] >= 228)
            & (rgb[:, :, 0] <= 247)
            & (rgb[:, :, 2] <= 235)
        )
        dark_details = (
            np.max(
                np.abs(
                    rgb.astype(np.int16)
                    - np.array([87, 85, 80], dtype=np.int16)
                ),
                axis=2,
            )
            <= 12
        )
        white_pixels = np.min(rgb, axis=2) >= 252

        self.assertGreater(float(np.median(alpha[pale_details])), 245.0)
        self.assertGreater(float(np.percentile(alpha[dark_details], 95)), 245.0)
        self.assertGreater(int(np.count_nonzero(alpha[dark_details] > 245)), 100)
        self.assertLess(float(np.mean(alpha[white_pixels])), 5.0)

    def test_dark_soft_bag_with_detached_lower_details_uses_defined_cleanup_masks(self):
        source = Image.new("RGB", (520, 460), "white")
        draw = ImageDraw.Draw(source)
        draw.polygon(
            ((105, 180), (415, 180), (390, 390), (130, 390)),
            fill="#242323",
        )
        draw.arc((150, 45, 370, 285), 180, 360, fill="#292828", width=22)
        draw.rectangle((72, 352, 88, 374), fill="#252424")
        draw.rectangle((432, 350, 448, 372), fill="#252424")

        model_matte = np.zeros((460, 520), dtype=np.float32)
        cv2.fillPoly(
            model_matte,
            [np.array(((105, 180), (415, 180), (390, 390), (130, 390)))],
            0.995,
        )
        cv2.ellipse(model_matte, (260, 165), (110, 110), 0, 180, 360, 0.995, 22)
        model_matte[352:375, 72:89] = 0.995
        model_matte[350:373, 432:449] = 0.995

        alpha = np.asarray(
            service._prepared_product_cutout(source, model_matte).getchannel("A")
        )

        self.assertGreater(int(alpha[250, 260]), 245)
        self.assertEqual(int(alpha[0, 260]), 0)

    def test_coloured_product_does_not_lose_gold_hardware(self):
        source = Image.new("RGB", (520, 420), "white")
        draw = ImageDraw.Draw(source)
        draw.rounded_rectangle((105, 105, 415, 325), radius=38, fill="#7198b8")
        draw.rectangle((145, 323, 375, 336), fill="#746b65")
        draw.line((75, 340, 165, 340), fill="#d5ad55", width=5)

        model_matte = np.zeros((420, 520), dtype=np.float32)
        cv2.rectangle(model_matte, (105, 105), (415, 325), 0.995, -1)
        cv2.rectangle(model_matte, (145, 323), (375, 336), 0.86, -1)
        cv2.line(model_matte, (75, 340), (165, 340), 0.995, 5)

        rgba = np.asarray(service._prepared_product_cutout(source, model_matte))
        rgb = rgba[:, :, :3].astype(np.int16)
        alpha = rgba[:, :, 3]
        product_pixels = np.linalg.norm(rgb - np.array([113, 152, 184]), axis=2) <= 8
        hardware_pixels = np.linalg.norm(rgb - np.array([213, 173, 85]), axis=2) <= 12

        self.assertGreater(float(np.median(alpha[product_pixels])), 245.0)
        self.assertGreater(float(np.median(alpha[hardware_pixels])), 200.0)

    def test_coloured_body_extending_below_model_bottom_is_preserved(self):
        source = Image.new("RGB", (520, 420), "white")
        pixels = np.asarray(source).copy()
        yy, xx = np.indices((420, 520))
        shadow = np.exp(-(((xx - 260) / 120) ** 2 + ((yy - 356) / 9) ** 2))
        pixels = np.clip(pixels - shadow[:, :, None] * 38, 0, 255).astype(np.uint8)
        source = Image.fromarray(pixels, "RGB")
        draw = ImageDraw.Draw(source)
        draw.rounded_rectangle((105, 105, 415, 340), radius=55, fill="#7198b8")

        model_matte = np.zeros((420, 520), dtype=np.float32)
        # The model underestimates the rounded bottom by thirty pixels.
        cv2.rectangle(model_matte, (105, 105), (415, 310), 0.995, -1)
        cv2.rectangle(model_matte, (105, 311), (415, 340), 0.86, -1)

        rgba = np.asarray(service._prepared_product_cutout(source, model_matte))
        rgb = rgba[:, :, :3].astype(np.int16)
        alpha = rgba[:, :, 3]
        blue_body = np.linalg.norm(rgb - np.array([113, 152, 184]), axis=2) <= 12

        blue_rows, _ = np.where(blue_body)
        bottom_blue = blue_body & (
            np.indices(blue_body.shape)[0] >= int(blue_rows.max()) - 12
        )
        bottom_alpha = float(np.median(alpha[bottom_blue]))
        self.assertGreater(bottom_alpha, 140.0)
        self.assertLess(bottom_alpha, 230.0)
        self.assertLess(float(np.mean(alpha[-8:, :])), 3.0)

    def test_bottom_contour_follows_side_piping_without_touching_upper_product(self):
        height, width = 150, 200
        rgb = np.full((height, width, 3), 255, dtype=np.uint8)
        alpha = np.zeros((height, width), dtype=np.uint8)
        model_matte = np.zeros((height, width), dtype=np.float32)
        protected_detail = np.zeros((height, width), dtype=bool)

        # Coloured body and handle. The helper is allowed to inspect these
        # pixels but may never modify the upper product.
        rgb[35:112, 35:165] = (204, 176, 185)
        alpha[35:112, 35:165] = 255
        model_matte[35:112, 35:165] = 0.995
        cv2.ellipse(alpha, (100, 40), (42, 28), 0, 180, 360, 255, 8)
        cv2.ellipse(model_matte, (100, 40), (42, 28), 0, 180, 360, 0.995, 8)

        # Darker piping has the same colour on both side edges and along the
        # real bottom. The model is less confident on the lower piping, so the
        # iterative colour continuation must find it before deleting shadow.
        piping = np.array([126, 78, 88], dtype=np.uint8)
        rgb[55:116, 35:39] = piping
        rgb[55:116, 161:165] = piping
        rgb[112:116, 35:165] = piping
        alpha[55:116, 35:39] = 255
        alpha[55:116, 161:165] = 255
        alpha[112:116, 35:165] = 255
        model_matte[55:116, 35:39] = 0.995
        model_matte[55:116, 161:165] = 0.995
        model_matte[112:116, 35:165] = 0.90

        # Black contact shadow followed by a grey-white tail. A sparse gold
        # fitting at floor level is protected independently and does not
        # participate in the body contour.
        rgb[116:121, 45:155] = (48, 44, 45)
        rgb[121:125, 55:145] = (188, 185, 184)
        alpha[116:121, 45:155] = 225
        alpha[121:125, 55:145] = 150
        model_matte[116:121, 45:155] = 0.74
        model_matte[121:125, 55:145] = 0.42
        rgb[119:123, 20:31] = (213, 173, 85)
        alpha[119:123, 20:31] = 255
        model_matte[119:123, 20:31] = 0.98
        protected_detail[119:123, 20:31] = True

        before = alpha.copy()
        cleaned = service._clean_bottom_against_body_contour(
            alpha,
            rgb,
            model_matte,
            protected_detail,
            confidence_floor=0.96,
            contour_mode="piecewise",
        )

        np.testing.assert_array_equal(cleaned[:105], before[:105])
        self.assertGreater(int(np.median(cleaned[112:116, 50:150])), 245)
        self.assertEqual(int(np.max(cleaned[118:125, 60:140])), 0)
        self.assertEqual(int(np.min(cleaned[119:123, 20:31])), 255)

    def test_gold_and_silver_hardware_holes_stay_transparent(self):
        source = Image.new("RGB", (520, 440), "white")
        draw = ImageDraw.Draw(source)
        draw.rounded_rectangle((110, 190, 410, 365), radius=32, fill="#7198b8")
        draw.ellipse((145, 75, 235, 165), outline="#d5ad55", width=12)
        draw.ellipse((285, 75, 375, 165), outline="#8f969c", width=12)
        draw.line((190, 155, 190, 205), fill="#d5ad55", width=10)
        draw.line((330, 155, 330, 205), fill="#8f969c", width=10)

        model_matte = np.zeros((440, 520), dtype=np.float32)
        cv2.rectangle(model_matte, (110, 190), (410, 365), 0.995, -1)
        # A segmentation model commonly fills closed chain/fitting holes.
        cv2.circle(model_matte, (190, 120), 45, 0.995, -1)
        cv2.circle(model_matte, (330, 120), 45, 0.995, -1)
        cv2.line(model_matte, (190, 155), (190, 205), 0.995, 10)
        cv2.line(model_matte, (330, 155), (330, 205), 0.995, 10)

        rgba = np.asarray(service._prepared_product_cutout(source, model_matte))
        rgb = rgba[:, :, :3].astype(np.int16)
        alpha = rgba[:, :, 3]
        for color in (np.array([213, 173, 85]), np.array([143, 150, 156])):
            metal_pixels = np.linalg.norm(rgb - color, axis=2) <= 12
            rows, columns = np.where(metal_pixels)
            self.assertGreater(rows.size, 40)
            self.assertGreater(float(np.median(alpha[metal_pixels])), 220.0)
            center_y = (int(rows.min()) + int(rows.max())) // 2
            center_x = (int(columns.min()) + int(columns.max())) // 2
            self.assertLess(int(alpha[center_y, center_x]), 12)

    def test_pale_product_keeps_silver_hardware_and_removes_floor_shadow(self):
        source = Image.new("RGB", (560, 460), "white")
        pixels = np.asarray(source).copy()
        yy, xx = np.indices((460, 560))
        shadow = np.exp(-(((xx - 280) / 150) ** 2 + ((yy - 374) / 12) ** 2))
        pixels = np.clip(pixels - shadow[:, :, None] * 42, 0, 255).astype(np.uint8)
        source = Image.fromarray(pixels, "RGB")
        draw = ImageDraw.Draw(source)
        draw.rounded_rectangle((125, 150, 435, 360), radius=22, fill="#e2e0d8")
        draw.line((82, 360, 478, 360), fill="#6c7072", width=3)
        draw.line((82, 357, 478, 357), fill="#d7dbdc", width=2)

        model_matte = np.zeros((460, 560), dtype=np.float32)
        cv2.rectangle(model_matte, (125, 150), (435, 360), 0.995, -1)
        cv2.line(model_matte, (82, 360), (478, 360), 0.99, 5)
        # The model deliberately misclassifies the contact shadow as nearly
        # certain foreground. Floor cleanup must therefore use the silhouette
        # width collapse rather than trusting model confidence alone.
        cv2.ellipse(model_matte, (280, 374), (150, 12), 0, 0, 360, 0.99, -1)

        rgba = np.asarray(service._prepared_product_cutout(source, model_matte))
        rgb = rgba[:, :, :3].astype(np.int16)
        alpha = rgba[:, :, 3]
        silver_pixels = (
            (rgb[:, :, 0] >= 70)
            & (rgb[:, :, 0] <= 225)
            & (np.max(rgb, axis=2) - np.min(rgb, axis=2) <= 18)
            # The synthetic floor shadow and pale bag edge are neutral/warm.
            # Silver metal keeps a small cool-channel lead after decontamination.
            & (rgb[:, :, 2] >= rgb[:, :, 0] + 2)
        )

        self.assertGreater(float(np.percentile(alpha[silver_pixels], 80)), 220.0)
        self.assertLess(float(np.mean(alpha[-10:, :])), 3.0)

    def test_export_cleanup_removes_only_faint_detached_floor_slivers(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((150, 180, 650, 650), radius=45, fill=(113, 152, 184, 255))
        draw.rectangle((285, 670, 380, 673), fill=(150, 145, 138, 125))
        # Side-floor fragments can sit below a fitting, outside the central
        # body band but still within the product's overall horizontal span.
        draw.rectangle((620, 670, 625, 673), fill=(205, 202, 198, 125))
        # A detached, fully opaque gold fitting in the same lower band is real
        # product detail and must not be treated as a faint floor fragment.
        draw.rectangle((455, 671, 510, 674), fill=(213, 173, 85, 255))

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[671, 330, 3]), 0)
        self.assertEqual(int(cleaned[671, 622, 3]), 0)
        self.assertEqual(int(cleaned[672, 480, 3]), 255)

    def test_export_cleanup_removes_large_connected_contact_shadow(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((150, 170, 650, 650), fill=(190, 170, 135, 255))
        draw.rectangle((310, 651, 490, 662), fill=(105, 92, 78, 175))

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[656, 400, 3]), 0)
        self.assertEqual(int(cleaned[649, 400, 3]), 255)

    def test_export_cleanup_uses_product_to_shadow_collapse_before_shadow_tail(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((180, 170, 620, 650), fill=(190, 170, 135, 255))
        # A contact shadow can narrow sharply below the product, widen again,
        # and finally collapse at its own tail. The first, larger drop is the
        # product boundary; selecting the last drop leaves the shadow behind.
        draw.rectangle((355, 651, 445, 654), fill=(105, 92, 78, 210))
        draw.rectangle((285, 655, 515, 661), fill=(105, 92, 78, 175))
        draw.rectangle((330, 662, 470, 665), fill=(105, 92, 78, 120))

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[658, 400, 3]), 0)
        self.assertEqual(int(cleaned[649, 400, 3]), 255)

    def test_export_cleanup_removes_side_shadow_without_erasing_floor_hardware(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((180, 170, 620, 650), fill=(190, 150, 105, 255))
        # Separate white-background shadow shoulders remain connected to the
        # main component through small bridges above the lower taper band.
        draw.rectangle((145, 638, 174, 650), fill=(205, 202, 198, 220))
        draw.line((174, 635, 180, 635), fill=(205, 202, 198, 220), width=3)
        draw.line((174, 635, 174, 638), fill=(205, 202, 198, 220), width=3)
        # A side fitting and its pale studio shadow exercise the same geometry
        # away from the central body run.
        draw.line((620, 605, 652, 605), fill=(213, 173, 85, 255), width=5)
        draw.rectangle((650, 595, 670, 630), fill=(213, 173, 85, 255))
        draw.rectangle((655, 630, 665, 647), fill=(205, 202, 198, 220))
        # Substantial central contact shadow activates connected-floor cleanup.
        draw.rectangle((310, 651, 490, 662), fill=(105, 92, 78, 175))

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[645, 155, 3]), 0)
        self.assertEqual(int(cleaned[640, 660, 3]), 0)
        self.assertEqual(int(cleaned[610, 660, 3]), 255)
        self.assertEqual(int(cleaned[645, 400, 3]), 255)

    def test_export_cleanup_removes_bright_floor_shadow_below_dark_product(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((180, 170, 620, 650), fill=(42, 40, 38, 255))
        draw.rectangle((340, 651, 460, 660), fill=(110, 108, 106, 210))
        # Cool silver and saturated gold at floor level are real fittings.
        draw.rectangle((250, 651, 270, 660), fill=(115, 118, 122, 255))
        draw.rectangle((530, 651, 550, 660), fill=(213, 173, 85, 255))

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[655, 400, 3]), 0)
        self.assertEqual(int(cleaned[645, 400, 3]), 255)
        self.assertEqual(int(cleaned[655, 260, 3]), 255)
        self.assertEqual(int(cleaned[655, 540, 3]), 255)

    def test_export_cleanup_removes_low_chroma_floor_from_coloured_product(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((170, 170, 630, 650), fill=(113, 152, 184, 255))
        draw.rectangle((190, 651, 610, 657), fill=(145, 142, 139, 255))
        draw.rectangle((250, 658, 550, 661), fill=(125, 122, 119, 170))
        # A short gold chain segment sits beside the bag.  Keep it separate
        # from the neutral test shadow so hardware protection cannot mask the
        # very pixels this regression test is meant to exercise.
        draw.line((100, 655, 160, 655), fill=(213, 173, 85, 255), width=4)

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[645, 400, 3]), 255)
        self.assertEqual(int(cleaned[653, 400, 3]), 0)
        self.assertEqual(int(cleaned[660, 400, 3]), 0)
        self.assertEqual(int(cleaned[655, 120, 3]), 255)

    def test_export_cleanup_preserves_pastel_body_and_removes_cast_shadow(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        pastel = (204, 176, 185, 255)
        draw.rectangle((170, 170, 630, 657), fill=pastel)
        draw.rectangle((450, 658, 610, 664), fill=(91, 70, 74, 177))
        draw.line((100, 660, 155, 660), fill=(213, 173, 85, 255), width=4)

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[654, 400, 3]), 255)
        self.assertEqual(int(cleaned[660, 520, 3]), 0)
        self.assertEqual(int(cleaned[660, 120, 3]), 255)

    def test_export_cleanup_follows_bucket_piping_and_clears_floor_shadow(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        # A broad, light woven body ends in saturated brown piping. The
        # neutral studio shadow is connected and wider than that real base,
        # so a simple component or last-row crop cannot distinguish them.
        draw.rectangle((180, 170, 620, 650), fill=(190, 170, 135, 255))
        draw.rectangle((190, 651, 610, 656), fill=(92, 55, 35, 255))
        draw.rectangle((150, 657, 650, 663), fill=(38, 35, 34, 210))
        # Side hardware stays opaque while its neutral cast shadow is removed.
        draw.line((620, 625, 660, 625), fill=(213, 173, 85, 255), width=5)
        draw.rectangle((650, 615, 670, 645), fill=(213, 173, 85, 255))
        draw.rectangle((655, 646, 668, 660), fill=(180, 178, 175, 210))

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[645, 400, 3]), 255)
        self.assertEqual(int(cleaned[653, 400, 3]), 255)
        self.assertEqual(int(cleaned[660, 400, 3]), 0)
        self.assertEqual(int(cleaned[630, 660, 3]), 255)
        self.assertEqual(int(cleaned[655, 660, 3]), 0)

    def test_export_cleanup_removes_opaque_colour_cast_contact_tail(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((180, 170, 620, 650), fill=(190, 170, 135, 255))
        draw.rectangle((190, 651, 610, 656), fill=(92, 55, 35, 255))
        draw.rectangle((245, 657, 555, 665), fill=(70, 38, 24, 255))
        draw.rectangle((300, 657, 330, 665), fill=(220, 165, 35, 255))

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[653, 400, 3]), 255)
        self.assertEqual(int(cleaned[661, 400, 3]), 0)
        self.assertEqual(int(cleaned[661, 315, 3]), 255)

    def test_export_cleanup_clears_mixed_brightness_after_confirmed_contact_edge(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((180, 170, 620, 650), fill=(190, 170, 135, 255))
        # Real saturated piping is followed by a multi-row dark contact core.
        # The same shadow also contains brighter grey/yellow studio fragments;
        # once the contact boundary is confirmed, those fragments must not be
        # left behind as a dotted line on the transparent preview.
        draw.rectangle((190, 651, 610, 656), fill=(98, 60, 31, 255))
        draw.rectangle((245, 657, 555, 665), fill=(61, 37, 24, 255))
        draw.rectangle((270, 659, 315, 663), fill=(185, 170, 145, 255))
        draw.rectangle((460, 658, 520, 664), fill=(145, 139, 125, 255))
        draw.rectangle((330, 657, 350, 665), fill=(220, 165, 35, 255))

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[653, 400, 3]), 255)
        self.assertEqual(int(cleaned[661, 285, 3]), 0)
        self.assertEqual(int(cleaned[661, 480, 3]), 0)
        self.assertEqual(int(cleaned[661, 340, 3]), 255)

    def test_colour_cast_contact_cleanup_generalises_across_bag_shapes(self):
        cases = (
            ("bucket", (620, 760), (150, 150, 470, 625)),
            ("wide_tote", (960, 720), (150, 150, 810, 585)),
            ("box_case", (720, 720), (145, 145, 575, 585)),
            ("small_crossbody", (520, 640), (105, 135, 415, 515)),
        )
        for name, size, bounds in cases:
            with self.subTest(name=name):
                width, height = size
                left, top, right, bottom = bounds
                image = Image.new(
                    "RGBA",
                    size,
                    (255, 255, 255, 0),
                )
                draw = ImageDraw.Draw(image)
                body_colour = (188, 156, 105, 255)
                if name == "wide_tote":
                    draw.polygon(
                        (
                            (left + 35, top),
                            (right - 35, top),
                            (right, bottom),
                            (left, bottom),
                        ),
                        fill=body_colour,
                    )
                elif name == "bucket":
                    draw.rounded_rectangle(
                        bounds,
                        radius=round((right - left) * 0.12),
                        fill=body_colour,
                    )
                else:
                    draw.rounded_rectangle(
                        bounds,
                        radius=max(10, round((right - left) * 0.05)),
                        fill=body_colour,
                    )

                # Real leather piping is followed by a deeper cast shadow.
                pipe_top = bottom + 1
                shadow_top = pipe_top + 5
                draw.rectangle(
                    (left + 10, pipe_top, right - 10, shadow_top - 1),
                    fill=(98, 60, 31, 255),
                )
                draw.rectangle(
                    (
                        left + round((right - left) * 0.18),
                        shadow_top,
                        right - round((right - left) * 0.18),
                        shadow_top + 7,
                    ),
                    fill=(61, 37, 24, 255),
                )
                hardware_x = left + round((right - left) * 0.30)
                draw.rectangle(
                    (hardware_x, shadow_top, hardware_x + 16, shadow_top + 7),
                    fill=(220, 165, 35, 255),
                )

                cleaned = np.asarray(
                    service._remove_detached_floor_fragments(image)
                )
                center_x = (left + right) // 2

                self.assertEqual(
                    int(cleaned[pipe_top + 2, center_x, 3]),
                    255,
                )
                self.assertEqual(
                    int(cleaned[shadow_top + 4, center_x, 3]),
                    0,
                )
                self.assertEqual(
                    int(cleaned[shadow_top + 4, hardware_x + 8, 3]),
                    255,
                )

    def test_colour_cast_cleanup_keeps_real_terminal_piping_without_shadow(self):
        image = Image.new("RGBA", (640, 640), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (120, 120, 520, 525),
            radius=35,
            fill=(116, 166, 142, 255),
        )
        draw.rectangle((135, 526, 505, 527), fill=(42, 78, 62, 255))

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[526, 320, 3]), 255)
        self.assertEqual(int(cleaned[527, 320, 3]), 255)

    def test_export_cleanup_normalises_only_subvisible_alpha_noise(self):
        image = Image.new("RGBA", (240, 240), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((45, 45, 195, 195), fill=(130, 100, 75, 255))
        draw.point((43, 120), fill=(70, 50, 35, 8))
        draw.point((44, 120), fill=(70, 50, 35, 9))

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[120, 43, 3]), 0)
        self.assertEqual(int(cleaned[120, 44, 3]), 9)

    def test_export_cleanup_uses_terminal_alpha_collapse_on_patterned_bag(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((180, 170, 620, 650), fill=(77, 72, 68, 255))
        for x in range(190, 620, 18):
            draw.line((x, 180, x + 80, 650), fill=(185, 178, 169, 255), width=4)
        # The model-supported floor tail is connected, fairly opaque and much
        # larger than a fragment, but begins only after the real base.
        draw.rectangle((245, 651, 555, 662), fill=(88, 84, 80, 170))
        # Gold feet below the same edge remain protected.
        draw.rectangle((225, 651, 242, 662), fill=(213, 173, 85, 255))
        draw.rectangle((558, 651, 575, 662), fill=(213, 173, 85, 255))

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[645, 400, 3]), 255)
        self.assertEqual(int(cleaned[658, 400, 3]), 0)
        self.assertEqual(int(cleaned[658, 233, 3]), 255)
        self.assertEqual(int(cleaned[658, 566, 3]), 255)

    def test_export_cleanup_removes_opaque_detached_woven_floor_sliver(self):
        image = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((200, 170, 600, 650), fill=(204, 151, 169, 255))
        # This is detached after resize and too opaque for the faint-fragment
        # cleanup, but remains a shallow central colour-cast floor sliver.
        draw.rectangle((310, 659, 490, 665), fill=(126, 91, 101, 235))
        # A tall leather cord and a separate gold chain are real details.
        draw.rectangle((270, 620, 278, 675), fill=(153, 93, 112, 255))
        draw.line((510, 662, 575, 662), fill=(213, 173, 85, 255), width=4)

        cleaned = np.asarray(service._remove_detached_floor_fragments(image))

        self.assertEqual(int(cleaned[662, 400, 3]), 0)
        self.assertEqual(int(cleaned[660, 274, 3]), 255)
        self.assertEqual(int(cleaned[662, 540, 3]), 255)

    def test_residue_audit_clears_small_white_hardware_pocket_only(self):
        shape = (120, 120)
        alpha = np.zeros(shape, dtype=np.uint8)
        alpha[20:100, 20:100] = 255
        tight_hardware = np.zeros(shape, dtype=bool)
        cv2.circle(tight_hardware.astype(np.uint8), (82, 55), 8, 1, 3)
        hardware_u8 = np.zeros(shape, dtype=np.uint8)
        cv2.circle(hardware_u8, (82, 55), 8, 1, 3)
        tight_hardware = hardware_u8.astype(bool)
        # A tiny studio-white pocket sits inside the metal ring.
        white_pocket = np.zeros(shape, dtype=bool)
        white_pocket[52:56, 80:84] = True
        lab_distance = np.full(shape, 70.0, dtype=np.float32)
        saturation = np.full(shape, 80, dtype=np.uint8)
        value = np.full(shape, 150, dtype=np.uint8)
        source_min = np.full(shape, 120, dtype=np.uint8)
        source_spread = np.full(shape, 60, dtype=np.int16)
        lab_distance[white_pocket] = 4
        saturation[white_pocket] = 2
        value[white_pocket] = 245
        source_min[white_pocket] = 242
        source_spread[white_pocket] = 3
        false_mask = np.zeros(shape, dtype=bool)

        cleaned = service._audit_white_studio_residue(
            alpha,
            lab_distance=lab_distance,
            saturation=saturation,
            value=value,
            source_min=source_min,
            source_spread=source_spread,
            model_matte=np.full(shape, 0.96, dtype=np.float32),
            tight_hardware=tight_hardware,
            pale_body_protection=false_mask,
            colored_material=false_mask,
            compact_detail=false_mask,
            verified_floor_hardware=false_mask,
        )

        self.assertEqual(int(cleaned[53, 81]), 0)
        self.assertEqual(int(cleaned[55, 74]), 255)
        self.assertEqual(int(cleaned[50, 50]), 255)

    def test_residue_audit_clears_shallow_floor_but_preserves_hardware(self):
        shape = (120, 120)
        alpha = np.zeros(shape, dtype=np.uint8)
        alpha[20:100, 20:100] = 255
        alpha[104:107, 42:55] = 190
        alpha[104:107, 76:84] = 255
        model_matte = np.full(shape, 0.97, dtype=np.float32)
        model_matte[104:107, 42:55] = 0.35
        tight_hardware = np.zeros(shape, dtype=bool)
        tight_hardware[104:107, 76:84] = True
        false_mask = np.zeros(shape, dtype=bool)

        cleaned = service._audit_white_studio_residue(
            alpha,
            lab_distance=np.full(shape, 45.0, dtype=np.float32),
            saturation=np.full(shape, 10, dtype=np.uint8),
            value=np.full(shape, 105, dtype=np.uint8),
            source_min=np.full(shape, 90, dtype=np.uint8),
            source_spread=np.full(shape, 12, dtype=np.int16),
            model_matte=model_matte,
            tight_hardware=tight_hardware,
            pale_body_protection=false_mask,
            colored_material=false_mask,
            compact_detail=false_mask,
            verified_floor_hardware=tight_hardware,
        )

        self.assertEqual(int(cleaned[105, 48]), 0)
        self.assertEqual(int(cleaned[105, 80]), 255)
        self.assertEqual(int(cleaned[95, 48]), 255)

    def test_vip_30_export_is_800_square_and_within_required_file_size(self):
        image = Image.new("RGBA", (1100, 900), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((110, 100, 990, 820), radius=90, fill="#7599b7")
        with TemporaryDirectory() as directory:
            output = Path(directory) / "30.png"
            service._save_png_30(image, output)
            with Image.open(output) as saved:
                self.assertEqual(saved.size, (800, 800))
                self.assertIn("A", saved.mode)
            self.assertGreaterEqual(output.stat().st_size, 100_000)
            self.assertLessEqual(output.stat().st_size, 600_000)


if __name__ == "__main__":
    unittest.main()
