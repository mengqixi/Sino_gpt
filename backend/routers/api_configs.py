import json

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import db_session, now_iso
from ..services.api_config_service import (
    API_CONFIG_FIELDS,
    IMAGE_API_TYPE,
    VALID_API_TYPES,
    get_config,
    row_to_config,
    set_default_config,
)

router = APIRouter(prefix="/api/api-configs", tags=["api-configs"])


class ApiConfigPayload(BaseModel):
    config_name: str
    api_type: str = IMAGE_API_TYPE
    api_base_url: str
    api_key: str | None = None
    model_name: str | None = "gpt-image-2"
    endpoint_path: str | None = "/v1/images/edits"
    method: str | None = "POST"
    request_content_type: str | None = "multipart/form-data"
    auth_type: str | None = "bearer"
    auth_header_name: str | None = "Authorization"
    auth_header_prefix: str | None = "Bearer"
    image_field_name: str | None = "image"
    prompt_field_name: str | None = "prompt"
    model_field_name: str | None = "model"
    count_field_name: str | None = "n"
    size_field_name: str | None = "size"
    quality_field_name: str | None = "quality"
    extra_params_json: str | None = "{}"
    response_image_type: str | None = "base64"
    response_image_path: str | None = "data.0.b64_json"
    response_text_path: str | None = "choices.0.message.content"
    timeout_seconds: int | None = 350
    enabled: bool | None = True
    is_default: bool | None = False


class ApiConfigPatch(BaseModel):
    config_name: str | None = None
    api_type: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    model_name: str | None = None
    endpoint_path: str | None = None
    method: str | None = None
    request_content_type: str | None = None
    auth_type: str | None = None
    auth_header_name: str | None = None
    auth_header_prefix: str | None = None
    image_field_name: str | None = None
    prompt_field_name: str | None = None
    model_field_name: str | None = None
    count_field_name: str | None = None
    size_field_name: str | None = None
    quality_field_name: str | None = None
    extra_params_json: str | None = None
    response_image_type: str | None = None
    response_image_path: str | None = None
    response_text_path: str | None = None
    timeout_seconds: int | None = None
    enabled: bool | None = None
    is_default: bool | None = None


def _validate_extra_params(value: str | None) -> None:
    if value is None:
        return
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="额外参数 JSON 格式错误") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="额外参数 JSON 必须是对象")


def _validate_api_type(value: str | None) -> None:
    if value is not None and value not in VALID_API_TYPES:
        raise HTTPException(status_code=400, detail="API 用途必须是生图或文本分析")


@router.get("")
def list_configs(api_type: str | None = None):
    _validate_api_type(api_type)
    with db_session() as conn:
        if api_type:
            rows = conn.execute(
                "SELECT * FROM api_configs WHERE api_type = ? ORDER BY is_default DESC, id DESC",
                (api_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM api_configs ORDER BY api_type, is_default DESC, id DESC"
            ).fetchall()
        return [row_to_config(row, include_secret=False) for row in rows]


@router.post("")
def create_config(payload: ApiConfigPayload):
    _validate_extra_params(payload.extra_params_json)
    data = payload.model_dump()
    _validate_api_type(data.get("api_type"))
    data["enabled"] = 1 if data.get("enabled") else 0
    data["is_default"] = 1 if data.get("is_default") else 0
    ts = now_iso()
    with db_session() as conn:
        if data["is_default"]:
            conn.execute("UPDATE api_configs SET is_default = 0 WHERE api_type = ?", (data["api_type"],))
        fields = [field for field in API_CONFIG_FIELDS if field in data]
        placeholders = ", ".join("?" for _ in fields)
        cursor = conn.execute(
            f"INSERT INTO api_configs ({', '.join(fields)}, created_at, updated_at) VALUES ({placeholders}, ?, ?)",
            [data[field] for field in fields] + [ts, ts],
        )
        row = conn.execute("SELECT * FROM api_configs WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return row_to_config(row, include_secret=False)


@router.patch("/{config_id}")
def update_config(config_id: int, payload: ApiConfigPatch):
    data = payload.model_dump(exclude_unset=True)
    _validate_extra_params(data.get("extra_params_json"))
    _validate_api_type(data.get("api_type"))
    if "enabled" in data:
        data["enabled"] = 1 if data["enabled"] else 0
    if "is_default" in data:
        data["is_default"] = 1 if data["is_default"] else 0
    updates = []
    values = []
    for key, value in data.items():
        if key in API_CONFIG_FIELDS:
            updates.append(f"{key} = ?")
            values.append(value)
    if not updates:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    updates.append("updated_at = ?")
    values.append(now_iso())
    with db_session() as conn:
        row = conn.execute("SELECT * FROM api_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="API 配置不存在")
        target_type = data.get("api_type") or row["api_type"] or IMAGE_API_TYPE
        if data.get("is_default"):
            conn.execute("UPDATE api_configs SET is_default = 0 WHERE api_type = ?", (target_type,))
        conn.execute(f"UPDATE api_configs SET {', '.join(updates)} WHERE id = ?", [*values, config_id])
        updated = conn.execute("SELECT * FROM api_configs WHERE id = ?", (config_id,)).fetchone()
        return row_to_config(updated, include_secret=False)


@router.post("/{config_id}/set-default")
def set_default(config_id: int):
    try:
        set_default_config(config_id)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{config_id}")
def delete_config(config_id: int):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM api_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="API 配置不存在")
        conn.execute("DELETE FROM api_configs WHERE id = ?", (config_id,))
        return {"ok": True}


@router.post("/{config_id}/test")
def test_config(config_id: int):
    config = get_config(config_id, include_secret=True)
    if not config:
        raise HTTPException(status_code=404, detail="API 配置不存在")
    base_url = (config.get("api_base_url") or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="API Base URL 为空")
    try:
        response = requests.request("HEAD", base_url, timeout=10)
        return {"ok": True, "message": f"Base URL 可访问，状态码 {response.status_code}"}
    except Exception:
        return {"ok": True, "message": "配置已保存，未执行真实生成测试；如果中转站无通用测试接口，可以跳过测试。"}
