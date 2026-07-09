import json
from pathlib import Path
from typing import Any

from ..database import db_session, now_iso
from .file_service import public_url_for


def create_job(payload: dict[str, Any], upload_row, config: dict[str, Any]) -> int:
    ts = now_iso()
    preview = {
        "api_config_id": payload["api_config_id"],
        "request_content_type": config.get("request_content_type"),
        "endpoint_path": config.get("endpoint_path"),
        "model": config.get("model_name"),
        "field_names": {
            "image": config.get("image_field_name"),
            "prompt": config.get("prompt_field_name"),
            "model": config.get("model_field_name"),
            "count": config.get("count_field_name"),
            "size": config.get("size_field_name"),
            "quality": config.get("quality_field_name"),
        },
    }
    with db_session() as conn:
        cursor = conn.execute(
            """
            INSERT INTO image_jobs (
                task_type, status, original_image_path, original_image_name,
                prompt_template_id, final_prompt, params_json, api_config_id,
                model_name, endpoint_path, output_count, image_size, quality,
                request_payload_preview, created_at, updated_at
            ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["task_type"],
                upload_row["file_path"],
                upload_row["file_name"],
                payload.get("prompt_template_id"),
                payload["final_prompt"],
                json.dumps(payload.get("params") or {}, ensure_ascii=False),
                payload["api_config_id"],
                config.get("model_name"),
                config.get("endpoint_path"),
                payload.get("output_count"),
                payload.get("image_size"),
                payload.get("quality"),
                json.dumps(preview, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        return int(cursor.lastrowid)


def set_job_status(job_id: int, status: str, error_message: str | None = None, response_json: Any = None) -> None:
    with db_session() as conn:
        conn.execute(
            """
            UPDATE image_jobs
            SET status = ?, error_message = ?, response_raw_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                error_message,
                json.dumps(response_json, ensure_ascii=False) if response_json is not None else None,
                now_iso(),
                job_id,
            ),
        )


def add_generated_image(job_id: int, image_path: str, source_type: str) -> None:
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO generated_images (job_id, image_path, image_url, source_type, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, image_path, public_url_for(image_path), source_type, now_iso()),
        )


def create_local_recolor_job(upload_row, target_color: str, result_path: str) -> int:
    ts = now_iso()
    with db_session() as conn:
        cursor = conn.execute(
            """
            INSERT INTO image_jobs (
                task_type, status, original_image_path, original_image_name,
                final_prompt, params_json, model_name, endpoint_path, output_count,
                image_size, quality, request_payload_preview, created_at, updated_at
            ) VALUES ('local_recolor', 'success', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                upload_row["file_path"],
                upload_row["file_name"],
                f"本地智能调色：{target_color}",
                json.dumps({"target_color": target_color}, ensure_ascii=False),
                "local-opencv",
                "/api/recolor/apply",
                1,
                "",
                "",
                json.dumps({"source": "local_recolor", "result_path": result_path}, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        job_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO generated_images (job_id, image_path, image_url, source_type, created_at)
            VALUES (?, ?, ?, 'local_recolor', ?)
            """,
            (job_id, result_path, public_url_for(result_path), ts),
        )
        return job_id


def get_upload(uploaded_image_id: int):
    with db_session() as conn:
        return conn.execute("SELECT * FROM uploaded_images WHERE id = ?", (uploaded_image_id,)).fetchone()


def get_generated_image(generated_image_id: int):
    with db_session() as conn:
        return conn.execute("SELECT * FROM generated_images WHERE id = ?", (generated_image_id,)).fetchone()


def get_job(job_id: int) -> dict[str, Any] | None:
    with db_session() as conn:
        job = conn.execute("SELECT * FROM image_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return None
        images = conn.execute(
            "SELECT * FROM generated_images WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        config = None
        if job["api_config_id"]:
            config = conn.execute("SELECT config_name FROM api_configs WHERE id = ?", (job["api_config_id"],)).fetchone()
        return {
            "job_id": job["id"],
            "status": job["status"],
            "task_type": job["task_type"],
            "original_image_url": public_url_for(job["original_image_path"]) if job["original_image_path"] else None,
            "original_image_name": job["original_image_name"],
            "final_prompt": job["final_prompt"],
            "api_config_name": config["config_name"] if config else "",
            "model_name": job["model_name"],
            "endpoint_path": job["endpoint_path"],
            "output_count": job["output_count"],
            "image_size": job["image_size"],
            "quality": job["quality"],
            "error_message": job["error_message"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "results": [
                {
                    "id": image["id"],
                    "image_url": image["image_url"],
                    "source_type": image["source_type"],
                    "created_at": image["created_at"],
                }
                for image in images
            ],
        }


def list_history(filters: dict[str, str | None]) -> list[dict[str, Any]]:
    clauses = []
    values: list[Any] = []
    if filters.get("task_type"):
        clauses.append("j.task_type = ?")
        values.append(filters["task_type"])
    if filters.get("status"):
        clauses.append("j.status = ?")
        values.append(filters["status"])
    if filters.get("date_from"):
        clauses.append("j.created_at >= ?")
        values.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("j.created_at <= ?")
        values.append(filters["date_to"])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db_session() as conn:
        rows = conn.execute(
            f"""
            SELECT j.*, c.config_name
            FROM image_jobs j
            LEFT JOIN api_configs c ON c.id = j.api_config_id
            {where}
            ORDER BY j.id DESC
            LIMIT 200
            """,
            values,
        ).fetchall()
        result = []
        for row in rows:
            images = conn.execute(
                "SELECT image_url, source_type FROM generated_images WHERE job_id = ? ORDER BY id",
                (row["id"],),
            ).fetchall()
            result.append(
                {
                    "job_id": row["id"],
                    "task_type": row["task_type"],
                    "status": row["status"],
                    "original_image_url": public_url_for(row["original_image_path"]) if row["original_image_path"] else None,
                    "final_prompt": row["final_prompt"],
                    "api_config_name": row["config_name"] or "",
                    "model_name": row["model_name"],
                    "output_count": row["output_count"],
                    "image_size": row["image_size"],
                    "quality": row["quality"],
                    "error_message": row["error_message"],
                    "created_at": row["created_at"],
                    "results": [dict(image) for image in images],
                }
            )
        return result


def delete_job(job_id: int) -> bool:
    with db_session() as conn:
        images = conn.execute("SELECT image_path FROM generated_images WHERE job_id = ?", (job_id,)).fetchall()
        job = conn.execute("SELECT id FROM image_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return False
        conn.execute("DELETE FROM image_jobs WHERE id = ?", (job_id,))
    for image in images:
        if image["image_path"]:
            Path(image["image_path"]).unlink(missing_ok=True)
    return True
