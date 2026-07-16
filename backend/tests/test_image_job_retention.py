import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend.services.image_job_service import prune_job_history


class ImageJobRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "retention.db"
        with self._session() as conn:
            conn.executescript(
                """
                CREATE TABLE image_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    original_image_path TEXT,
                    request_payload_preview TEXT,
                    created_at TEXT
                );
                CREATE TABLE generated_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER,
                    image_path TEXT,
                    FOREIGN KEY(job_id) REFERENCES image_jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE uploaded_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT
                );
                """
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    @contextmanager
    def _session(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _add_job(self, status="success", created_at=None, upload_id=None, upload_path=None):
        created_at = created_at or datetime.now().isoformat(timespec="seconds")
        with self._session() as conn:
            if upload_id is None:
                upload_path = self.root / f"upload_{conn.execute('SELECT COUNT(*) FROM uploaded_images').fetchone()[0] + 1}.png"
                upload_path.write_bytes(b"upload")
                cursor = conn.execute("INSERT INTO uploaded_images (file_path) VALUES (?)", (str(upload_path),))
                upload_id = int(cursor.lastrowid)
            cursor = conn.execute(
                """
                INSERT INTO image_jobs (status, original_image_path, request_payload_preview, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    status,
                    str(upload_path),
                    json.dumps({"uploaded_image_ids": [upload_id]}),
                    created_at,
                ),
            )
            job_id = int(cursor.lastrowid)
            result_path = self.root / f"result_{job_id}.png"
            result_path.write_bytes(b"result")
            conn.execute(
                "INSERT INTO generated_images (job_id, image_path) VALUES (?, ?)",
                (job_id, str(result_path)),
            )
        return job_id, upload_id, Path(upload_path), result_path

    def test_keeps_latest_fifty_and_preserves_shared_upload(self):
        first_job, shared_upload_id, shared_upload_path, first_result = self._add_job()
        second_job, _, second_upload_path, second_result = self._add_job()
        for _ in range(49):
            self._add_job()
        self._add_job(upload_id=shared_upload_id, upload_path=shared_upload_path)

        with patch("backend.services.image_job_service.db_session", self._session):
            removed = prune_job_history()

        self.assertEqual(removed, 2)
        self.assertFalse(first_result.exists())
        self.assertFalse(second_result.exists())
        self.assertTrue(shared_upload_path.exists())
        self.assertFalse(second_upload_path.exists())
        with self._session() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_jobs").fetchone()[0], 50)
            self.assertIsNone(conn.execute("SELECT id FROM image_jobs WHERE id = ?", (first_job,)).fetchone())
            self.assertIsNone(conn.execute("SELECT id FROM image_jobs WHERE id = ?", (second_job,)).fetchone())

    def test_removes_failed_and_unknown_jobs_after_seven_days(self):
        old_date = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
        failed_job, _, failed_upload, failed_result = self._add_job("failed", old_date)
        unknown_job, _, unknown_upload, unknown_result = self._add_job("unknown", old_date)
        self._add_job("success", old_date)

        with patch("backend.services.image_job_service.db_session", self._session):
            removed = prune_job_history(max_jobs=100)

        self.assertEqual(removed, 2)
        self.assertFalse(failed_upload.exists())
        self.assertFalse(failed_result.exists())
        self.assertFalse(unknown_upload.exists())
        self.assertFalse(unknown_result.exists())
        with self._session() as conn:
            ids = {row["id"] for row in conn.execute("SELECT id FROM image_jobs")}
        self.assertNotIn(failed_job, ids)
        self.assertNotIn(unknown_job, ids)
        self.assertEqual(len(ids), 1)


if __name__ == "__main__":
    unittest.main()
