from time import perf_counter
from queue import Empty, Queue
from threading import Thread

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from ..services.api_config_service import get_config
from ..services.file_service import crop_generated_image, save_existing_image_as_upload, split_image_grid
from ..services.image_job_service import (
    add_generated_image,
    create_job,
    get_generated_image,
    get_job,
    get_upload,
    replace_grid_split_images,
    set_job_status,
)
from ..services.relay_image_service import RelayGatewayTimeoutError, call_relay_image_api, save_images_from_response

router = APIRouter(prefix="/api", tags=["generate"])


class GeneratePayload(BaseModel):
    task_type: str
    uploaded_image_id: int | None = None
    uploaded_image_ids: list[int] | None = None
    prompt_template_id: int | None = None
    final_prompt: str
    api_config_id: int
    output_count: int | None = None
    image_size: str = "1024x1024"
    quality: str | None = None
    params: dict = {}


class CropPayload(BaseModel):
    left: float
    top: float
    right: float
    bottom: float


def _response_preview(value):
    if isinstance(value, dict):
        return {key: _response_preview(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_response_preview(item) for item in value]
    if isinstance(value, str) and len(value) > 2000:
        kind = "data_url" if value.startswith("data:image/") else "long_text"
        return f"[{kind} omitted, {len(value)} chars]"
    return value


def _run_generation(job_id: int, payload: dict, image_paths: list[str], config: dict):
    started = perf_counter()
    try:
        result_queue: Queue = Queue(maxsize=1)

        def request_relay():
            try:
                result_queue.put(("success", call_relay_image_api(
                    config,
                    image_paths,
                    payload["final_prompt"],
                    payload.get("output_count"),
                    payload.get("image_size") or "1024x1024",
                    payload.get("quality"),
                )))
            except Exception as exc:
                result_queue.put(("error", exc))

        hard_timeout = min(max(int(config.get("timeout_seconds") or 180), 30), 180)
        Thread(target=request_relay, daemon=True, name=f"relay-job-{job_id}").start()
        try:
            outcome, value = result_queue.get(timeout=hard_timeout)
        except Empty as exc:
            raise RelayGatewayTimeoutError(
                f"中转站请求超过应用硬超时 {hard_timeout} 秒，任务结果无法确认且可能已经扣费；系统不会自动重试。"
            ) from exc
        if outcome == "error":
            raise value
        response_json = value
        saved_images = save_images_from_response(response_json, config, job_id)
        auto_split = bool((payload.get("params") or {}).get("auto_split_grid", False))
        if auto_split and len(saved_images) == 1:
            try:
                split_paths = split_image_grid(saved_images[0]["path"], job_id)
                for path in split_paths:
                    add_generated_image(job_id, path, "split_grid")
            except Exception:
                add_generated_image(job_id, saved_images[0]["path"], saved_images[0]["source_type"])
        else:
            for item in saved_images:
                add_generated_image(job_id, item["path"], item["source_type"])
        set_job_status(job_id, "success", response_json=_response_preview(response_json))
    except RelayGatewayTimeoutError as exc:
        elapsed = round(perf_counter() - started, 1)
        set_job_status(job_id, "unknown", error_message=f"{exc}（本次请求耗时 {elapsed} 秒）")
    except Exception as exc:
        elapsed = round(perf_counter() - started, 1)
        set_job_status(job_id, "failed", error_message=f"{exc}（本次请求耗时 {elapsed} 秒）")


@router.post("/generate")
def generate(payload: GeneratePayload, background_tasks: BackgroundTasks):
    upload_ids = payload.uploaded_image_ids or ([payload.uploaded_image_id] if payload.uploaded_image_id else [])
    upload_ids = [image_id for image_id in upload_ids if image_id]
    if not upload_ids:
        raise HTTPException(status_code=400, detail="请先上传女包原图")
    uploads = []
    for image_id in upload_ids:
        upload_row = get_upload(image_id)
        if not upload_row:
            raise HTTPException(status_code=404, detail=f"上传图片不存在：{image_id}")
        uploads.append(upload_row)
    upload = uploads[0]
    config = get_config(payload.api_config_id, include_secret=True)
    if not config:
        raise HTTPException(status_code=404, detail="API 配置不存在")
    if not config.get("enabled"):
        raise HTTPException(status_code=400, detail="API 配置未启用")
    if not payload.final_prompt.strip():
        raise HTTPException(status_code=400, detail="最终提示词不能为空")

    job_id = create_job(payload.model_dump(), upload, config)
    set_job_status(job_id, "running")
    background_tasks.add_task(
        _run_generation,
        job_id,
        payload.model_dump(),
        [item["file_path"] for item in uploads],
        config,
    )
    job = get_job(job_id)
    return {"job_id": job_id, "status": job["status"], "job": job}


@router.get("/jobs/{job_id}")
def read_job(job_id: int):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.post("/generated-images/{image_id}/reuse")
def reuse_generated_image(image_id: int):
    image = get_generated_image(image_id)
    if not image:
        raise HTTPException(status_code=404, detail="生成图片不存在")
    try:
        return save_existing_image_as_upload(image["image_path"], file_name=f"continue_from_{image_id}.png")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/generated-images/{image_id}/split-grid")
def split_generated_grid(image_id: int):
    image = get_generated_image(image_id)
    if not image:
        raise HTTPException(status_code=404, detail="生成图片不存在")
    try:
        paths = split_image_grid(image["image_path"], image["job_id"])
        replace_grid_split_images(image["job_id"], image_id, paths)
        return {"job": get_job(image["job_id"])}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/generated-images/{image_id}/crop")
def crop_generated(image_id: int, payload: CropPayload):
    image = get_generated_image(image_id)
    if not image:
        raise HTTPException(status_code=404, detail="生成图片不存在")
    try:
        path = crop_generated_image(image["image_path"], image["job_id"], payload.left, payload.top, payload.right, payload.bottom)
        add_generated_image(image["job_id"], path, f"manual_crop:{image_id}")
        return {"job": get_job(image["job_id"])}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
