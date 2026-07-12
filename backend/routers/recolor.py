from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.file_service import save_existing_image_as_upload
from ..services.image_job_service import create_local_recolor_job, get_generated_image, get_job, get_upload
from ..services.recolor_service import (
    analyze_recolor_masks,
    apply_recolor,
    preview_recolor,
    result_payload,
    select_hardware_region,
)

router = APIRouter(prefix="/api/recolor", tags=["recolor"])


class AnalyzePayload(BaseModel):
    uploaded_image_id: int


class ApplyPayload(BaseModel):
    uploaded_image_id: int
    target_color: str
    subject_mask: str
    protect_mask: str
    recolor_strength: int = 86
    texture_strength: int = 78


class SelectPayload(BaseModel):
    uploaded_image_id: int
    protect_mask: str
    left: int
    top: int
    right: int
    bottom: int
    action: str = "add"


class ReusePayload(BaseModel):
    generated_image_id: int


@router.post("/analyze")
def analyze(payload: AnalyzePayload):
    upload = get_upload(payload.uploaded_image_id)
    if not upload:
        raise HTTPException(status_code=404, detail="上传图片不存在")
    try:
        return analyze_recolor_masks(upload["file_path"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/select")
def select(payload: SelectPayload):
    upload = get_upload(payload.uploaded_image_id)
    if not upload:
        raise HTTPException(status_code=404, detail="上传图片不存在")
    try:
        return select_hardware_region(
            upload["file_path"],
            payload.protect_mask,
            (payload.left, payload.top, payload.right, payload.bottom),
            payload.action,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/apply")
def apply(payload: ApplyPayload):
    upload = get_upload(payload.uploaded_image_id)
    if not upload:
        raise HTTPException(status_code=404, detail="上传图片不存在")
    try:
        result_path = apply_recolor(
            upload["file_path"], payload.target_color, payload.subject_mask, payload.protect_mask,
        )
        job_id = create_local_recolor_job(upload, payload.target_color, result_path)
        generated = get_job(job_id)["results"][0]
        reusable_upload = save_existing_image_as_upload(result_path, file_name=f"recolor_{job_id}.png")
        return {
            **result_payload(result_path),
            "job": get_job(job_id),
            "generated_image": generated,
            "uploaded_image": reusable_upload,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/preview")
def preview(payload: ApplyPayload):
    upload = get_upload(payload.uploaded_image_id)
    if not upload:
        raise HTTPException(status_code=404, detail="上传图片不存在")
    try:
        return preview_recolor(
            upload["file_path"], payload.target_color, payload.subject_mask, payload.protect_mask,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reuse")
def reuse(payload: ReusePayload):
    image = get_generated_image(payload.generated_image_id)
    if not image:
        raise HTTPException(status_code=404, detail="生成图片不存在")
    try:
        return save_existing_image_as_upload(image["image_path"], file_name=f"recolor_reuse_{payload.generated_image_id}.png")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
