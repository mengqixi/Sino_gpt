from __future__ import annotations

import json
import re
from threading import Event, Semaphore, Thread
from typing import Any

import requests

from ..database import db_session, now_iso
from .api_config_service import (
    IMAGE_API_TYPE,
    TEXT_API_TYPE,
    get_config,
    require_config_type,
)
from .json_path_service import json_path_get
from .product_image_service import (
    API_OUTPUT_SLOTS,
    REFERENCE_ROLES,
    ProductImageConflict,
    ProductImageError,
    analysis_candidates,
    analysis_image_data_url,
    apply_analysis_result,
    ensure_local_outputs,
    get_task,
    reference_rows,
    save_generated_response,
    set_output_status,
)
from .relay_image_service import RelayGatewayTimeoutError, call_relay_image_api


PRODUCT_IMAGE_API_SEMAPHORE = Semaphore(1)
MAX_ANALYSIS_RESPONSE_BYTES = 4 * 1024 * 1024


ROLE_ALIASES = {
    "front": "front",
    "back": "back",
    "semi_side": "semi_side",
    "three_quarter": "semi_side",
    "top": "top",
    "top_open": "top",
    "logo": "logo",
    "logo_detail": "logo",
}

ANALYSIS_PROMPT = """你是通用包袋商品素材审核员。不得假设品牌，不得凭空补全没拍到的结构。
请逐张判断是否清晰、分辨率是否足够、包体或关键结构是否被手或物体严重遮挡，并且每张最多分配一个角色：
- front：完整正面；
- back：完整背面；
- semi_side：能同时看到正面与一侧厚度的半侧面/三分之二角度；
- top：顶部开口全景，能真实看到开口和内部结构；
- logo：真实 Logo 与周围材质的清晰近照；
- irrelevant：不符合以上任一角色。
同一个素材不得承担多个角色。只有画面真实展示对应角度且足够清晰时 valid 才能为 true；不能根据别的角度猜测。
必须按输入 index 返回全部素材，只返回 JSON，不要 Markdown：
{"items":[{"index":1,"role":"front","valid":true,"confidence":90,"reason":"具体中文理由"}],"notes":"整批简短说明"}
"""


GENERATION_DIRECTIONS = {
    "front_main": "保持真实正面视角，生成完整居中的正面商品主图",
    "back": "保持真实背面视角，生成完整居中的背面商品图",
    "semi_side": "保持真实半侧面或三分之二视角，生成完整居中的半侧面商品图",
    "top": "保持真实顶部开口视角，清晰展示真实开口与已拍到的内部结构",
}


def _response_preview(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _response_preview(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_response_preview(item) for item in value]
    if isinstance(value, str) and len(value) > 2000:
        kind = "data_url" if value.startswith("data:image/") else "long_text"
        return f"[{kind} omitted, {len(value)} chars]"
    return value


def _enabled_config(config_id: int, expected_type: str) -> dict[str, Any]:
    config = get_config(config_id, include_secret=True)
    if not config:
        raise ProductImageError("API 配置不存在")
    if not config.get("enabled"):
        raise ProductImageError("所选 API 配置未启用")
    try:
        require_config_type(config, expected_type)
    except ValueError as exc:
        raise ProductImageError(str(exc)) from exc
    return config


def _next_attempt(conn: Any, task_id: str, call_type: str, slot: str | None) -> int:
    if slot is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM product_image_calls WHERE task_id = ? AND call_type = ? AND slot IS NULL",
            (task_id, call_type),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM product_image_calls WHERE task_id = ? AND call_type = ? AND slot = ?",
            (task_id, call_type, slot),
        ).fetchone()
    return int(row[0])


def _create_call(conn: Any, task_id: str, call_type: str, slot: str | None, config: dict[str, Any], prompt: str) -> int:
    ts = now_iso()
    attempt_no = _next_attempt(conn, task_id, call_type, slot)
    cursor = conn.execute(
        """
        INSERT INTO product_image_calls (
            task_id, call_type, slot, attempt_no, status, api_config_id,
            config_name, model_name, endpoint_path, prompt, started_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            call_type,
            slot,
            attempt_no,
            config["id"],
            config.get("config_name"),
            config.get("model_name"),
            config.get("endpoint_path"),
            prompt,
            ts,
            ts,
            ts,
        ),
    )
    return int(cursor.lastrowid)


def _finish_call(call_id: int, status: str, error: str | None = None, response: Any = None, *, preserve_unknown_at: bool = False) -> None:
    with db_session() as conn:
        row = conn.execute("SELECT unknown_at FROM product_image_calls WHERE id = ?", (call_id,)).fetchone()
        if not row:
            return
        ts = now_iso()
        conn.execute(
            """
            UPDATE product_image_calls
            SET status = ?, response_preview_json = ?, error_message = ?,
                unknown_at = CASE
                    WHEN ? = 'unknown' THEN COALESCE(unknown_at, ?)
                    WHEN ? THEN unknown_at
                    ELSE NULL
                END,
                finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                json.dumps(_response_preview(response), ensure_ascii=False) if response is not None else None,
                error,
                status,
                ts,
                1 if preserve_unknown_at else 0,
                ts,
                ts,
                call_id,
            ),
        )


def _mark_call_unknown(call_id: int, task_id: str, slot: str | None, message: str, call_type: str) -> bool:
    with db_session() as conn:
        ts = now_iso()
        updated = conn.execute(
            """
            UPDATE product_image_calls
            SET status = 'unknown', unknown_at = ?, error_message = ?, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (ts, message, ts, call_id),
        )
        if updated.rowcount != 1:
            return False
        if call_type == "analysis":
            conn.execute(
                """
                UPDATE product_image_tasks
                SET status = 'analysis_unknown', analysis_status = 'unknown', error_message = ?,
                    last_activity_at = ?, updated_at = ? WHERE id = ?
                """,
                (message, ts, ts, task_id),
            )
        else:
            conn.execute(
                """
                UPDATE product_image_tasks
                SET status = 'paused_unknown', error_message = ?, last_activity_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (message, ts, ts, task_id),
            )
    if slot:
        set_output_status(task_id, slot, "unknown", message)
    return True


def prepare_analysis(task_id: str, api_config_id: int) -> int:
    config = _enabled_config(api_config_id, TEXT_API_TYPE)
    candidates = analysis_candidates(task_id)
    if not candidates:
        raise ProductImageError("请先上传商品照片或视频")
    with db_session() as conn:
        task = conn.execute("SELECT * FROM product_image_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise ProductImageError("商品图任务不存在")
        if task["inputs_deleted"]:
            raise ProductImageConflict("本轮原始素材已删除，请开始下一轮")
        if task["analysis_used"]:
            raise ProductImageConflict("每个任务只能调用一次图文分析 API；补图后请手工选择参考素材")
        running = conn.execute(
            "SELECT id FROM product_image_calls WHERE task_id = ? AND status = 'running' LIMIT 1",
            (task_id,),
        ).fetchone()
        if running:
            raise ProductImageConflict("任务已有 API 请求正在处理")
        call_id = _create_call(conn, task_id, "analysis", None, config, ANALYSIS_PROMPT)
        ts = now_iso()
        conn.execute(
            """
            UPDATE product_image_tasks
            SET analysis_used = 1, analysis_status = 'running', analysis_config_id = ?,
                status = 'analyzing', error_message = NULL, last_activity_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (api_config_id, ts, ts, task_id),
        )
    return call_id


def _analysis_headers(config: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    auth_type = str(config.get("auth_type") or "bearer").lower()
    if auth_type == "none":
        return headers
    key = str(config.get("api_key") or "")
    if not key:
        raise ProductImageError("所选图文分析 API 尚未配置 API Key")
    name = str(config.get("auth_header_name") or "Authorization")
    if auth_type == "bearer":
        prefix = str(config.get("auth_header_prefix") or "Bearer")
        headers[name] = f"{prefix} {key}".strip()
    elif auth_type == "raw":
        headers[name] = key
    else:
        raise ProductImageError("图文分析 API 的认证方式无效")
    return headers


def _call_analysis_api(config: dict[str, Any], candidates: list[dict[str, Any]], timeout: int) -> dict[str, Any]:
    base_url = str(config.get("api_base_url") or "").strip()
    endpoint = str(config.get("endpoint_path") or "").strip()
    if not base_url or not endpoint:
        raise ProductImageError("图文分析 API 的 Base URL 或接口路径为空")
    if str(config.get("method") or "POST").upper() != "POST":
        raise ProductImageError("图文分析 API 当前仅支持 POST")
    if str(config.get("request_content_type") or "application/json").lower() != "application/json":
        raise ProductImageError("图文分析 API 必须使用 application/json")
    try:
        payload = json.loads(config.get("extra_params_json") or "{}")
    except json.JSONDecodeError as exc:
        raise ProductImageError("图文分析 API 的额外参数 JSON 格式错误") from exc
    if not isinstance(payload, dict):
        raise ProductImageError("图文分析 API 的额外参数必须是 JSON 对象")
    content: list[dict[str, Any]] = [{"type": "text", "text": ANALYSIS_PROMPT}]
    for index, item in enumerate(candidates, start=1):
        declared = item.get("slot") if item.get("media_type") == "image" else "视频抽帧（未指定角度）"
        content.append(
            {
                "type": "text",
                "text": (
                    f"素材 index={index}，asset_id={item['id']}，文件名={item.get('file_name') or ''}，"
                    f"上传入口={declared}。入口只是提示，必须以真实画面为准。"
                ),
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": analysis_image_data_url(item["file_path"]), "detail": "low"},
            }
        )
    payload.update(
        {
            config.get("model_field_name") or "model": config.get("model_name") or "",
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
        }
    )
    response = requests.post(
        base_url.rstrip("/") + "/" + endpoint.lstrip("/"),
        headers=_analysis_headers(config),
        json=payload,
        timeout=timeout,
        stream=True,
    )
    try:
        if response.status_code == 524:
            raise RelayGatewayTimeoutError("图文分析网关超时，结果无法确认且不会自动重试")
        try:
            declared_length = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared_length = 0
        if declared_length > MAX_ANALYSIS_RESPONSE_BYTES:
            raise ProductImageError("图文分析 API 返回内容超过 4MB 安全限制")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_ANALYSIS_RESPONSE_BYTES:
                raise ProductImageError("图文分析 API 返回内容超过 4MB 安全限制")
        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            compact = re.sub(r"\s+", " ", text).strip()[:400]
            raise ProductImageError(f"图文分析 API 返回 HTTP {response.status_code}：{compact}") from exc
        try:
            parsed = json.loads(text.lstrip("\ufeff"))
        except ValueError as exc:
            raise ProductImageError("图文分析 API 返回的不是 JSON") from exc
        if not isinstance(parsed, dict):
            raise ProductImageError("图文分析 API 返回的 JSON 结构不正确")
        return parsed
    finally:
        response.close()


def _parse_analysis_response(response: dict[str, Any], config: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        values = json_path_get(response, config.get("response_text_path") or "choices.0.message.content")
        raw = values[0]
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        match = re.search(r"\{.*\}", raw, flags=re.S)
        parsed = json.loads(match.group(0) if match else raw)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise ProductImageError("图文分析 API 未返回可读取的固定 JSON") from exc
    by_index = {index: int(item["id"]) for index, item in enumerate(candidates, start=1)}
    items: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()
    for value in parsed.get("items") or []:
        try:
            index = int(value.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if index not in by_index or index in seen_indexes:
            continue
        seen_indexes.add(index)
        role = ROLE_ALIASES.get(str(value.get("role") or "").lower(), "")
        valid = bool(value.get("valid", True)) and role in REFERENCE_ROLES
        items.append(
            {
                "asset_id": by_index[index],
                "role": role if role else "front",
                "valid": valid,
                "confidence": max(0, min(100, int(value.get("confidence") or 0))),
                "reason": str(value.get("reason") or "")[:300],
            }
        )
    if not items:
        raise ProductImageError("图文分析 API 没有返回任何有效素材判断")
    notes = {"summary": str(parsed.get("notes") or "")[:500], "candidate_count": len(candidates)}
    return items, notes


def run_analysis(task_id: str, call_id: int) -> None:
    try:
        with db_session() as conn:
            call = conn.execute(
                "SELECT * FROM product_image_calls WHERE id = ? AND task_id = ?",
                (call_id, task_id),
            ).fetchone()
        if not call or call["status"] != "running":
            return
        config = _enabled_config(int(call["api_config_id"]), TEXT_API_TYPE)
        candidates = analysis_candidates(task_id)
        hard_timeout = min(max(int(config.get("timeout_seconds") or 350), 30), 350)
        became_unknown = Event()
        request_finished = Event()

        def watchdog() -> None:
            if request_finished.wait(hard_timeout):
                return
            message = (
                f"图文分析已等待 {hard_timeout} 秒，结果暂时未知且可能已经扣费；"
                "后台仍会等待本次响应，但不会再次调用分析 API。"
            )
            if _mark_call_unknown(call_id, task_id, None, message, "analysis"):
                became_unknown.set()

        # Waiting for local capacity is not an API call and must not start the
        # paid-request watchdog.
        with PRODUCT_IMAGE_API_SEMAPHORE:
            with db_session() as conn:
                current = conn.execute(
                    "SELECT status FROM product_image_calls WHERE id = ? AND task_id = ?",
                    (call_id, task_id),
                ).fetchone()
            if not current or current["status"] != "running":
                return
            Thread(target=watchdog, daemon=True, name=f"product-analysis-watchdog-{call_id}").start()
            try:
                response = _call_analysis_api(config, candidates, max(hard_timeout, 900))
            finally:
                request_finished.set()
        items, notes = _parse_analysis_response(response, config, candidates)
        apply_analysis_result(task_id, items, notes)
        _finish_call(call_id, "success", response=response, preserve_unknown_at=became_unknown.is_set())
    except (requests.Timeout, RelayGatewayTimeoutError) as exc:
        message = f"{exc}；系统不会自动重试本次图文分析"
        _mark_call_unknown(call_id, task_id, None, message, "analysis")
        _finish_call(call_id, "unknown", message, preserve_unknown_at=True)
    except Exception as exc:
        message = str(exc)
        with db_session() as conn:
            call = conn.execute("SELECT status FROM product_image_calls WHERE id = ?", (call_id,)).fetchone()
            was_unknown = bool(call and call["status"] == "unknown")
            ts = now_iso()
            conn.execute(
                """
                UPDATE product_image_tasks SET status = ?, analysis_status = ?, error_message = ?,
                    last_activity_at = ?, updated_at = ? WHERE id = ?
                """,
                (
                    "analysis_unknown" if was_unknown else "analysis_failed",
                    "unknown" if was_unknown else "failed",
                    message,
                    ts,
                    ts,
                    task_id,
                ),
            )
        _finish_call(call_id, "unknown" if was_unknown else "failed", message, preserve_unknown_at=was_unknown)


def _successful_api_slots(conn: Any, task_id: str) -> set[str]:
    return {
        str(row["slot"])
        for row in conn.execute(
            """
            SELECT slot FROM product_image_outputs
            WHERE task_id = ? AND variant = 'highres' AND status = 'success' AND file_path IS NOT NULL
            """,
            (task_id,),
        ).fetchall()
    }


def prepare_generation(
    task_id: str,
    api_config_id: int,
    *,
    mode: str = "initial",
    slot: str | None = None,
    acknowledge_possible_charge: bool = False,
) -> list[str]:
    config = _enabled_config(api_config_id, IMAGE_API_TYPE)
    references = reference_rows(task_id)
    missing = [role for role in REFERENCE_ROLES if not references.get(role, {}).get("selected_asset_id")]
    if missing:
        raise ProductImageConflict(f"请先补全素材：{', '.join(missing)}")
    with db_session() as conn:
        task = conn.execute("SELECT * FROM product_image_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise ProductImageError("商品图任务不存在")
        if task["inputs_deleted"]:
            raise ProductImageConflict("本轮原始素材已删除，历史任务不能重新生成")
        if not task["analysis_used"]:
            raise ProductImageConflict("请先完成一次素材分析")
        running = conn.execute(
            "SELECT id FROM product_image_calls WHERE task_id = ? AND status = 'running' LIMIT 1",
            (task_id,),
        ).fetchone()
        if task["generation_active"] or running:
            raise ProductImageConflict("任务已有 API 请求正在处理")
        successful = _successful_api_slots(conn, task_id)
        if mode == "single":
            if slot not in API_OUTPUT_SLOTS:
                raise ProductImageError("只有四张 API 商品图可以单独重新生成")
            last_slot_call = conn.execute(
                """
                SELECT status, finished_at FROM product_image_calls
                WHERE task_id = ? AND call_type = 'generation' AND slot = ?
                ORDER BY id DESC LIMIT 1
                """,
                (task_id, slot),
            ).fetchone()
            if last_slot_call and last_slot_call["status"] == "unknown":
                if not last_slot_call["finished_at"]:
                    raise ProductImageConflict("该图片上一次结果未知的请求仍在后台等待，请稍后再重试")
                if not acknowledge_possible_charge:
                    raise ProductImageConflict("该图片上一次请求结果未知且可能已经扣费；确认风险后才能重试")
            slots = [str(slot)]
        elif mode == "resume":
            unknown = conn.execute(
                """
                SELECT u.* FROM product_image_calls u
                WHERE u.task_id = ? AND u.call_type = 'generation' AND u.status = 'unknown'
                  AND NOT EXISTS (
                      SELECT 1 FROM product_image_calls later
                      WHERE later.task_id = u.task_id AND later.call_type = 'generation'
                        AND later.slot = u.slot AND later.id > u.id AND later.status = 'success'
                  )
                ORDER BY u.id DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if unknown and not unknown["finished_at"]:
                raise ProductImageConflict("上一次结果未知的请求仍在后台等待，请稍后再继续")
            if unknown and not acknowledge_possible_charge:
                raise ProductImageConflict("结果未知的请求可能已经扣费；确认风险后才能重试")
            slots = [item for item in API_OUTPUT_SLOTS if item not in successful]
        elif mode == "initial":
            if str(task["status"]).startswith("paused_"):
                raise ProductImageConflict("任务已暂停，请使用继续生成并确认可能的扣费风险")
            slots = [item for item in API_OUTPUT_SLOTS if item not in successful]
        else:
            raise ProductImageError("生成模式不正确")
        if not slots:
            raise ProductImageConflict("四张 API 商品图已经生成完成")
        ts = now_iso()
        claimed = conn.execute(
            """
            UPDATE product_image_tasks
            SET generation_active = 1, status = 'generating', image_config_id = ?,
                error_message = NULL, last_activity_at = ?, updated_at = ?
            WHERE id = ? AND generation_active = 0
              AND NOT EXISTS (
                  SELECT 1 FROM product_image_calls c
                  WHERE c.task_id = product_image_tasks.id
                    AND (
                        c.status = 'running'
                        OR (c.status = 'unknown' AND c.finished_at IS NULL)
                    )
              )
            """,
            (api_config_id, ts, ts, task_id),
        )
        if claimed.rowcount != 1:
            raise ProductImageConflict("任务已有 API 请求正在处理")
    # Keep a non-secret marker for logging/debugging; the worker reloads the key.
    _ = config.get("config_name")
    return slots


def _generation_prompt(task: dict[str, Any], slot: str) -> str:
    return "\n".join(
        [
            "你是专业商品摄影后期师，只能依据本次提供的这一张实物包袋参考图处理。",
            GENERATION_DIRECTIONS[slot] + "。",
            "使用纯白 #FFFFFF 影棚背景和很淡、真实的落地阴影；商品完整居中，不裁掉包体、肩带、链条或挂饰。",
            "允许清除拍摄环境、手、灰尘、小污点和轻微折痕，并整理肩带、链条和挂饰为标准陈列方式。",
            "必须严格保持真实包型、颜色、材质纹理、车线、Logo 拼写与位置、五金形状与颜色及全部真实配件。",
            "不得新增、删除或改造配件，不得改变结构，不得根据其他视角猜画看不见的区域，不得生成额外文字或水印。",
            f"任务标识：款号 {task['product_code']}，颜色 {task['color']}。标识只用于一致性，不要画进图片。",
            "输出一张独立的正方形高质量商品图。",
        ]
    )


def _set_generation_pause(task_id: str, slot: str, status: str, message: str) -> None:
    task_status = "paused_unknown" if status == "unknown" else "paused_failed"
    set_output_status(task_id, slot, status, message)
    with db_session() as conn:
        ts = now_iso()
        conn.execute(
            """
            UPDATE product_image_tasks
            SET generation_active = 0, status = ?, error_message = ?,
                last_activity_at = ?, updated_at = ? WHERE id = ?
            """,
            (task_status, message, ts, ts, task_id),
        )


def _run_generation_slot(task_id: str, slot: str, config: dict[str, Any], task: dict[str, Any], source: dict[str, Any]) -> str:
    prompt = _generation_prompt(task, slot)
    hard_timeout = min(max(int(config.get("timeout_seconds") or 350), 30), 350)
    became_unknown = Event()
    request_finished = Event()
    call_id: int | None = None
    response_received = False

    def watchdog() -> None:
        if request_finished.wait(hard_timeout):
            return
        message = (
            f"{slot} 已等待 {hard_timeout} 秒，结果暂时未知且可能已经扣费；"
            "后台仍会等待本次响应，后续商品图不会自动继续。"
        )
        if call_id is not None and _mark_call_unknown(call_id, task_id, slot, message, "generation"):
            became_unknown.set()

    try:
        # Do not create a billable call record or start its watchdog while this
        # task is merely waiting for the single low-memory API lane.
        with PRODUCT_IMAGE_API_SEMAPHORE:
            with db_session() as conn:
                active = conn.execute(
                    "SELECT generation_active FROM product_image_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            if not active or not active["generation_active"]:
                return "cancelled"
            set_output_status(task_id, slot, "running", None)
            with db_session() as conn:
                call_id = _create_call(conn, task_id, "generation", slot, config, prompt)
            Thread(target=watchdog, daemon=True, name=f"product-generation-watchdog-{call_id}").start()
            relay_config = {**config, "timeout_seconds": max(hard_timeout, 900)}
            try:
                response = call_relay_image_api(
                    relay_config,
                    [str(source["file_path"])],
                    prompt,
                    1,
                    "2048x2048",
                    "high",
                )
                response_received = True
            finally:
                request_finished.set()
        save_generated_response(
            task_id,
            slot,
            response,
            config,
            int(source["selected_asset_id"]),
            prompt,
        )
        with db_session() as conn:
            current = conn.execute(
                "SELECT status FROM product_image_calls WHERE id = ?",
                (call_id,),
            ).fetchone()
        late_success = became_unknown.is_set() or bool(current and current["status"] == "unknown")
        _finish_call(call_id, "success", response=response, preserve_unknown_at=late_success)
        if late_success:
            with db_session() as conn:
                ts = now_iso()
                conn.execute(
                    """
                    UPDATE product_image_tasks
                    SET generation_active = 0, status = 'paused_late_success',
                        error_message = '上一张图片在结果未知后晚到成功；请人工确认后继续其余图片',
                        last_activity_at = ?, updated_at = ? WHERE id = ?
                    """,
                    (ts, ts, task_id),
                )
            return "late_success"
        return "success"
    except RelayGatewayTimeoutError as exc:
        message = str(exc)
        request_finished.set()
        if call_id is not None:
            _mark_call_unknown(call_id, task_id, slot, message, "generation")
            _finish_call(call_id, "unknown", message, preserve_unknown_at=True)
        _set_generation_pause(task_id, slot, "unknown", message)
        return "unknown"
    except Exception as exc:
        request_finished.set()
        message = str(exc)
        was_unknown = False
        if call_id is not None:
            with db_session() as conn:
                call = conn.execute("SELECT status FROM product_image_calls WHERE id = ?", (call_id,)).fetchone()
                was_unknown = bool(call and call["status"] == "unknown")
        # Once the API has returned, a local decode/disk failure may still have
        # consumed the paid request. Treat it as unknown before allowing retry.
        final_status = "unknown" if was_unknown or response_received else "failed"
        if response_received and not was_unknown:
            message = f"API 已返回但本地保存失败，可能已经扣费：{message}"
        if call_id is not None:
            _finish_call(
                call_id,
                final_status,
                message,
                preserve_unknown_at=final_status == "unknown",
            )
        _set_generation_pause(task_id, slot, final_status, message)
        return final_status


def run_generation(task_id: str, slots: list[str], api_config_id: int) -> None:
    try:
        config = _enabled_config(api_config_id, IMAGE_API_TYPE)
        task = get_task(task_id, include_calls=False)
        references = reference_rows(task_id)
        # Local outputs must be valid before any billable generation request is sent.
        ensure_local_outputs(task_id)
        for slot in slots:
            role = "front" if slot == "front_main" else slot
            source = references.get(role)
            if not source or not source.get("file_path"):
                raise ProductImageError(f"{role} 参考素材不存在，请重新上传")
            result = _run_generation_slot(task_id, slot, config, task, source)
            if result != "success":
                return
        with db_session() as conn:
            finished = conn.execute(
                """
                SELECT COUNT(DISTINCT slot) FROM product_image_outputs
                WHERE task_id = ? AND variant = '800' AND status = 'success' AND file_path IS NOT NULL
                """,
                (task_id,),
            ).fetchone()[0]
            ts = now_iso()
            conn.execute(
                """
                UPDATE product_image_tasks
                SET generation_active = 0, status = ?, error_message = NULL,
                    last_activity_at = ?, updated_at = ? WHERE id = ?
                """,
                ("completed" if int(finished) == 6 else "ready", ts, ts, task_id),
            )
    except Exception as exc:
        message = str(exc)
        with db_session() as conn:
            ts = now_iso()
            conn.execute(
                """
                UPDATE product_image_tasks
                SET generation_active = 0, status = 'paused_failed', error_message = ?,
                    last_activity_at = ?, updated_at = ? WHERE id = ?
                """,
                (message, ts, ts, task_id),
            )


def recover_interrupted_calls() -> int:
    """Mark requests orphaned by a server restart as unknown; never retry them."""
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT id, task_id, call_type, slot FROM product_image_calls
            WHERE status = 'running' OR (status = 'unknown' AND finished_at IS NULL)
            """
        ).fetchall()
        ts = now_iso()
        for row in rows:
            message = "服务重启时该 API 请求仍在运行，结果无法确认且可能已经扣费；系统不会自动重试。"
            conn.execute(
                """
                UPDATE product_image_calls SET status = 'unknown', error_message = ?,
                    unknown_at = COALESCE(unknown_at, ?), finished_at = ?, updated_at = ? WHERE id = ?
                """,
                (message, ts, ts, ts, row["id"]),
            )
            if row["call_type"] == "analysis":
                conn.execute(
                    """
                    UPDATE product_image_tasks SET generation_active = 0, status = 'analysis_unknown',
                        analysis_status = 'unknown', error_message = ?, updated_at = ? WHERE id = ?
                    """,
                    (message, ts, row["task_id"]),
                )
            else:
                conn.execute(
                    """
                    UPDATE product_image_tasks SET generation_active = 0, status = 'paused_unknown',
                        error_message = ?, updated_at = ? WHERE id = ?
                    """,
                    (message, ts, row["task_id"]),
                )
                if row["slot"]:
                    conn.execute(
                        """
                        UPDATE product_image_outputs SET status = 'unknown', error_message = ?, updated_at = ?
                        WHERE task_id = ? AND slot = ?
                        """,
                        (message, ts, row["task_id"], row["slot"]),
                    )

        # A crash can happen after prepare_generation claims the task but
        # before BackgroundTasks creates the first call. No API request exists
        # in that window, so release the task as a non-billable failure and
        # never submit anything automatically.
        orphans = conn.execute(
            """
            SELECT t.id,
                   EXISTS (
                       SELECT 1 FROM product_image_calls u
                       WHERE u.task_id = t.id AND u.call_type = 'generation'
                         AND u.status = 'unknown'
                         AND NOT EXISTS (
                             SELECT 1 FROM product_image_calls later
                             WHERE later.task_id = u.task_id
                               AND later.call_type = 'generation'
                               AND later.slot = u.slot AND later.id > u.id
                               AND later.status = 'success'
                         )
                   ) AS has_unresolved_unknown
            FROM product_image_tasks t
            WHERE t.generation_active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM product_image_calls c
                  WHERE c.task_id = t.id
                    AND (
                        c.status = 'running'
                        OR (c.status = 'unknown' AND c.finished_at IS NULL)
                    )
              )
            """
        ).fetchall()
        safe_orphan_message = (
            "服务在生成任务排队后中断，未发现已经发出的 API 请求；"
            "任务已安全暂停，系统不会自动重试。"
        )
        for orphan in orphans:
            has_unknown = bool(orphan["has_unresolved_unknown"])
            orphan_message = (
                "服务中断前有生成请求结果未知且可能已经扣费；"
                "任务已暂停，确认风险前系统不会重试。"
                if has_unknown
                else safe_orphan_message
            )
            conn.execute(
                """
                UPDATE product_image_tasks
                SET generation_active = 0, status = 'paused_failed', error_message = ?,
                    last_activity_at = ?, updated_at = ? WHERE id = ?
                """,
                (
                    "paused_unknown" if has_unknown else "paused_failed",
                    orphan_message,
                    ts,
                    ts,
                    orphan["id"],
                ),
            )
            conn.execute(
                """
                UPDATE product_image_outputs
                SET status = ?, error_message = ?, updated_at = ?
                WHERE task_id = ? AND status = 'running'
                """,
                (
                    "unknown" if has_unknown else "failed",
                    orphan_message,
                    ts,
                    orphan["id"],
                ),
            )
    return len(rows) + len(orphans)
