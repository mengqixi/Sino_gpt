import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import DATA_DIR, DB_PATH, RESULT_DIR, UPLOAD_DIR


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    with db_session() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_name TEXT NOT NULL,
                api_base_url TEXT NOT NULL,
                api_key TEXT,
                model_name TEXT,
                endpoint_path TEXT,
                method TEXT DEFAULT 'POST',
                request_content_type TEXT DEFAULT 'multipart/form-data',
                auth_type TEXT DEFAULT 'bearer',
                auth_header_name TEXT DEFAULT 'Authorization',
                auth_header_prefix TEXT DEFAULT 'Bearer',
                image_field_name TEXT DEFAULT 'image',
                prompt_field_name TEXT DEFAULT 'prompt',
                model_field_name TEXT DEFAULT 'model',
                count_field_name TEXT DEFAULT 'n',
                size_field_name TEXT DEFAULT 'size',
                quality_field_name TEXT DEFAULT 'quality',
                extra_params_json TEXT DEFAULT '{}',
                response_image_type TEXT DEFAULT 'base64',
                response_image_path TEXT DEFAULT 'data.0.b64_json',
                timeout_seconds INTEGER DEFAULT 350,
                enabled INTEGER DEFAULT 1,
                is_default INTEGER DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME
            );

            CREATE TABLE IF NOT EXISTS prompt_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                task_type TEXT NOT NULL,
                template_content TEXT NOT NULL,
                variables_json TEXT,
                is_default INTEGER DEFAULT 0,
                is_system INTEGER DEFAULT 0,
                original_file_name TEXT,
                created_at DATETIME,
                updated_at DATETIME
            );

            CREATE TABLE IF NOT EXISTS image_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL,
                original_image_path TEXT,
                original_image_name TEXT,
                prompt_template_id INTEGER,
                final_prompt TEXT,
                params_json TEXT,
                api_config_id INTEGER,
                model_name TEXT,
                endpoint_path TEXT,
                output_count INTEGER,
                image_size TEXT,
                quality TEXT,
                request_payload_preview TEXT,
                response_raw_json TEXT,
                error_message TEXT,
                created_at DATETIME,
                updated_at DATETIME
            );

            CREATE TABLE IF NOT EXISTS generated_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                image_path TEXT,
                image_url TEXT,
                source_type TEXT,
                created_at DATETIME,
                FOREIGN KEY(job_id) REFERENCES image_jobs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS uploaded_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT,
                file_path TEXT,
                file_size INTEGER,
                mime_type TEXT,
                width INTEGER,
                height INTEGER,
                created_at DATETIME
            );

            CREATE TABLE IF NOT EXISTS vip_organizer_sessions (
                id TEXT PRIMARY KEY,
                created_at DATETIME
            );

            CREATE TABLE IF NOT EXISTS vip_organizer_assets (
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

            CREATE TABLE IF NOT EXISTS vip_analysis_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                api_base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                model_name TEXT NOT NULL,
                updated_at DATETIME
            );
            """
        )
        count = conn.execute("SELECT COUNT(*) FROM api_configs").fetchone()[0]
        if count == 0:
            ts = now_iso()
            conn.execute(
                """
                INSERT INTO api_configs (
                    config_name, api_base_url, api_key, model_name, endpoint_path,
                    method, request_content_type, auth_type, auth_header_name,
                    auth_header_prefix, image_field_name, prompt_field_name,
                    model_field_name, count_field_name, size_field_name,
                    quality_field_name, extra_params_json, response_image_type,
                    response_image_path, timeout_seconds, enabled, is_default,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "默认中转站配置",
                    "https://your-relay-domain.com",
                    "",
                    "gpt-image-2",
                    "/v1/images/edits",
                    "POST",
                    "multipart/form-data",
                    "bearer",
                    "Authorization",
                    "Bearer",
                    "image",
                    "prompt",
                    "model",
                    "n",
                    "size",
                    "quality",
                    "{}",
                    "base64",
                    "data.0.b64_json",
                    350,
                    1,
                    1,
                    ts,
                    ts,
                ),
            )
        fast = conn.execute(
            "SELECT id FROM api_configs WHERE config_name = '快速' AND enabled = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        if fast:
            conn.execute("UPDATE api_configs SET is_default = 0")
            conn.execute(
                "UPDATE api_configs SET is_default = 1, updated_at = ? WHERE id = ?",
                (now_iso(), fast["id"]),
            )
