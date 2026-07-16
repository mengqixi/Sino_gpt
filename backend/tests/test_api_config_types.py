import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException

from backend.routers.generate import GeneratePayload, generate
from backend.services.api_config_service import IMAGE_API_TYPE, TEXT_API_TYPE, require_config_type
from backend.services.vip_organizer_service import _analysis_config


class ApiConfigTypeTests(unittest.TestCase):
    def test_config_type_guard_rejects_wrong_usage(self):
        require_config_type({"api_type": IMAGE_API_TYPE}, IMAGE_API_TYPE)
        with self.assertRaisesRegex(ValueError, "不能用于文本分析"):
            require_config_type({"api_type": IMAGE_API_TYPE}, TEXT_API_TYPE)

    def test_generate_rejects_text_analysis_config_before_creating_job(self):
        payload = GeneratePayload(
            task_type="custom",
            uploaded_image_id=1,
            final_prompt="test",
            api_config_id=9,
        )
        with (
            patch("backend.routers.generate.get_upload", return_value={"id": 1, "file_path": "test.png"}),
            patch("backend.routers.generate.get_config", return_value={"id": 9, "enabled": True, "api_type": TEXT_API_TYPE}),
            patch("backend.routers.generate.create_job") as create_job,
        ):
            with self.assertRaises(HTTPException) as raised:
                generate(payload, BackgroundTasks())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("不能用于生图", raised.exception.detail)
        create_job.assert_not_called()

    def test_organizer_rejects_image_generation_config(self):
        with patch(
            "backend.services.vip_organizer_service.get_config",
            return_value={"id": 3, "enabled": True, "api_type": IMAGE_API_TYPE},
        ):
            with self.assertRaisesRegex(ValueError, "不能用于文本分析"):
                _analysis_config(3)


if __name__ == "__main__":
    unittest.main()
