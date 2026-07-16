from typing import Any

import requests
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..services.vip_organizer_service import (
    analysis_config_status,
    asset_original,
    asset_thumbnail,
    analyze_assets,
    analyze_assets_with_api,
    export_package,
    export_file,
    export_zip,
    save_analysis_config,
    save_assets,
    start_session,
)

router = APIRouter(prefix="/api/vip-organizer", tags=["vip-organizer"])


class AnalyzePayload(BaseModel):
    session_id: str
    product_image_ids: list[int] = Field(default_factory=list)
    model_image_ids: list[int] = Field(default_factory=list)
    tag_image_ids: list[int] = Field(default_factory=list)
    asset_roles: dict[int, str] = Field(default_factory=dict)
    asset_tags: dict[int, list[str]] = Field(default_factory=dict)


class ExportPayload(BaseModel):
    session_id: str
    slots: list[dict[str, Any]] = Field(default_factory=list)
    product_info: dict[str, str] = Field(default_factory=dict)


class ApiAnalyzePayload(BaseModel):
    session_id: str
    product_image_ids: list[int] = Field(default_factory=list)
    api_config_id: int


class AnalysisConfigPayload(BaseModel):
    api_base_url: str
    api_key: str
    model_name: str


@router.post("/session")
def create_session():
    return start_session()


@router.post("/upload")
def upload_assets(
    session_id: str = Form(...),
    asset_type: str = Form(...),
    files: list[UploadFile] = File(...),
):
    try:
        return save_assets(session_id, asset_type, files)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/assets/{image_id}/thumbnail")
def get_asset_thumbnail(image_id: int):
    try:
        path = asset_thumbnail(image_id)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000, immutable"})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/assets/{image_id}/original")
def get_asset_original(image_id: int):
    try:
        path = asset_original(image_id)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/exports/{export_id}/files/{file_name}")
def get_export_file(export_id: str, file_name: str):
    try:
        path = export_file(export_id, file_name)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/exports/{export_id}/download")
def download_export(export_id: str):
    try:
        path = export_zip(export_id)
        return FileResponse(path, media_type="application/zip", filename=path.name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/analyze")
def analyze(payload: AnalyzePayload):
    try:
        return analyze_assets(
            payload.session_id,
            payload.product_image_ids,
            payload.model_image_ids,
            payload.tag_image_ids,
            payload.asset_roles,
            payload.asset_tags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analysis-config")
def get_analysis_config():
    return analysis_config_status()


@router.post("/analysis-config")
def update_analysis_config(payload: AnalysisConfigPayload):
    try:
        return save_analysis_config(payload.api_base_url, payload.api_key, payload.model_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/analyze-with-api")
def analyze_with_api(payload: ApiAnalyzePayload):
    try:
        return analyze_assets_with_api(payload.session_id, payload.product_image_ids, payload.api_config_id)
    except (ValueError, requests.RequestException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/export")
def export(payload: ExportPayload):
    try:
        return export_package(payload.session_id, payload.slots, payload.product_info)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
