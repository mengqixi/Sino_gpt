from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ..services.product_image_service import (
    ProductImageConflict,
    ProductImageError,
    asset_file,
    build_task_zip,
    call_plan,
    cleanup_expired_sources,
    create_task,
    delete_asset,
    delete_task,
    delete_task_sources,
    get_task,
    list_task_history,
    mark_task_activity,
    output_file,
    render_logo_crop,
    save_browser_video_frames,
    save_image_assets,
    save_transparent_data_url,
    save_transparent_upload,
    save_video_assets,
    select_reference,
)
from ..services.product_image_worker import (
    prepare_analysis,
    prepare_generation,
    run_analysis,
    run_generation,
)


router = APIRouter(prefix="/api/product-images", tags=["product-images"])


class CreateTaskPayload(BaseModel):
    product_code: str
    color: str
    previous_task_id: str | None = None


class AnalyzePayload(BaseModel):
    api_config_id: int
    confirmed_call_count: int = Field(ge=1, le=5)


class ReferencePayload(BaseModel):
    asset_id: int


class GeneratePayload(BaseModel):
    api_config_id: int
    confirmed_call_count: int = Field(ge=1, le=4)
    acknowledge_possible_charge: bool = False


class TransparentPayload(BaseModel):
    image_data_url: str = Field(min_length=32, max_length=54 * 1024 * 1024)


class LogoCropPayload(BaseModel):
    left: float
    top: float
    right: float
    bottom: float


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProductImageConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ProductImageError):
        detail = str(exc)
        status = 507 if "磁盘空间不足" in detail else (404 if "不存在" in detail else 400)
        return HTTPException(status_code=status, detail=detail)
    return HTTPException(status_code=400, detail=str(exc))


def _confirm_calls(task_id: str, confirmed: int, expected: int | None = None) -> int:
    planned = call_plan(task_id)
    count = int(expected if expected is not None else planned["maximum_total_calls"])
    if confirmed != count:
        raise ProductImageConflict(f"预计调用 API {count} 次，请重新确认后开始")
    return count


@router.post("/tasks")
def create_product_image_task(payload: CreateTaskPayload):
    try:
        cleanup_expired_sources()
        return create_task(payload.product_code, payload.color, payload.previous_task_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/history")
def product_image_history(limit: int = 50):
    try:
        return list_task_history(limit)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}")
def read_product_image_task(task_id: str):
    try:
        mark_task_activity(task_id)
        return get_task(task_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.delete("/tasks/{task_id}", status_code=204)
def remove_product_image_task(task_id: str):
    try:
        delete_task(task_id)
        return Response(status_code=204)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/images")
def upload_product_images(
    task_id: str,
    role: str = Form(...),
    files: list[UploadFile] = File(...),
):
    try:
        return {"assets": save_image_assets(task_id, role, files), "task": get_task(task_id)}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/videos")
def upload_product_videos(task_id: str, files: list[UploadFile] = File(...)):
    try:
        saved = save_video_assets(task_id, files)
        return {**saved, "task": get_task(task_id)}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/browser-video-frames")
def upload_browser_product_video_frames(
    task_id: str,
    duration_seconds: float = Form(...),
    files: list[UploadFile] = File(...),
    video_name: str | None = Form(None),
    original_video_name: str | None = Form(None),
    timestamps_ms: list[float] | None = Form(None),
):
    try:
        saved = save_browser_video_frames(
            task_id,
            original_video_name or video_name or "",
            duration_seconds,
            files,
            timestamps_ms,
        )
        return {**saved, "task": get_task(task_id)}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}/assets/{asset_id}/file")
def read_product_asset(task_id: str, asset_id: int):
    try:
        path, mime_type, file_name = asset_file(task_id, asset_id)
        return FileResponse(
            path,
            media_type=mime_type,
            filename=file_name if mime_type and mime_type.startswith("video/") else None,
            headers={"Cache-Control": "private, no-store"},
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.delete("/tasks/{task_id}/assets/{asset_id}", status_code=204)
def remove_product_asset(task_id: str, asset_id: int):
    try:
        delete_asset(task_id, asset_id)
        return Response(status_code=204)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/analyze")
def analyze_product_assets(task_id: str, payload: AnalyzePayload, background_tasks: BackgroundTasks):
    try:
        _confirm_calls(task_id, payload.confirmed_call_count, 1)
        call_id = prepare_analysis(task_id, payload.api_config_id)
        background_tasks.add_task(run_analysis, task_id, call_id)
        return {"call_id": call_id, "task": get_task(task_id)}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.patch("/tasks/{task_id}/references/{role}")
def choose_product_reference(task_id: str, role: str, payload: ReferencePayload):
    try:
        return select_reference(task_id, role, payload.asset_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}/call-plan")
def product_image_call_plan(task_id: str):
    try:
        return call_plan(task_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/generate")
def generate_product_images(task_id: str, payload: GeneratePayload, background_tasks: BackgroundTasks):
    try:
        expected = int(call_plan(task_id)["generation_calls"])
        _confirm_calls(task_id, payload.confirmed_call_count, expected)
        slots = prepare_generation(task_id, payload.api_config_id, mode="initial")
        background_tasks.add_task(run_generation, task_id, slots, payload.api_config_id)
        return {"queued_slots": slots, "task": get_task(task_id)}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/resume")
def resume_product_images(task_id: str, payload: GeneratePayload, background_tasks: BackgroundTasks):
    try:
        expected = int(call_plan(task_id)["generation_calls"])
        _confirm_calls(task_id, payload.confirmed_call_count, expected)
        slots = prepare_generation(
            task_id,
            payload.api_config_id,
            mode="resume",
            acknowledge_possible_charge=payload.acknowledge_possible_charge,
        )
        background_tasks.add_task(run_generation, task_id, slots, payload.api_config_id)
        return {"queued_slots": slots, "task": get_task(task_id)}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/outputs/{slot}/regenerate")
def regenerate_product_image(
    task_id: str,
    slot: str,
    payload: GeneratePayload,
    background_tasks: BackgroundTasks,
):
    try:
        _confirm_calls(task_id, payload.confirmed_call_count, 1)
        slots = prepare_generation(
            task_id,
            payload.api_config_id,
            mode="single",
            slot=slot,
            acknowledge_possible_charge=payload.acknowledge_possible_charge,
        )
        background_tasks.add_task(run_generation, task_id, slots, payload.api_config_id)
        return {"queued_slots": slots, "task": get_task(task_id)}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put("/tasks/{task_id}/outputs/front_transparent")
def update_transparent_product_image(task_id: str, payload: TransparentPayload):
    try:
        return save_transparent_data_url(task_id, payload.image_data_url)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/outputs/front_transparent/file")
def upload_transparent_product_image(task_id: str, file: UploadFile = File(...)):
    try:
        return save_transparent_upload(task_id, file)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.patch("/tasks/{task_id}/outputs/logo_detail/crop")
def crop_product_logo(task_id: str, payload: LogoCropPayload):
    try:
        return render_logo_crop(task_id, payload.left, payload.top, payload.right, payload.bottom)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/sources/cleanup", status_code=204)
def cleanup_product_sources(task_id: str):
    try:
        delete_task_sources(task_id)
        return Response(status_code=204)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}/outputs/{slot}/{variant}/file")
def read_product_output(task_id: str, slot: str, variant: str):
    try:
        path, mime_type, file_name = output_file(task_id, slot, variant)
        return FileResponse(
            path,
            media_type=mime_type,
            headers={"Cache-Control": "private, no-store"},
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}/download")
def download_product_images(task_id: str):
    try:
        path = build_task_zip(task_id)
        return FileResponse(path, media_type="application/zip", filename=path.name)
    except Exception as exc:
        raise _http_error(exc) from exc
