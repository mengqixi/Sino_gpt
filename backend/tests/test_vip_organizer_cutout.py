import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np
from PIL import Image, ImageDraw

from backend.services import vip_organizer_service as service


class PreparedProductCutoutTests(unittest.TestCase):
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
