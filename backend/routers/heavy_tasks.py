from fastapi import APIRouter, HTTPException

from ..services.heavy_task_service import prewarm_heavy_task


router = APIRouter(prefix="/api/heavy-tasks", tags=["heavy-tasks"])


@router.post("/prewarm/{feature}")
def prewarm(feature: str):
    try:
        return prewarm_heavy_task(feature)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
