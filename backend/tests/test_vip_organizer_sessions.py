import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import database
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


if __name__ == "__main__":
    unittest.main()
