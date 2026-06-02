"""System health and runtime configuration endpoints."""

from fastapi import APIRouter

from app.core.config import settings
from app.services.camera import get_camera
from app.services.face_engine import face_engine

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health_check():
    cam = get_camera()
    return {
        "status": "ok",
        "camera": cam.cap.isOpened() if cam and cam.cap else False,
        "face_engine": face_engine._initialized,
        "employees": face_engine.registered_count,
    }


@router.get("/config")
def get_config():
    return {
        "threshold": face_engine.threshold,
        "cooldown_minutes": settings.COOLDOWN_MINUTES,
    }


@router.put("/config")
async def update_config(payload: dict):
    if "threshold" in payload:
        face_engine.threshold = float(payload["threshold"])
    return {"success": True, "message": "Da cap nhat cau hinh"}
