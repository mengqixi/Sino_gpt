import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fastapi import UploadFile
from PIL import Image, ImageDraw

from backend import database
from backend.routers import vip_organizer as organizer_router
from backend.services import vip_organizer_service as service


class VipOrganizerSessionIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_path = root / "app.db"
        self.organizer_root = root / "vip_organizer"
        self.patches = [
            patch.object(database, "DB_PATH", self.db_path),
            patch.object(service, "ORGANIZER_DATA_DIR", self.organizer_root),
            patch.object(service, "ORGANIZER_UPLOAD_DIR", self.organizer_root / "uploads"),
            patch.object(service, "ORGANIZER_RESULT_DIR", self.organizer_root / "results"),
        ]
        for item in self.patches:
            item.start()
        with database.db_session() as conn:
            conn.executescript(
                """
                CREATE TABLE vip_organizer_sessions (
                    id TEXT PRIMARY KEY,
                    created_at DATETIME,
                    updated_at DATETIME
                );
                CREATE TABLE vip_organizer_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    file_name TEXT,
                    file_path TEXT NOT NULL,
                    file_size INTEGER,
                    mime_type TEXT,
                    width INTEGER,
                    height INTEGER,
                    created_at DATETIME,
                    FOREIGN KEY(session_id) REFERENCES vip_organizer_sessions(id) ON DELETE CASCADE
                );
                """
            )

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def _add_asset(self, session_id: str, name: str) -> Path:
        folder = service._session_upload_dir(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(b"test")
        with database.db_session() as conn:
            conn.execute(
                """
                INSERT INTO vip_organizer_assets
                    (session_id, asset_type, file_name, file_path, created_at)
                VALUES (?, 'product', ?, ?, ?)
                """,
                (session_id, name, str(path), database.now_iso()),
            )
        return path

    def _add_image_asset(self, session_id: str, name: str = "product.png") -> tuple[int, Path]:
        folder = service._session_upload_dir(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        image = Image.new("RGB", (160, 160), "white")
        ImageDraw.Draw(image).rounded_rectangle((35, 45, 125, 140), radius=12, fill="#244a73")
        image.save(path)
        with database.db_session() as conn:
            cursor = conn.execute(
                """
                INSERT INTO vip_organizer_assets
                    (session_id, asset_type, file_name, file_path, width, height, created_at)
                VALUES (?, 'product', ?, ?, 160, 160, ?)
                """,
                (session_id, name, str(path), database.now_iso()),
            )
            image_id = int(cursor.lastrowid)
        return image_id, path

    def test_replacing_one_session_does_not_delete_another(self):
        session_a = service.start_session()["session_id"]
        asset_a = self._add_asset(session_a, "a.jpg")
        session_b = service.start_session()["session_id"]
        asset_b = self._add_asset(session_b, "b.jpg")

        session_b_next = service.start_session(session_b)["session_id"]

        self.assertTrue(asset_a.exists())
        self.assertFalse(asset_b.exists())
        with database.db_session() as conn:
            ids = {row["id"] for row in conn.execute("SELECT id FROM vip_organizer_sessions")}
        self.assertEqual(ids, {session_a, session_b_next})

        service.delete_session(session_a)
        self.assertFalse(asset_a.exists())
        with database.db_session() as conn:
            ids = {row["id"] for row in conn.execute("SELECT id FROM vip_organizer_sessions")}
        self.assertEqual(ids, {session_b_next})

    def test_resume_session_restores_assets_without_replacing_session(self):
        session_id = service.start_session()["session_id"]
        product_path = self._add_asset(session_id, "product.jpg")
        model_path = self._add_asset(session_id, "model.jpg")
        tag_path = self._add_asset(session_id, "tag.jpg")
        with database.db_session() as conn:
            rows = conn.execute(
                "SELECT id, file_path FROM vip_organizer_assets ORDER BY id"
            ).fetchall()
            asset_types = {
                str(product_path): "product",
                str(model_path): "model",
                str(tag_path): "tag",
            }
            for row in rows:
                conn.execute(
                    "UPDATE vip_organizer_assets SET asset_type = ? WHERE id = ?",
                    (asset_types[row["file_path"]], row["id"]),
                )

        resumed = service.resume_session(session_id)

        self.assertEqual(resumed["session_id"], session_id)
        self.assertEqual(
            {name: [item["file_name"] for item in items] for name, items in resumed["assets"].items()},
            {
                "product": ["product.jpg"],
                "model": ["model.jpg"],
                "tag": ["tag.jpg"],
            },
        )
        for items in resumed["assets"].values():
            for item in items:
                self.assertEqual(
                    set(item),
                    {"image_id", "file_name", "preview_url", "original_url", "width", "height"},
                )
        with database.db_session() as conn:
            self.assertIsNotNone(
                conn.execute(
                    "SELECT id FROM vip_organizer_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
            )

    def test_resume_session_rejects_expired_session(self):
        session_id = service.start_session()["session_id"]
        with database.db_session() as conn:
            conn.execute(
                "UPDATE vip_organizer_sessions SET updated_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00", session_id),
            )

        service._cleanup_expired_sessions()

        with self.assertRaises(ValueError):
            service.resume_session(session_id)
        self.assertTrue(service._session_tombstone_path(session_id).is_file())

    def test_delete_asset_only_deletes_asset_owned_by_session(self):
        session_a = service.start_session()["session_id"]
        asset_a = self._add_asset(session_a, "a.jpg")
        session_b = service.start_session()["session_id"]
        asset_b = self._add_asset(session_b, "b.jpg")
        with database.db_session() as conn:
            asset_a_id = conn.execute(
                "SELECT id FROM vip_organizer_assets WHERE session_id = ?",
                (session_a,),
            ).fetchone()["id"]

        with self.assertRaises(ValueError):
            service.delete_asset(session_b, asset_a_id)
        self.assertTrue(asset_a.exists())

        service.delete_asset(session_a, asset_a_id)
        self.assertFalse(asset_a.exists())
        self.assertTrue(asset_b.exists())
        with database.db_session() as conn:
            remaining = conn.execute(
                "SELECT session_id FROM vip_organizer_assets"
            ).fetchall()
        self.assertEqual([row["session_id"] for row in remaining], [session_b])

    def test_prepared_cutout_is_downloadable_without_becoming_an_uploaded_asset(self):
        session_id = service.start_session()["session_id"]
        source = Image.new("RGB", (320, 320), "white")
        ImageDraw.Draw(source).rounded_rectangle((70, 80, 250, 270), radius=22, fill="#244a73")
        payload = io.BytesIO()
        source.save(payload, format="JPEG", quality=95)
        payload.seek(0)

        from backend.services.cutout_pipeline_worker import _handle as run_cutout_inline

        with patch.object(
            service,
            "run_heavy_task",
            side_effect=lambda _module, worker_payload, **_kwargs: run_cutout_inline(worker_payload),
        ):
            result = service.prepare_product_cutout(
                session_id,
                UploadFile(filename="front.jpg", file=payload),
            )

        transparent = service.prepared_cutout_file(session_id, result["prepared_id"], "transparent")
        gray = service.prepared_cutout_file(session_id, result["prepared_id"], "gray")
        self.assertTrue(transparent.is_file())
        self.assertTrue(gray.is_file())
        with Image.open(transparent) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)
        with Image.open(gray) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.getpixel((0, 0)), (150, 152, 149))
        with database.db_session() as conn:
            asset_count = conn.execute(
                "SELECT COUNT(*) AS total FROM vip_organizer_assets WHERE session_id = ?",
                (session_id,),
            ).fetchone()["total"]
        self.assertEqual(asset_count, 0)
        with self.assertRaises(ValueError):
            service.prepared_cutout_file(session_id, result["prepared_id"], "invalid")

    def test_organizer_layer_is_generated_once_and_reused_by_fast_slot_preview(self):
        session_id = service.start_session()["session_id"]
        image_id, _ = self._add_image_asset(session_id)
        adjustment = {
            "crop_x": 0,
            "crop_y": 0,
            "crop_width": 1,
            "crop_height": 1,
            "zoom": 1.15,
            "offset_x": 0.08,
            "offset_y": -0.04,
        }

        def render_layer_inline(_module, payload, **_kwargs):
            self.assertEqual(payload["operation"], "organizer_layer")
            return service._render_organizer_layer_cache(payload["image_id"], payload["adjustment"])

        with patch.object(service, "run_heavy_task", side_effect=render_layer_inline) as worker:
            first = service.asset_organizer_layer_info(image_id, adjustment)
            second = service.asset_organizer_layer_info(image_id, adjustment)

        self.assertEqual(worker.call_count, 1)
        self.assertEqual(first, second)
        self.assertTrue((self.organizer_root / "uploads" / session_id / "render-cache").is_dir())

        slots = [{
            "file_name": "2.jpg",
            "image_ids": [image_id],
            "adjustments": [adjustment],
        }]
        with patch.object(service, "run_heavy_task") as worker:
            result = service.render_slot_preview(session_id, slots, {}, "2.jpg")

        worker.assert_not_called()
        self.assertEqual(result["file_name"], "2.jpg")
        preview_parts = result["preview_url"].removeprefix("/api/vip-organizer/previews/").split("/")
        preview_path = service.preview_file(preview_parts[0], preview_parts[1], preview_parts[2])
        self.assertTrue(preview_path.is_file())
        with Image.open(preview_path) as preview:
            self.assertEqual(preview.size, (800, 800))

    def test_organizer_layer_algorithm_version_changes_disk_url_and_lru_key(self):
        session_id = service.start_session()["session_id"]
        image_id, source_path = self._add_image_asset(session_id)
        modified_ns = source_path.stat().st_mtime_ns
        crop_key = service._crop_cache_key(None)
        service._cached_product_cutout.cache_clear()

        with (
            patch.object(service, "ORGANIZER_LAYER_RENDER_VERSION", 101),
            patch.object(
                service,
                "_product_cutout",
                return_value=Image.new("RGBA", (20, 20), "red"),
            ) as renderer,
        ):
            first = service._cached_product_cutout(image_id, modified_ns, crop_key)
            first_path = service._organizer_layer_cache_path(image_id, modified_ns, crop_key)
            first_info = service.asset_organizer_layer_info(image_id)
        renderer.assert_called_once()

        with (
            patch.object(service, "ORGANIZER_LAYER_RENDER_VERSION", 102),
            patch.object(
                service,
                "_product_cutout",
                return_value=Image.new("RGBA", (20, 20), "blue"),
            ) as renderer,
        ):
            second = service._cached_product_cutout(image_id, modified_ns, crop_key)
            second_path = service._organizer_layer_cache_path(image_id, modified_ns, crop_key)
            second_info = service.asset_organizer_layer_info(image_id)
        renderer.assert_called_once()

        self.assertNotEqual(first_path, second_path)
        self.assertIn("organizer-layer-v101-", first_path.name)
        self.assertIn("organizer-layer-v102-", second_path.name)
        self.assertIn("&v=101", first_info["url"])
        self.assertIn("&v=102", second_info["url"])
        self.assertEqual(first.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(second.getpixel((0, 0)), (0, 0, 255, 255))
        service._cached_product_cutout.cache_clear()

    def test_jd5_fast_preview_reuses_disk_layer_without_decoding_or_cutting_source(self):
        session_id = service.start_session()["session_id"]
        image_id, source_path = self._add_image_asset(session_id)
        adjustment = service._normalize_adjustment(None)
        with patch.object(
            service,
            "_product_cutout",
            return_value=Image.new("RGBA", (120, 150), (40, 60, 80, 255)),
        ):
            service._cached_product_cutout(
                image_id,
                source_path.stat().st_mtime_ns,
                service._crop_cache_key(adjustment),
            )
        service._cached_product_cutout.cache_clear()
        slots = [{
            "file_name": "5.jpg",
            "image_ids": [image_id],
            "adjustments": [adjustment],
            "logo_color": "black",
        }]

        with (
            patch.object(service, "_product_cutout", side_effect=AssertionError("cutout recomputed")),
            patch.object(service, "_load_image", side_effect=AssertionError("source decoded")),
            patch.object(service, "run_heavy_task") as worker,
        ):
            result = service.render_slot_preview(
                session_id,
                slots,
                {"product_length": "180", "product_height": "105"},
                "5.jpg",
                "jd",
                "800",
            )

        worker.assert_not_called()
        self.assertEqual(result["file_name"], "5.jpg")
        preview_parts = result["preview_url"].removeprefix(
            "/api/vip-organizer/previews/"
        ).split("/")
        self.assertTrue(
            service.preview_file(
                preview_parts[0], preview_parts[1], preview_parts[2]
            ).is_file()
        )

    def test_deleted_session_cannot_be_recreated_by_finishing_preview(self):
        session_id = service.start_session()["session_id"]
        slot = {
            "image_ids": [1],
            "adjustments": [],
            "logo_color": "black",
        }

        def delete_during_render(*_args, **_kwargs):
            service.delete_session(session_id)
            return Image.new("RGB", (16, 16), "red")

        with patch.object(service, "_render_slot_image", side_effect=delete_during_render):
            with self.assertRaises(ValueError):
                service._render_cached_slot_preview(
                    session_id,
                    "2.jpg",
                    slot,
                    {},
                    "vip",
                )

        self.assertFalse(service._session_upload_dir(session_id).exists())
        self.assertFalse(service._session_result_dir(session_id).exists())
        self.assertTrue(service._session_tombstone_path(session_id).is_file())

    def test_orphan_session_directories_are_swept_safely(self):
        orphan_id = "f" * 32
        upload = service._session_upload_dir(orphan_id)
        result = service._session_result_dir(orphan_id)
        upload.mkdir(parents=True)
        result.mkdir(parents=True)
        (result / "stale.jpg").write_bytes(b"stale")

        service._cleanup_orphan_session_directories()

        self.assertFalse(upload.exists())
        self.assertFalse(result.exists())

    def test_orphan_sweep_rechecks_session_created_after_database_snapshot(self):
        session_id = "e" * 32
        upload = service._session_upload_dir(session_id)
        result = service._session_result_dir(session_id)
        upload.mkdir(parents=True)
        result.mkdir(parents=True)
        original_is_active = service._session_is_active
        activated = False

        def activate_before_delete(candidate: str) -> bool:
            nonlocal activated
            if candidate == session_id and not activated:
                activated = True
                timestamp = database.now_iso()
                with database.db_session() as conn:
                    conn.execute(
                        """
                        INSERT INTO vip_organizer_sessions
                            (id, created_at, updated_at)
                        VALUES (?, ?, ?)
                        """,
                        (session_id, timestamp, timestamp),
                    )
            return original_is_active(candidate)

        with patch.object(
            service,
            "_session_is_active",
            side_effect=activate_before_delete,
        ):
            service._cleanup_orphan_session_directories()

        self.assertTrue(activated)
        self.assertTrue(upload.is_dir())
        self.assertTrue(result.is_dir())

    def test_preview_cache_version_change_forces_a_fresh_render(self):
        session_id = service.start_session()["session_id"]
        slot = {
            "image_ids": [1],
            "adjustments": [],
            "logo_color": "black",
        }

        with patch.object(
            service,
            "_render_slot_image",
            return_value=Image.new("RGB", (16, 16), "black"),
        ) as renderer:
            first_url = service._render_cached_slot_preview(
                session_id,
                "2.jpg",
                slot,
                {},
                "vip",
            )
            cached_url = service._render_cached_slot_preview(
                session_id,
                "2.jpg",
                slot,
                {},
                "vip",
            )

        self.assertEqual(first_url, cached_url)
        self.assertEqual(renderer.call_count, 1)

        with (
            patch.object(service, "PREVIEW_RENDER_VERSION", service.PREVIEW_RENDER_VERSION + 1),
            patch.object(
                service,
                "_render_slot_image",
                return_value=Image.new("RGB", (16, 16), "red"),
            ) as renderer,
        ):
            refreshed_url = service._render_cached_slot_preview(
                session_id,
                "2.jpg",
                slot,
                {},
                "vip",
            )

        self.assertNotEqual(first_url, refreshed_url)
        renderer.assert_called_once()

        with (
            patch.object(
                service,
                "ORGANIZER_LAYER_RENDER_VERSION",
                service.ORGANIZER_LAYER_RENDER_VERSION + 1,
            ),
            patch.object(
                service,
                "_render_slot_image",
                return_value=Image.new("RGB", (16, 16), "blue"),
            ) as renderer,
        ):
            refreshed_layer_url = service._render_cached_slot_preview(
                session_id,
                "2.jpg",
                slot,
                {},
                "vip",
            )

        self.assertNotEqual(first_url, refreshed_layer_url)
        renderer.assert_called_once()

    def test_full_preview_cache_hit_does_not_start_heavy_worker(self):
        session_id = service.start_session()["session_id"]
        file_names = [name for name, _, _, _ in service.SLOT_DEFINITIONS]
        cached_urls = {
            name: f"/api/vip-organizer/previews/{session_id}/{index:012x}/{name}"
            for index, name in enumerate(file_names, start=1)
        }

        with (
            patch.object(
                service,
                "_existing_cached_slot_preview",
                side_effect=lambda _session_id, file_name, *_args: cached_urls[file_name],
            ),
            patch.object(service, "run_heavy_task") as worker,
        ):
            result = service.render_previews(session_id, [], {}, "vip", "800")

        worker.assert_not_called()
        self.assertEqual(list(result["previews"]), file_names)
        self.assertEqual(result["previews"], cached_urls)
        self.assertEqual(result["missing"], [])

    def test_cached_preview_disappearing_during_touch_is_a_cache_miss(self):
        session_id = service.start_session()["session_id"]
        file_name = service.SLOT_DEFINITIONS[0][0]
        slot = {"image_ids": [], "adjustments": [], "logo_color": "black"}
        preview_id = service._preview_cache_id(file_name, slot, {}, "vip", "800")
        folder = service._session_result_dir(session_id) / "previews" / preview_id
        folder.mkdir(parents=True)
        (folder / file_name).write_bytes(b"cached")

        with patch.object(service.os, "utime", side_effect=FileNotFoundError):
            result = service._existing_cached_slot_preview(
                session_id,
                file_name,
                slot,
                {},
                "vip",
                "800",
            )

        self.assertIsNone(result)

    def test_preview_cache_pruning_is_safe_when_requests_run_concurrently(self):
        session_id = service.start_session()["session_id"]
        preview_root = service._session_result_dir(session_id) / "previews"
        preview_root.mkdir(parents=True, exist_ok=True)
        for index in range(service.MAX_PREVIEW_CACHE_ENTRIES + 120):
            folder = preview_root / f"{index:012x}"
            folder.mkdir()
            (folder / "preview.jpg").write_bytes(b"preview")

        worker_count = 12
        barrier = Barrier(worker_count)

        def prune_once(_index: int) -> None:
            barrier.wait()
            service._prune_preview_cache(session_id)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            list(executor.map(prune_once, range(worker_count)))

        remaining = [path for path in preview_root.iterdir() if path.is_dir()]
        self.assertLessEqual(len(remaining), service.MAX_PREVIEW_CACHE_ENTRIES)

    def test_partial_preview_cache_only_sends_misses_to_heavy_worker(self):
        session_id = service.start_session()["session_id"]
        file_names = [name for name, _, _, _ in service.SLOT_DEFINITIONS]
        cached_names = set(file_names[::2])
        uncached_names = [name for name in file_names if name not in cached_names]
        missing_name = uncached_names[-1]

        def cached_preview(_session_id, file_name, *_args):
            if file_name not in cached_names:
                return None
            return f"/cached/{file_name}"

        def render_missing(_module, payload, **_kwargs):
            self.assertEqual(payload["file_names"], uncached_names)
            return {
                "previews": {
                    name: f"/rendered/{name}"
                    for name in uncached_names
                    if name != missing_name
                },
                "missing": [missing_name],
            }

        with (
            patch.object(
                service,
                "_existing_cached_slot_preview",
                side_effect=cached_preview,
            ),
            patch.object(
                service,
                "run_heavy_task",
                side_effect=render_missing,
            ) as worker,
        ):
            result = service.render_previews(session_id, [], {}, "vip", "800")

        worker.assert_called_once()
        self.assertEqual(
            list(result["previews"]),
            [name for name in file_names if name != missing_name],
        )
        self.assertEqual(result["missing"], [missing_name])

    def test_requested_preview_batch_never_expands_linked_model_slot(self):
        session_id = service.start_session()["session_id"]
        requested_names = ["1.jpg", "2.jpg", "3.jpg", "4.jpg", "15.jpg"]
        missing_names = ["3.jpg", "15.jpg"]

        def render_requested(_module, payload, **_kwargs):
            self.assertEqual(payload["file_names"], requested_names)
            self.assertNotIn("50.jpg", payload["file_names"])
            return {
                "previews": {
                    name: f"/rendered/{name}"
                    for name in requested_names
                    if name not in missing_names
                },
                "missing": missing_names,
            }

        with (
            patch.object(
                service,
                "_existing_cached_slot_preview",
                return_value=None,
            ) as cache_lookup,
            patch.object(
                service,
                "run_heavy_task",
                side_effect=render_requested,
            ) as worker,
        ):
            result = service.render_previews(
                session_id,
                [{"file_name": "1.jpg", "image_ids": []}],
                {},
                "vip",
                "800",
                requested_names,
            )

        worker.assert_called_once()
        self.assertEqual(cache_lookup.call_count, len(requested_names))
        self.assertEqual(
            list(result["previews"]),
            [name for name in requested_names if name not in missing_names],
        )
        self.assertEqual(result["missing"], missing_names)
        self.assertTrue(
            set(result["previews"]).union(result["missing"]).issubset(requested_names)
        )

    def test_preview_router_forwards_requested_file_names(self):
        requested_names = ["1.jpg", "2.jpg", "3.jpg", "4.jpg", "15.jpg"]
        payload = organizer_router.PreviewPayload(
            session_id="a" * 32,
            slots=[],
            product_info={},
            platform="vip",
            target_folder="800",
            preview_file_names=requested_names,
        )

        with patch.object(
            organizer_router,
            "render_previews",
            return_value={"previews": {}, "missing": requested_names},
        ) as renderer:
            result = organizer_router.preview(payload)

        renderer.assert_called_once_with(
            payload.session_id,
            payload.slots,
            payload.product_info,
            payload.platform,
            payload.target_folder,
            requested_names,
        )
        self.assertEqual(result["missing"], requested_names)

    def test_partial_preview_renderer_only_visits_requested_file_names(self):
        session_id = service.start_session()["session_id"]
        requested = service.SLOT_DEFINITIONS[3][0]

        with patch.object(
            service,
            "_render_cached_slot_preview",
            return_value=None,
        ) as renderer:
            result = service._render_previews(
                session_id,
                [],
                {},
                "vip",
                "800",
                [requested],
            )

        renderer.assert_called_once()
        self.assertEqual(renderer.call_args.args[1], requested)
        self.assertEqual(result, {"previews": {}, "missing": [requested]})


if __name__ == "__main__":
    unittest.main()
