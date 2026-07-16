from typing import Any

from ..database import db_session, now_iso


IMAGE_API_TYPE = "image_generation"
TEXT_API_TYPE = "text_analysis"
VALID_API_TYPES = {IMAGE_API_TYPE, TEXT_API_TYPE}


API_CONFIG_FIELDS = [
    "config_name",
    "api_type",
    "api_base_url",
    "api_key",
    "model_name",
    "endpoint_path",
    "method",
    "request_content_type",
    "auth_type",
    "auth_header_name",
    "auth_header_prefix",
    "image_field_name",
    "prompt_field_name",
    "model_field_name",
    "count_field_name",
    "size_field_name",
    "quality_field_name",
    "extra_params_json",
    "response_image_type",
    "response_image_path",
    "response_text_path",
    "timeout_seconds",
    "enabled",
    "is_default",
]


def mask_api_key(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return value[:1] + "****" + value[-1:]
    return value[:4] + "****" + value[-4:]


def row_to_config(row, include_secret: bool = False) -> dict[str, Any]:
    data = {key: row[key] for key in API_CONFIG_FIELDS if key in row.keys()}
    data["id"] = row["id"]
    data["enabled"] = bool(row["enabled"])
    data["is_default"] = bool(row["is_default"])
    data["api_key_masked"] = mask_api_key(row["api_key"])
    if not include_secret:
        data.pop("api_key", None)
    data["created_at"] = row["created_at"]
    data["updated_at"] = row["updated_at"]
    return data


def get_config(config_id: int, include_secret: bool = True) -> dict[str, Any] | None:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM api_configs WHERE id = ?", (config_id,)).fetchone()
        return row_to_config(row, include_secret=include_secret) if row else None


def set_default_config(config_id: int) -> None:
    with db_session() as conn:
        row = conn.execute("SELECT id, api_type FROM api_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            raise ValueError("API 配置不存在")
        conn.execute("UPDATE api_configs SET is_default = 0 WHERE api_type = ?", (row["api_type"],))
        conn.execute("UPDATE api_configs SET is_default = 1, updated_at = ? WHERE id = ?", (now_iso(), config_id))


def get_default_config(api_type: str, include_secret: bool = True) -> dict[str, Any] | None:
    if api_type not in VALID_API_TYPES:
        raise ValueError("不支持的 API 用途")
    with db_session() as conn:
        row = conn.execute(
            """
            SELECT * FROM api_configs
            WHERE api_type = ? AND enabled = 1
            ORDER BY is_default DESC, id ASC
            LIMIT 1
            """,
            (api_type,),
        ).fetchone()
        return row_to_config(row, include_secret=include_secret) if row else None


def require_config_type(config: dict[str, Any], expected_type: str) -> None:
    actual_type = config.get("api_type") or IMAGE_API_TYPE
    if actual_type != expected_type:
        expected_label = "生图" if expected_type == IMAGE_API_TYPE else "文本分析"
        actual_label = "生图" if actual_type == IMAGE_API_TYPE else "文本分析"
        raise ValueError(f"当前选择的是{actual_label} API，不能用于{expected_label}")
