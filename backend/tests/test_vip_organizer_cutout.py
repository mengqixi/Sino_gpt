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

    def test_coloured_product_does_not_lose_gold_hardware(self):
        source = Image.new("RGB", (520, 420), "white")
        draw = ImageDraw.Draw(source)
        draw.rounded_rectangle((105, 105, 415, 325), radius=38, fill="#7198b8")
        draw.rectangle((145, 323, 375, 336), fill="#746b65")
        draw.line((75, 340, 445, 340), fill="#d5ad55", width=5)

        model_matte = np.zeros((420, 520), dtype=np.float32)
        cv2.rectangle(model_matte, (105, 105), (415, 325), 0.995, -1)
        cv2.rectangle(model_matte, (145, 323), (375, 336), 0.86, -1)
        cv2.line(model_matte, (75, 340), (445, 340), 0.995, 5)

        rgba = np.asarray(service._prepared_product_cutout(source, model_matte))
        rgb = rgba[:, :, :3].astype(np.int16)
        alpha = rgba[:, :, 3]
        product_pixels = np.linalg.norm(rgb - np.array([113, 152, 184]), axis=2) <= 8
        hardware_pixels = np.linalg.norm(rgb - np.array([213, 173, 85]), axis=2) <= 12

        self.assertGreater(float(np.median(alpha[product_pixels])), 245.0)
        self.assertGreater(float(np.median(alpha[hardware_pixels])), 220.0)

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
