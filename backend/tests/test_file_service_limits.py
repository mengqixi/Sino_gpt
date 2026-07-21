import base64
import unittest
from unittest.mock import patch

from backend.services.file_service import decode_base64_image, download_image


class FakeResponse:
    def __init__(self, chunks: list[bytes], content_length: str | None = None):
        self._chunks = chunks
        self.headers = {"Content-Length": content_length} if content_length is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield from self._chunks


class FileServiceLimitTests(unittest.TestCase):
    def test_base64_limit_is_checked_before_and_after_decode(self):
        value = base64.b64encode(b"123456").decode("ascii")
        self.assertEqual(decode_base64_image(value, max_bytes=6), b"123456")
        with self.assertRaisesRegex(ValueError, "超过服务器"):
            decode_base64_image(value, max_bytes=5)

    @patch("backend.services.file_service.requests.get")
    def test_download_rejects_large_content_length(self, mocked_get):
        mocked_get.return_value = FakeResponse([], content_length="100")
        with self.assertRaisesRegex(ValueError, "超过服务器"):
            download_image("https://example.invalid/image.jpg", 5, max_bytes=10)

    @patch("backend.services.file_service.requests.get")
    def test_download_stream_enforces_limit_without_header(self, mocked_get):
        mocked_get.return_value = FakeResponse([b"1234", b"5678"])
        with self.assertRaisesRegex(ValueError, "超过服务器"):
            download_image("https://example.invalid/image.jpg", 5, max_bytes=7)


if __name__ == "__main__":
    unittest.main()
