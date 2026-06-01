"""Camera stream and control endpoints."""

import time

import cv2
import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.services.camera import (
    camera_status,
    get_camera,
    heartbeat_camera,
    start_camera,
    stop_camera,
)

router = APIRouter(tags=["camera"])


def _placeholder_mjpeg():
    """Stream frame tinh khi camera tat."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[:] = (20, 30, 45)
    cv2.putText(img, "CAMERA DA TAT", (175, 210),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (60, 60, 80), 2)
    cv2.putText(img, "Nhan [Bat Camera] de khoi dong", (110, 260),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (60, 60, 80), 1)
    _, jpeg = cv2.imencode(".jpg", img)
    packet = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n"
    while True:
        yield packet
        time.sleep(1.0)


@router.get("/video_feed")
def video_feed():
    cam = get_camera()
    if cam is None:
        return StreamingResponse(
            _placeholder_mjpeg(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )
    return StreamingResponse(
        cam.generate_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


async def _camera_client_id(request: Request) -> str:
    if request.query_params.get("client_id"):
        return request.query_params.get("client_id", "")
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            return str(payload.get("client_id") or "")
        if isinstance(payload, str):
            return payload
    except Exception:
        try:
            return (await request.body()).decode("utf-8").strip()
        except Exception:
            return ""
    return ""


@router.post("/api/camera/start")
async def api_camera_start(request: Request):
    return start_camera(owner_id=await _camera_client_id(request))


@router.post("/api/camera/stop")
async def api_camera_stop(request: Request):
    return stop_camera(owner_id=await _camera_client_id(request))


@router.post("/api/camera/heartbeat")
async def api_camera_heartbeat(request: Request):
    return heartbeat_camera(owner_id=await _camera_client_id(request))


@router.get("/api/camera/status")
def api_camera_status(client_id: str = ""):
    return camera_status(client_id=client_id)
