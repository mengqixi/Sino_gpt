import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import (
    DATA_DIR,
    DB_PATH,
    PRODUCT_IMAGE_INPUT_DIR,
    PRODUCT_IMAGE_OUTPUT_DIR,
    RESULT_DIR,
    UPLOAD_DIR,
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
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
    PRODUCT_IMAGE_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    PRODUCT_IMAGE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with db_session() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_name TEXT NOT NULL,
                api_type TEXT DEFAULT 'image_generation',
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
                response_text_path TEXT DEFAULT 'choices.0.message.content',
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
                created_at DATETIME,
                updated_at DATETIME
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

            CREATE TABLE IF NOT EXISTS product_image_tasks (
                id TEXT PRIMARY KEY,
                product_code TEXT NOT NULL,
                color TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'draft',
                analysis_config_id INTEGER,
                image_config_id INTEGER,
                analysis_used INTEGER NOT NULL DEFAULT 0,
                analysis_status TEXT,
                missing_roles_json TEXT NOT NULL DEFAULT '["front","back","semi_side","top","logo"]',
                selected_assets_json TEXT NOT NULL DEFAULT '{}',
                analysis_notes_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT,
                generation_active INTEGER NOT NULL DEFAULT 0,
                inputs_deleted INTEGER NOT NULL DEFAULT 0,
                inputs_deleted_at DATETIME,
                last_activity_at DATETIME NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );

            CREATE TABLE IF NOT EXISTS product_image_assets (
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
                created_at DATETIME NOT NULL,
                FOREIGN KEY(task_id) REFERENCES product_image_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(parent_asset_id) REFERENCES product_image_assets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS product_image_outputs (
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
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE(task_id, slot, variant),
                FOREIGN KEY(task_id) REFERENCES product_image_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(source_asset_id) REFERENCES product_image_assets(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS product_image_references (
                task_id TEXT NOT NULL,
                role TEXT NOT NULL,
                selected_asset_id INTEGER,
                status TEXT NOT NULL DEFAULT 'missing',
                selection_source TEXT,
                confidence INTEGER,
                reason TEXT,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY(task_id, role),
                FOREIGN KEY(task_id) REFERENCES product_image_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(selected_asset_id) REFERENCES product_image_assets(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS product_image_calls (
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
                started_at DATETIME,
                unknown_at DATETIME,
                finished_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE(task_id, call_type, slot, attempt_no),
                FOREIGN KEY(task_id) REFERENCES product_image_tasks(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_product_image_tasks_updated_at
            ON product_image_tasks(updated_at);
            CREATE INDEX IF NOT EXISTS idx_product_image_assets_task
            ON product_image_assets(task_id);
            CREATE INDEX IF NOT EXISTS idx_product_image_outputs_task
            ON product_image_outputs(task_id);
            CREATE INDEX IF NOT EXISTS idx_product_image_calls_task
            ON product_image_calls(task_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_product_image_one_analysis_call
            ON product_image_calls(task_id) WHERE call_type = 'analysis';
            """
        )
        product_task_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(product_image_tasks)").fetchall()
        }
        product_task_migrations = {
            "version": "INTEGER NOT NULL DEFAULT 1",
            "generation_active": "INTEGER NOT NULL DEFAULT 0",
            "inputs_deleted_at": "DATETIME",
            "last_activity_at": "DATETIME",
        }
        for column_name, column_sql in product_task_migrations.items():
            if column_name not in product_task_columns:
                conn.execute(f"ALTER TABLE product_image_tasks ADD COLUMN {column_name} {column_sql}")
        conn.execute(
            "UPDATE product_image_tasks SET last_activity_at = updated_at WHERE last_activity_at IS NULL"
        )
        api_config_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(api_configs)").fetchall()
        }
        organizer_session_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(vip_organizer_sessions)").fetchall()
        }
        if "updated_at" not in organizer_session_columns:
            conn.execute("ALTER TABLE vip_organizer_sessions ADD COLUMN updated_at DATETIME")
        conn.execute(
            "UPDATE vip_organizer_sessions SET updated_at = created_at WHERE updated_at IS NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vip_organizer_sessions_updated_at "
            "ON vip_organizer_sessions(updated_at)"
        )
        if "api_type" not in api_config_columns:
            conn.execute("ALTER TABLE api_configs ADD COLUMN api_type TEXT DEFAULT 'image_generation'")
        if "response_text_path" not in api_config_columns:
            conn.execute(
                "ALTER TABLE api_configs ADD COLUMN response_text_path TEXT DEFAULT 'choices.0.message.content'"
            )
        conn.execute(
            """
            UPDATE api_configs
            SET api_type = 'image_generation'
            WHERE api_type IS NULL OR api_type NOT IN ('image_generation', 'text_analysis')
            """
        )
        conn.execute(
            """
            UPDATE api_configs
            SET config_name = '素材分析（图文）'
            WHERE api_type = 'text_analysis' AND config_name = '素材分析（文本）'
            """
        )

        legacy_analysis = conn.execute(
            "SELECT api_base_url, api_key, model_name FROM vip_analysis_config WHERE id = 1"
        ).fetchone()
        if legacy_analysis:
            existing_analysis = conn.execute(
                """
                SELECT id FROM api_configs
                WHERE api_type = 'text_analysis' AND api_base_url = ? AND model_name = ?
                LIMIT 1
                """,
                (legacy_analysis["api_base_url"], legacy_analysis["model_name"]),
            ).fetchone()
            if not existing_analysis:
                ts = now_iso()
                text_config_count = conn.execute(
                    "SELECT COUNT(*) FROM api_configs WHERE api_type = 'text_analysis'"
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO api_configs (
                        config_name, api_type, api_base_url, api_key, model_name,
                        endpoint_path, method, request_content_type, auth_type,
                        auth_header_name, auth_header_prefix, extra_params_json,
                        response_text_path, timeout_seconds, enabled, is_default,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "素材分析（图文）",
                        "text_analysis",
                        legacy_analysis["api_base_url"],
                        legacy_analysis["api_key"],
                        legacy_analysis["model_name"],
                        "/chat/completions",
                        "POST",
                        "application/json",
                        "bearer",
                        "Authorization",
                        "Bearer",
                        "{}",
                        "choices.0.message.content",
                        350,
                        1,
                        1 if text_config_count == 0 else 0,
                        ts,
                        ts,
                    ),
                )
        image_config_count = conn.execute(
            "SELECT COUNT(*) FROM api_configs WHERE api_type = 'image_generation'"
        ).fetchone()[0]
        if image_config_count == 0:
            ts = now_iso()
            conn.execute(
                """
                INSERT INTO api_configs (
                    config_name, api_type, api_base_url, api_key, model_name, endpoint_path,
                    method, request_content_type, auth_type, auth_header_name,
                    auth_header_prefix, image_field_name, prompt_field_name,
                    model_field_name, count_field_name, size_field_name,
                    quality_field_name, extra_params_json, response_image_type,
                    response_image_path, response_text_path, timeout_seconds, enabled, is_default,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "默认中转站配置",
                    "image_generation",
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
                    "choices.0.message.content",
                    350,
                    1,
                    1,
                    ts,
                    ts,
                ),
            )
        fast = conn.execute(
            """
            SELECT id FROM api_configs
            WHERE config_name = '快速' AND api_type = 'image_generation' AND enabled = 1
            ORDER BY id LIMIT 1
            """
        ).fetchone()
        if fast:
            conn.execute("UPDATE api_configs SET is_default = 0 WHERE api_type = 'image_generation'")
            conn.execute(
                "UPDATE api_configs SET is_default = 1, updated_at = ? WHERE id = ?",
                (now_iso(), fast["id"]),
            )
