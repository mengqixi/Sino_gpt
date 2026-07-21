import io
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import UploadFile
from PIL import Image, ImageDraw

from backend.services import product_image_service as service
from backend.services import product_image_worker as worker


SCHEMA = """
CREATE TABLE api_configs (
    id INTEGER PRIMARY KEY,
    config_name TEXT,
    api_type TEXT,
    api_base_url TEXT,
    api_key TEXT,
    model_name TEXT,
    endpoint_path TEXT,
    method TEXT,
    request_content_type TEXT,
    auth_type TEXT,
    auth_header_name TEXT,
    auth_header_prefix TEXT,
    image_field_name TEXT,
    prompt_field_name TEXT,
    model_field_name TEXT,
    count_field_name TEXT,
    size_field_name TEXT,
    quality_field_name TEXT,
    extra_params_json TEXT,
    response_image_type TEXT,
    response_image_path TEXT,
    response_text_path TEXT,
    timeout_seconds INTEGER,
    enabled INTEGER,
    is_default INTEGER,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE product_image_tasks (
    id TEXT PRIMARY KEY,
    product_code TEXT NOT NULL,
    color TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft',
    analysis_config_id INTEGER,
    image_config_id INTEGER,
    analysis_used INTEGER NOT NULL DEFAULT 0,
    analysis_status TEXT,
    missing_roles_json TEXT NOT NULL,
    selected_assets_json TEXT NOT NULL,
    analysis_notes_json TEXT NOT NULL,
    error_message TEXT,
    generation_active INTEGER NOT NULL DEFAULT 0,
    inputs_deleted INTEGER NOT NULL DEFAULT 0,
    inputs_deleted_at TEXT,
    last_activity_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE product_image_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    media_type TEXT NOT NULL,
    file_name TEXT,
    file_path TEXT NOT NULL,
    file_size INTEGER,
    mime_type TEXT,
    width INTEGER,
    height INTEGER,
    duration_seconds REAL,
    sharpness REAL,
    parent_asset_id INTEGER,
    frame_time_seconds REAL,
    analysis_role TEXT,
    analysis_valid INTEGER,
    analysis_confidence INTEGER,
    analysis_reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES product_image_tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_asset_id) REFERENCES product_image_assets(id) ON DELETE CASCADE
);
CREATE TABLE product_image_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    variant TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    file_path TEXT,
    mime_type TEXT,
    width INTEGER,
    height INTEGER,
    source_asset_id INTEGER,
    api_config_id INTEGER,
    prompt TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, slot, variant),
    FOREIGN KEY(task_id) REFERENCES product_image_tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(source_asset_id) REFERENCES product_image_assets(id) ON DELETE SET NULL
);
CREATE TABLE product_image_references (
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,
    selected_asset_id INTEGER,
    status TEXT NOT NULL DEFAULT 'missing',
    selection_source TEXT,
    confidence INTEGER,
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(task_id, role),
    FOREIGN KEY(task_id) REFERENCES product_image_tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(selected_asset_id) REFERENCES product_image_assets(id) ON DELETE SET NULL
);
CREATE TABLE product_image_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    call_type TEXT NOT NULL,
    slot TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    api_config_id INTEGER,
    config_name TEXT,
    model_name TEXT,
    endpoint_path TEXT,
    prompt TEXT,
    response_preview_json TEXT,
    error_message TEXT,
    started_at TEXT,
    unknown_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, call_type, slot, attempt_no),
    FOREIGN KEY(task_id) REFERENCES product_image_tasks(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX one_analysis ON product_image_calls(task_id) WHERE call_type = 'analysis';
"""


class ProductImageTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "tasks.db"
        self.input_dir = self.root / "inputs"
        self.output_dir = self.root / "outputs"
        with self.session() as conn:
            conn.executescript(SCHEMA)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(service, "db_session", self.session))
        self.stack.enter_context(patch.object(worker, "db_session", self.session))
        self.stack.enter_context(patch.object(service, "PRODUCT_IMAGE_INPUT_DIR", self.input_dir))
        self.stack.enter_context(patch.object(service, "PRODUCT_IMAGE_OUTPUT_DIR", self.output_dir))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    @contextmanager
    def session(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create(self, code="SKU-1", color="黑色"):
        return service.create_task(code, color)

    def add_reference(self, task_id: str, role: str, *, media_type="image", parent_id=None) -> int:
        folder = self.input_dir / task_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{role}_{media_type}_{parent_id or 0}.jpg"
        path.write_bytes(b"reference")
        with self.session() as conn:
            cursor = conn.execute(
                """
                INSERT INTO product_image_assets (
                    task_id, slot, media_type, file_name, file_path, file_size,
                    mime_type, width, height, sharpness, parent_asset_id,
                    frame_time_seconds, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'image/jpeg', 1600, 1600, 123.4, ?, ?, '2026-01-01T00:00:00')
                """,
                (
                    task_id,
                    role if media_type == "image" else "video",
                    media_type,
                    path.name,
                    str(path),
                    path.stat().st_size,
                    parent_id,
                    2.5 if media_type == "frame" else None,
                ),
            )
            asset_id = int(cursor.lastrowid)
        return asset_id

    def select_all(self, task_id: str) -> dict[str, int]:
        selected = {}
        for role in service.REFERENCE_ROLES:
            asset_id = self.add_reference(task_id, role)
            service.select_reference(task_id, role, asset_id)
            selected[role] = asset_id
        with self.session() as conn:
            conn.execute(
                "UPDATE product_image_tasks SET analysis_used = 1, analysis_status = 'success', status = 'ready' WHERE id = ?",
                (task_id,),
            )
        return selected

    def test_create_task_builds_five_references_and_twelve_output_variants(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(len(first["references"]), 5)
        self.assertEqual(len(first["outputs"]), 6)
        self.assertTrue(all(set(item["variants"]) == {"highres", "800"} for item in first["outputs"]))
        self.assertEqual(first["call_summary"]["remaining_generation_calls"], 4)

    def test_history_limit_and_pruning_apply_to_sku_color_groups_not_versions(self):
        with patch.object(service, "prune_task_history", return_value=0):
            self.create("SKU-00")
            self.create("SKU-00")
            for index in range(1, 50):
                self.create(f"SKU-{index:02d}")
            self.create("SKU-50")
            self.create("SKU-50")
            self.create("SKU-50")

        with self.session() as conn:
            conn.execute("UPDATE product_image_tasks SET status = 'completed'")
            for index in range(51):
                conn.execute(
                    "UPDATE product_image_tasks SET created_at = ? WHERE product_code = ?",
                    (f"2026-01-01T00:00:{index:02d}", f"SKU-{index:02d}"),
                )

        history = service.list_task_history(50)

        self.assertEqual(len(history), 52)
        self.assertNotIn("SKU-00", {item["product_code"] for item in history})
        newest_versions = [item["version"] for item in history if item["product_code"] == "SKU-50"]
        self.assertEqual(newest_versions, [3, 2, 1])
        with self.session() as conn:
            removed = conn.execute(
                "SELECT COUNT(*) FROM product_image_tasks WHERE product_code = 'SKU-00'"
            ).fetchone()[0]
        self.assertEqual(removed, 0)

    def test_reference_must_belong_to_same_task(self):
        first = self.create("ONE")
        second = self.create("TWO")
        foreign_asset = self.add_reference(second["id"], "front")
        with self.assertRaisesRegex(service.ProductImageError, "不存在"):
            service.select_reference(first["id"], "front", foreign_asset)

    def test_one_asset_cannot_be_selected_for_two_reference_roles(self):
        task = self.create()
        asset_id = self.add_reference(task["id"], "front")
        service.select_reference(task["id"], "front", asset_id)

        with self.assertRaisesRegex(service.ProductImageConflict, "不能同时代表两个角度"):
            service.select_reference(task["id"], "back", asset_id)

        unchanged = service.get_task(task["id"])
        back = next(item for item in unchanged["references"] if item["role"] == "back")
        self.assertIsNone(back["selected_asset_id"])

    def test_image_upload_uses_media_normalization_and_records_local_quality(self):
        task = self.create()
        image = Image.new("RGB", (1200, 900), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((180, 180, 1020, 760), fill="#202020")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        uploaded = UploadFile(filename="front.png", file=buffer)
        assets = service.save_image_assets(task["id"], "front", [uploaded])
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["mime_type"], "image/jpeg")
        self.assertEqual(assets[0]["kind"], "image")
        stored, _, _ = service.asset_file(task["id"], assets[0]["id"])
        self.assertEqual(stored.suffix, ".jpg")
        with Image.open(stored) as normalized:
            self.assertEqual(normalized.format, "JPEG")

    def test_low_disk_rejects_upload_without_leaving_partial_file(self):
        task = self.create()
        image = Image.new("RGB", (900, 900), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        uploaded = UploadFile(filename="front.png", file=buffer)
        low_space = type("DiskUsage", (), {"free": service.MIN_FREE_DISK_BYTES - 1})()

        with (
            patch.object(service.shutil, "disk_usage", return_value=low_space),
            self.assertRaisesRegex(service.ProductImageError, "磁盘空间不足"),
        ):
            service.save_image_assets(task["id"], "front", [uploaded])

        task_input = self.input_dir / task["id"]
        self.assertFalse(any(path.is_file() for path in task_input.rglob("*")))

    def test_browser_frames_create_lightweight_video_parent_without_original_video(self):
        task = self.create()
        uploads = []
        for index, color in enumerate(("#111111", "#eeeeee"), start=1):
            image = Image.new("RGB", (1000, 800), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((120, 120, 880, 680), fill=color)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=85)
            buffer.seek(0)
            uploads.append(UploadFile(filename=f"frame-{index}.jpg", file=buffer))

        saved = service.save_browser_video_frames(
            task["id"],
            "bag.MOV",
            10.0,
            uploads,
        )

        self.assertEqual(len(saved["videos"]), 1)
        self.assertEqual(len(saved["frames"]), 2)
        video = saved["videos"][0]
        self.assertEqual(video["source_mode"], "browser_frames")
        self.assertFalse(video["original_video_uploaded"])
        self.assertIsNone(video["file_url"])
        self.assertTrue(all(frame["parent_asset_id"] == video["id"] for frame in saved["frames"]))
        self.assertEqual(
            [round(frame["frame_time_seconds"], 1) for frame in saved["frames"]],
            [2.5, 7.5],
        )
        stored_files = [path for path in (self.input_dir / task["id"]).rglob("*") if path.is_file()]
        self.assertFalse(any(path.suffix.lower() in {".mp4", ".mov"} for path in stored_files))
        self.assertEqual(sum(path.suffix.lower() == ".json" for path in stored_files), 1)

    def test_server_video_fallback_accepts_only_one_file_per_request(self):
        task = self.create()
        files = [
            UploadFile(filename="one.mp4", file=io.BytesIO(b"one")),
            UploadFile(filename="two.mp4", file=io.BytesIO(b"two")),
        ]
        with self.assertRaisesRegex(service.ProductImageError, "每次只能上传 1 个视频"):
            service.save_video_assets(task["id"], files)

    def test_video_frame_can_be_selected_without_second_analysis(self):
        task = self.create()
        video_path = self.input_dir / task["id"] / "video.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"video")
        with self.session() as conn:
            video_id = int(
                conn.execute(
                    """
                    INSERT INTO product_image_assets
                        (task_id, slot, media_type, file_name, file_path, created_at)
                    VALUES (?, 'video', 'video', 'video.mp4', ?, '2026-01-01T00:00:00')
                    """,
                    (task["id"], str(video_path)),
                ).lastrowid
            )
            conn.execute(
                "UPDATE product_image_tasks SET analysis_used = 1, analysis_status = 'success' WHERE id = ?",
                (task["id"],),
            )
        frame_id = self.add_reference(task["id"], "video", media_type="frame", parent_id=video_id)
        selected = service.select_reference(task["id"], "back", frame_id)
        back = next(item for item in selected["references"] if item["role"] == "back")
        self.assertEqual(back["selected_asset_id"], frame_id)
        self.assertTrue(selected["analysis_used"])
        self.assertEqual(len(selected["calls"]), 0)

    def test_prepare_analysis_is_enforced_once_in_database(self):
        task = self.create()
        self.add_reference(task["id"], "front")
        config = {"id": 9, "enabled": True, "api_type": "text_analysis", "config_name": "vision"}
        with patch.object(worker, "_enabled_config", return_value=config):
            call_id = worker.prepare_analysis(task["id"], 9)
            self.assertGreater(call_id, 0)
            with self.assertRaisesRegex(service.ProductImageConflict, "只能调用一次"):
                worker.prepare_analysis(task["id"], 9)
        current = service.get_task(task["id"])
        self.assertIsNotNone(current["analysis_used_at"])
        self.assertEqual(current["analysis_api_config_id"], 9)

    def test_generation_worker_uses_one_matching_reference_and_strict_order(self):
        task = self.create()
        selected = self.select_all(task["id"])
        config = {"id": 3, "enabled": True, "api_type": "image_generation", "config_name": "fast"}
        slots = worker.prepare_generation(task["id"], 3)
        observed = []

        def fake_slot(task_id, slot, _config, _task, source):
            observed.append((slot, int(source["selected_asset_id"]), source["file_path"]))
            return "success"

        with (
            patch.object(worker, "_enabled_config", return_value=config),
            patch.object(worker, "ensure_local_outputs"),
            patch.object(worker, "_run_generation_slot", side_effect=fake_slot),
        ):
            worker.run_generation(task["id"], slots, 3)
        self.assertEqual([item[0] for item in observed], list(service.API_OUTPUT_SLOTS))
        self.assertEqual(
            [item[1] for item in observed],
            [selected["front"], selected["back"], selected["semi_side"], selected["top"]],
        )

    def test_generation_stops_immediately_on_unknown(self):
        task = self.create()
        self.select_all(task["id"])
        config = {"id": 3, "enabled": True, "api_type": "image_generation", "config_name": "fast"}
        slots = list(service.API_OUTPUT_SLOTS)
        with self.session() as conn:
            conn.execute(
                "UPDATE product_image_tasks SET generation_active = 1, status = 'generating' WHERE id = ?",
                (task["id"],),
            )
        observed = []

        def fake_slot(_task_id, slot, _config, _task, _source):
            observed.append(slot)
            return "unknown" if slot == "back" else "success"

        with (
            patch.object(worker, "_enabled_config", return_value=config),
            patch.object(worker, "ensure_local_outputs"),
            patch.object(worker, "_run_generation_slot", side_effect=fake_slot),
        ):
            worker.run_generation(task["id"], slots, 3)
        self.assertEqual(observed, ["front_main", "back"])

    def test_generation_setup_failure_releases_active_task(self):
        task = self.create()
        self.select_all(task["id"])
        with self.session() as conn:
            conn.execute(
                "UPDATE product_image_tasks SET generation_active = 1, status = 'generating' WHERE id = ?",
                (task["id"],),
            )

        with patch.object(worker, "_enabled_config", side_effect=service.ProductImageError("配置损坏")):
            worker.run_generation(task["id"], ["front_main"], 3)

        failed = service.get_task(task["id"])
        self.assertFalse(failed["generation_active"])
        self.assertEqual(failed["internal_status"], "paused_failed")
        self.assertEqual(failed["error_message"], "配置损坏")

    def test_recovery_releases_generation_claim_created_before_background_call(self):
        task = self.create()
        self.select_all(task["id"])
        with self.session() as conn:
            conn.execute(
                "UPDATE product_image_tasks SET generation_active = 1, status = 'generating' WHERE id = ?",
                (task["id"],),
            )
            conn.execute(
                "UPDATE product_image_outputs SET status = 'running' WHERE task_id = ? AND slot = 'front_main'",
                (task["id"],),
            )

        self.assertEqual(worker.recover_interrupted_calls(), 1)
        recovered = service.get_task(task["id"])
        self.assertFalse(recovered["generation_active"])
        self.assertEqual(recovered["internal_status"], "paused_failed")
        front = next(item for item in recovered["outputs"] if item["slot"] == "front_main")
        self.assertEqual(front["status"], "failed")

    def test_recovery_finishes_unknown_request_left_waiting_by_crash(self):
        task = self.create()
        self.select_all(task["id"])
        with self.session() as conn:
            conn.execute(
                "UPDATE product_image_tasks SET generation_active = 1, status = 'paused_unknown' WHERE id = ?",
                (task["id"],),
            )
            conn.execute(
                """
                INSERT INTO product_image_calls (
                    task_id, call_type, slot, attempt_no, status, api_config_id,
                    unknown_at, created_at, updated_at
                ) VALUES (?, 'generation', 'back', 1, 'unknown', 3,
                          '2026-01-01T00:00:01', '2026-01-01T00:00:00', '2026-01-01T00:00:01')
                """,
                (task["id"],),
            )

        self.assertEqual(worker.recover_interrupted_calls(), 1)
        recovered = service.get_task(task["id"])
        self.assertFalse(recovered["generation_active"])
        self.assertEqual(recovered["internal_status"], "paused_unknown")
        self.assertFalse(recovered["call_plan"]["unknown_request_still_running"])
        self.assertIsNotNone(recovered["calls"][-1]["finished_at"])

    def test_recovery_keeps_billing_warning_for_finished_unknown_orphan(self):
        task = self.create()
        self.select_all(task["id"])
        with self.session() as conn:
            conn.execute(
                "UPDATE product_image_tasks SET generation_active = 1, status = 'generating' WHERE id = ?",
                (task["id"],),
            )
            conn.execute(
                """
                INSERT INTO product_image_calls (
                    task_id, call_type, slot, attempt_no, status, api_config_id,
                    unknown_at, finished_at, created_at, updated_at
                ) VALUES (?, 'generation', 'top', 1, 'unknown', 3,
                          '2026-01-01T00:00:01', '2026-01-01T00:00:02',
                          '2026-01-01T00:00:00', '2026-01-01T00:00:02')
                """,
                (task["id"],),
            )

        self.assertEqual(worker.recover_interrupted_calls(), 1)
        recovered = service.get_task(task["id"])
        self.assertFalse(recovered["generation_active"])
        self.assertEqual(recovered["internal_status"], "paused_unknown")
        self.assertTrue(recovered["call_plan"]["unknown_retry_warning"])
        self.assertIn("可能已经扣费", recovered["error_message"])

    def test_single_regenerate_requires_ack_for_last_unknown_slot_call(self):
        task = self.create()
        self.select_all(task["id"])
        config = {"id": 3, "enabled": True, "api_type": "image_generation", "config_name": "fast"}
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO product_image_calls (
                    task_id, call_type, slot, attempt_no, status, api_config_id,
                    unknown_at, created_at, updated_at
                ) VALUES (?, 'generation', 'back', 1, 'unknown', 3,
                          '2026-01-01T00:00:01', '2026-01-01T00:00:00', '2026-01-01T00:00:01')
                """,
                (task["id"],),
            )

        with patch.object(worker, "_enabled_config", return_value=config):
            with self.assertRaisesRegex(service.ProductImageConflict, "仍在后台等待"):
                worker.prepare_generation(task["id"], 3, mode="single", slot="back")
            with self.session() as conn:
                conn.execute(
                    "UPDATE product_image_calls SET finished_at = '2026-01-01T00:00:02' WHERE task_id = ?",
                    (task["id"],),
                )
            with self.assertRaisesRegex(service.ProductImageConflict, "确认风险"):
                worker.prepare_generation(task["id"], 3, mode="single", slot="back")
            slots = worker.prepare_generation(
                task["id"],
                3,
                mode="single",
                slot="back",
                acknowledge_possible_charge=True,
            )
        self.assertEqual(slots, ["back"])

    def test_generation_local_prep_does_not_run_server_transparency(self):
        task = self.create()
        self.select_all(task["id"])
        with (
            patch.object(service, "render_default_transparent") as transparent,
            patch.object(service, "render_logo_crop") as logo,
        ):
            service.ensure_local_outputs(task["id"])
        transparent.assert_not_called()
        logo.assert_called_once_with(task["id"], 0.0, 0.0, 1.0, 1.0)

    def test_transparent_png_multipart_path_saves_alpha_variants(self):
        task = self.create()
        front_id = self.add_reference(task["id"], "front")
        service.select_reference(task["id"], "front", front_id)
        image = Image.new("RGBA", (320, 240), (30, 40, 50, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((50, 30, 270, 220), fill=(180, 30, 40, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)

        saved = service.save_transparent_upload(
            task["id"],
            UploadFile(filename="transparent.png", file=buffer),
        )

        output = next(item for item in saved["outputs"] if item["slot"] == "front_transparent")
        self.assertEqual(output["status"], "success")
        self.assertIsNotNone(output["current_result"])

    def test_source_cleanup_preserves_results_and_history_metadata(self):
        task = self.create()
        source_id = self.add_reference(task["id"], "front")
        service.select_reference(task["id"], "front", source_id)
        result_dir = self.output_dir / task["id"] / "front_main"
        result_dir.mkdir(parents=True, exist_ok=True)
        result = result_dir / "800.jpg"
        result.write_bytes(b"result")
        with self.session() as conn:
            conn.execute(
                """
                UPDATE product_image_outputs SET status = 'success', file_path = ?, mime_type = 'image/jpeg'
                WHERE task_id = ? AND slot = 'front_main' AND variant = '800'
                """,
                (str(result), task["id"]),
            )
            conn.execute("UPDATE product_image_tasks SET status = 'completed' WHERE id = ?", (task["id"],))
        service.delete_task_sources(task["id"])
        current = service.get_task(task["id"])
        self.assertTrue(current["inputs_deleted"])
        self.assertIsNotNone(current["source_deleted_at"])
        self.assertEqual(current["assets"], [])
        self.assertTrue(result.exists())
        self.assertEqual(service.list_task_history()[0]["id"], task["id"])

    def test_zip_contains_exact_two_folders_and_six_semantic_names(self):
        task = self.create()
        for slot in service.OUTPUT_SLOTS:
            for variant in service.OUTPUT_VARIANTS:
                suffix = ".png" if slot == "front_transparent" else ".jpg"
                path = self.output_dir / task["id"] / slot / f"{variant}{suffix}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"{slot}-{variant}".encode())
                with self.session() as conn:
                    conn.execute(
                        """
                        UPDATE product_image_outputs
                        SET status = 'success', file_path = ?, mime_type = ?, width = 800, height = 800
                        WHERE task_id = ? AND slot = ? AND variant = ?
                        """,
                        (
                            str(path),
                            "image/png" if suffix == ".png" else "image/jpeg",
                            task["id"],
                            slot,
                            variant,
                        ),
                    )
        archive_path = service.build_task_zip(task["id"])
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
        expected = {
            f"SKU-1_黑色/{folder}/{file_name}"
            for folder in ("高清", "800x800")
            for file_name in service.ZIP_NAMES.values()
        }
        self.assertEqual(names, expected)


if __name__ == "__main__":
    unittest.main()
