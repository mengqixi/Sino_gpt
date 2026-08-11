import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import desktop_launcher
from backend import runtime


class DesktopRuntimeTests(unittest.TestCase):
    def test_override_data_directory_is_respected(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory).resolve()
            with patch.dict(os.environ, {"SINO_GPT_DESKTOP_DATA_DIR": directory}):
                self.assertEqual(desktop_launcher.user_data_dir(), expected)

    def test_frozen_module_worker_uses_the_desktop_executable(self):
        with (
            patch.object(runtime.sys, "frozen", True, create=True),
            patch.object(runtime.sys, "executable", "SinoImageTool.exe"),
        ):
            command = runtime.module_worker_command("backend.services.recolor_worker", "in.json", "out.json")
        self.assertEqual(command, [
            "SinoImageTool.exe",
            "--sino-module-worker",
            "backend.services.recolor_worker",
            "in.json",
            "out.json",
        ])

    def test_source_module_worker_uses_python_module_mode(self):
        with (
            patch.object(runtime.sys, "frozen", False, create=True),
            patch.object(runtime.sys, "executable", sys.executable),
        ):
            command = runtime.module_worker_command("backend.services.recolor_worker", "in.json", "out.json")
        self.assertEqual(command[:3], [sys.executable, "-m", "backend.services.recolor_worker"])

    def test_frozen_cutout_worker_uses_internal_dispatch(self):
        with (
            patch.object(runtime.sys, "frozen", True, create=True),
            patch.object(runtime.sys, "executable", "SinoImageTool.exe"),
        ):
            command = runtime.cutout_worker_command("worker.py", "model.onnx", "in.png", "out.png")
        self.assertEqual(command, [
            "SinoImageTool.exe",
            "--sino-cutout-worker",
            "model.onnx",
            "in.png",
            "out.png",
        ])


if __name__ == "__main__":
    unittest.main()
