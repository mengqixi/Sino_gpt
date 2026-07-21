import base64
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import UploadFile
from PIL import Image, ImageDraw, ImageFilter

from backend.services import product_image_media_service as media


class ProductImageMediaServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _upload(self, data: bytes, filename: str = "source.jpg") -> UploadFile:
        return UploadFile(file=io.BytesIO(data), filename=filename)

    def _save_image(self, name: str, image: Image.Image, **save_options) -> Path:
        path = self.root / name
        image.save(path, **save_options)
        return path

    def _data_url(self, image: Image.Image, image_format: str = "PNG") -> str:
        buffer = io.BytesIO()
        image.save(buffer, format=image_format)
        mime = "image/png" if image_format == "PNG" else "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"

    def test_stream_upload_enforces_limit_and_preserves_existing_target(self):
        target = self.root / "saved" / "upload.bin"
        written = media.stream_upload(self._upload(b"abcdef", "../unsafe.jpg"), target, 6)
        self.assertEqual(written, 6)
        self.assertEqual(target.read_bytes(), b"abcdef")

        with self.assertRaises(media.MediaValidationError):
            media.stream_upload(self._upload(b"too-large"), target, 3)
        self.assertEqual(target.read_bytes(), b"abcdef")
        self.assertFalse(list(target.parent.glob("*.part")))
        self.assertFalse(list(target.parent.glob(".*.part")))

    def test_stream_upload_rejects_empty_file(self):
        with self.assertRaises(media.MediaValidationError):
            media.stream_upload(self._upload(b""), self.root / "empty.jpg", 100)
        self.assertFalse((self.root / "empty.jpg").exists())

    def test_decode_rejects_excessive_pixel_count_before_loading(self):
        class OversizedImage:
            size = (10_000, 10_000)

        with self.assertRaises(media.MediaValidationError):
            media._convert_oriented_to_srgb(OversizedImage(), preserve_alpha=False)  # type: ignore[arg-type]

    def test_normalize_photo_applies_orientation_and_removes_exif(self):
        source = Image.new("RGB", (40, 20), "#c42f39")
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "private description"
        source_path = self._save_image("oriented.jpg", source, exif=exif)
        target = self.root / "normalized.jpg"

        result = media.normalize_photo(source_path, target)

        self.assertEqual((result["width"], result["height"]), (20, 40))
        with Image.open(target) as normalized:
            self.assertEqual(normalized.format, "JPEG")
            self.assertEqual(normalized.mode, "RGB")
            self.assertEqual(normalized.size, (20, 40))
            self.assertEqual(len(normalized.getexif()), 0)

    def test_quality_metrics_use_common_900px_scale(self):
        sharp = Image.new("RGB", (90, 60), "white")
        draw = ImageDraw.Draw(sharp)
        for x in range(0, 90, 6):
            draw.rectangle((x, 0, x + 2, 59), fill="black")
        blurred = sharp.filter(ImageFilter.GaussianBlur(3.0))
        sharp_path = self._save_image("sharp.png", sharp)
        blurred_path = self._save_image("blurred.png", blurred)
        white_path = self._save_image("white.png", Image.new("RGB", (45, 30), "white"))

        sharp_metrics = media.quality_metrics(sharp_path)
        blurred_metrics = media.quality_metrics(blurred_path)
        white_metrics = media.quality_metrics(white_path)

        self.assertEqual(sharp_metrics["analysis_long_edge"], 900)
        self.assertEqual(blurred_metrics["analysis_long_edge"], 900)
        self.assertGreater(
            sharp_metrics["laplacian_variance_900"],
            blurred_metrics["laplacian_variance_900"],
        )
        self.assertEqual(white_metrics["highlight_clipped_ratio"], 1.0)
        self.assertEqual(white_metrics["underexposed_ratio"], 0.0)

    def test_probe_video_prefers_ffprobe_and_parses_rotation(self):
        video = self.root / "sample.mov"
        video.write_bytes(b"placeholder")
        payload = {
            "format": {"duration": "12.500"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                    "side_data_list": [{"rotation": -90}],
                }
            ],
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with patch.object(media.shutil, "which", return_value="ffprobe"), patch.object(
            media.subprocess, "run", return_value=completed
        ):
            result = media.probe_video(video)

        self.assertEqual(result["backend"], "ffprobe")
        self.assertEqual(result["codec_name"], "hevc")
        self.assertAlmostEqual(result["duration_seconds"], 12.5)
        self.assertAlmostEqual(result["fps"], 29.97003, places=4)
        self.assertEqual(result["rotation_degrees"], -90)

    def test_probe_video_rejects_overlong_ffprobe_result_without_fallback(self):
        video = self.root / "long.mp4"
        video.write_bytes(b"placeholder")
        payload = {
            "format": {"duration": "60.2"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1280,
                    "height": 720,
                    "avg_frame_rate": "30/1",
                }
            ],
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with patch.object(media.shutil, "which", return_value="ffprobe"), patch.object(
            media.subprocess, "run", return_value=completed
        ), patch.object(media, "_probe_video_with_opencv") as fallback:
            with self.assertRaises(media.MediaValidationError):
                media.probe_video(video)
        fallback.assert_not_called()

    def test_probe_video_uses_opencv_when_ffprobe_is_unavailable(self):
        video = self.root / "fallback.mp4"
        video.write_bytes(b"placeholder")
        fallback_result = {
            "path": str(video),
            "duration_seconds": 9.0,
            "width": 640,
            "height": 480,
            "fps": 25.0,
            "codec_name": "mp4v",
            "rotation_degrees": 0,
            "backend": "opencv",
            "file_size": video.stat().st_size,
        }
        with patch.object(media.shutil, "which", return_value=None), patch.object(
            media, "_probe_video_with_opencv", return_value=fallback_result
        ):
            result = media.probe_video(video)
        self.assertEqual(result["backend"], "opencv")

    def test_extract_video_frames_caps_at_twelve_and_stratifies_time(self):
        video = self.root / "sixty-seconds.mp4"
        video.write_bytes(b"placeholder")
        output_dir = self.root / "frames"

        def fake_extract(_video_path, timestamp, target):
            Image.new("RGB", (120, 80), (int(timestamp * 3) % 255, 80, 120)).save(target, format="JPEG")
            return "fake"

        with patch.object(
            media,
            "probe_video",
            return_value={"duration_seconds": 60.0, "width": 1920, "height": 1080},
        ), patch.object(media, "_extract_frame_at_time", side_effect=fake_extract):
            frames = media.extract_video_frames(video, output_dir, max_frames=50)

        self.assertEqual(len(frames), 12)
        self.assertEqual(frames[0]["timestamp_seconds"], 2.5)
        self.assertEqual(frames[-1]["timestamp_seconds"], 57.5)
        self.assertTrue(all(Path(frame["path"]).is_file() for frame in frames))
        self.assertTrue(all(frame["quality"]["analysis_long_edge"] == 900 for frame in frames))

    def test_make_contact_sheet_numbers_frames(self):
        frames = []
        for index in range(1, 6):
            path = self._save_image(
                f"contact-{index}.jpg",
                Image.new("RGB", (100 + index, 60), (index * 30, 40, 80)),
            )
            frames.append({"index": index, "timestamp_seconds": index * 1.25, "path": str(path)})
        output = media.make_contact_sheet(frames, self.root / "sheet.jpg")
        self.assertIsInstance(output, Path)
        with Image.open(output) as sheet:
            self.assertEqual(sheet.size, (1280, 520))
            self.assertEqual(sheet.format, "JPEG")

    def test_image_data_url_uses_detected_mime(self):
        path = self._save_image("source.png", Image.new("RGBA", (8, 8), (1, 2, 3, 4)))
        value = media.image_data_url(path)
        self.assertTrue(value.startswith("data:image/png;base64,"))
        decoded = base64.b64decode(value.split(",", 1)[1])
        self.assertEqual(decoded, path.read_bytes())

    def test_image_data_url_can_downscale_analysis_payload(self):
        source = Image.new("RGB", (2400, 1200), "#7a3344")
        source_path = self._save_image("large-analysis.jpg", source, quality=96)

        data_url = media.image_data_url(source_path, max_long_edge=640)
        payload = base64.b64decode(data_url.split(",", 1)[1])

        self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))
        with Image.open(io.BytesIO(payload)) as decoded:
            self.assertEqual(decoded.size, (640, 320))

    def test_save_api_product_result_creates_native_jpg_and_800_contain(self):
        source = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
        ImageDraw.Draw(source).rectangle((100, 20, 300, 180), fill=(190, 20, 40, 255))
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        high = self.root / "high.jpg"
        small = self.root / "800.jpg"

        result = media.save_api_product_result(buffer.getvalue(), high, small)

        self.assertEqual(result["source_format"], "PNG")
        with Image.open(high) as high_image:
            self.assertEqual(high_image.size, (400, 200))
            self.assertEqual(high_image.format, "JPEG")
            self.assertGreater(min(high_image.getpixel((5, 5))), 240)
        with Image.open(small) as small_image:
            self.assertEqual(small_image.size, (800, 800))
            self.assertGreater(min(small_image.getpixel((5, 5))), 240)

    def test_create_initial_transparent_removes_connected_white_background(self):
        source = Image.new("RGB", (200, 200), "white")
        ImageDraw.Draw(source).rectangle((50, 60, 150, 160), fill="#202428")
        source_path = self._save_image("white-front.jpg", source, quality=100)
        high = self.root / "transparent.png"
        small = self.root / "transparent-800.png"

        result = media.create_initial_transparent(source_path, high, small)

        self.assertEqual(result["format"], "PNG")
        with Image.open(high) as cutout:
            alpha = cutout.getchannel("A")
            self.assertLess(alpha.getpixel((5, 5)), 10)
            self.assertGreater(alpha.getpixel((100, 100)), 245)
        with Image.open(small) as small_image:
            self.assertEqual(small_image.size, (800, 800))
            self.assertEqual(small_image.mode, "RGBA")

    def test_create_initial_transparent_accepts_complex_phone_background(self):
        source = Image.new("RGB", (220, 180), (38, 92, 145))
        draw = ImageDraw.Draw(source)
        draw.rectangle((0, 90, 219, 179), fill=(92, 74, 55))
        draw.rounded_rectangle((48, 36, 172, 158), radius=22, fill=(190, 38, 72))
        draw.arc((72, 10, 148, 78), 180, 360, fill=(190, 38, 72), width=12)
        source_path = self._save_image("phone-front.jpg", source, quality=96)
        high = self.root / "phone-transparent.png"
        small = self.root / "phone-transparent-800.png"

        result = media.create_initial_transparent(source_path, high, small)

        self.assertTrue(high.is_file())
        self.assertTrue(small.is_file())
        self.assertIn(result["segmentation_mode"], {"grabcut", "manual_opaque"})
        self.assertTrue(result["manual_review_required"])
        with Image.open(high) as rendered:
            self.assertEqual(rendered.mode, "RGBA")

    def test_initial_transparent_limits_mask_work_but_keeps_native_size(self):
        source = Image.new("RGB", (400, 240), "white")
        ImageDraw.Draw(source).rectangle((80, 40, 320, 210), fill="#35252a")
        source_path = self._save_image("large-white-front.jpg", source, quality=96)
        high = self.root / "large-transparent.png"
        small = self.root / "large-transparent-800.png"

        with patch.object(media, "MAX_SEGMENTATION_LONG_EDGE", 100):
            media.create_initial_transparent(source_path, high, small)

        with Image.open(high) as rendered:
            self.assertEqual(rendered.size, (400, 240))
            self.assertLess(rendered.getchannel("A").getpixel((2, 2)), 16)

    def test_save_edited_transparent_preserves_alpha_and_builds_800(self):
        edited = Image.new("RGBA", (100, 50), (0, 0, 0, 0))
        ImageDraw.Draw(edited).rectangle((20, 5, 80, 45), fill=(30, 50, 70, 255))
        high = self.root / "edited.png"
        small = self.root / "edited-800.png"

        result = media.save_edited_transparent(self._data_url(edited), high, small)

        self.assertEqual(result["width"], 100)
        with Image.open(high) as saved:
            self.assertEqual(saved.getchannel("A").getpixel((0, 0)), 0)
            self.assertEqual(saved.getchannel("A").getpixel((50, 25)), 255)
        with Image.open(small) as saved_small:
            self.assertEqual(saved_small.size, (800, 800))

    def test_save_transparent_file_preserves_alpha_without_base64(self):
        source = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
        ImageDraw.Draw(source).ellipse((20, 10, 100, 70), fill=(80, 30, 20, 255))
        source_path = self._save_image("edited-file.png", source)
        high = self.root / "edited-file-high.png"
        small = self.root / "edited-file-800.png"

        media.save_transparent_file(source_path, high, small)

        with Image.open(high) as saved:
            self.assertEqual(saved.mode, "RGBA")
            self.assertEqual(saved.getchannel("A").getpixel((0, 0)), 0)
            self.assertEqual(saved.getchannel("A").getpixel((60, 40)), 255)
        with Image.open(small) as saved_small:
            self.assertEqual(saved_small.size, (800, 800))

        opaque = Image.new("RGB", (20, 20), "red")
        with self.assertRaises(media.MediaValidationError):
            media.save_edited_transparent(
                self._data_url(opaque),
                self.root / "invalid.png",
                self.root / "invalid-800.png",
            )

    def test_render_logo_crop_uses_oriented_square_and_conservative_jpg(self):
        source = Image.new("RGB", (200, 100), "#7b2733")
        ImageDraw.Draw(source).rectangle((75, 25, 125, 75), fill="#d9b45c")
        source_path = self._save_image("logo-source.jpg", source, quality=100)
        high = self.root / "logo.jpg"
        small = self.root / "logo-800.jpg"

        result = media.render_logo_crop(
            source_path,
            {"left": 0.25, "top": 0.0, "right": 0.75, "bottom": 1.0},
            high,
            small,
        )

        self.assertEqual(result["crop_box"], (50, 0, 150, 100))
        with Image.open(high) as logo:
            self.assertEqual(logo.size, (100, 100))
            self.assertEqual(logo.format, "JPEG")
        with Image.open(small) as logo800:
            self.assertEqual(logo800.size, (800, 800))

    def test_safe_path_component_blocks_traversal_and_windows_reserved_names(self):
        self.assertEqual(media.safe_path_component("CON"), "_CON")
        unsafe = media.safe_path_component(" ../../款号:BK/红色? ")
        self.assertNotIn("/", unsafe)
        self.assertNotIn("\\", unsafe)
        self.assertNotIn(":", unsafe)
        self.assertNotIn("?", unsafe)
        self.assertNotIn("..", unsafe)
        arcname = media.safe_zip_arcname("E06/危险", "高清", "01:正面.jpg")
        self.assertEqual(arcname.count("/"), 2)

    def test_heic_registration_reports_boolean(self):
        self.assertIsInstance(media.register_heic_support(), bool)

    @unittest.skipUnless(media.register_heic_support(), "pillow-heif is not installed")
    def test_normalize_photo_decodes_heic_to_jpeg(self):
        from pillow_heif import from_pillow

        source_path = self.root / "phone-photo.heic"
        from_pillow(Image.new("RGB", (48, 32), "#37668b")).save(source_path, quality=90)
        target = self.root / "phone-photo.jpg"

        result = media.normalize_photo(source_path, target)

        self.assertEqual((result["width"], result["height"]), (48, 32))
        with Image.open(target) as normalized:
            self.assertEqual(normalized.format, "JPEG")
            self.assertEqual(normalized.mode, "RGB")


if __name__ == "__main__":
    unittest.main()
