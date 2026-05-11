"""
app/main.py
FastAPI application entry point.

Chỉ làm 3 việc:
  1. Khởi tạo app + middleware
  2. Mount static, đăng ký routers
  3. Định nghĩa lifespan (startup/shutdown)

Business logic KHÔNG nằm ở đây.
"""

import time
import cv2
import numpy as np
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.database import init_db
from app.services.face_engine import face_engine
from app.services.camera import get_camera, release_camera, start_camera, stop_camera, is_camera_enabled
from app.services.attendance import get_summary_today
from app.services.notify import notify_daily_report_async
from app.api.v1 import employees, reports
from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.ws import ws_attendance
from app.api.v1.leave import router as leave_router
from app.api.v1.calendar import router as calendar_router
from app.services.attendance import get_summary_today, auto_checkout_missing
scheduler = AsyncIOScheduler()


# ── Lifespan ────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n  FaceAttend — Khởi động hệ thống")
    init_db()
    print(f"  ✓ Database sẵn sàng")
    print(f"  ✓ Face engine: {face_engine.registered_count} nhân viên đã đăng ký")
    print(f"  ✓ Camera sẽ mở khi có người truy cập")

    async def _daily_report():
        summary = get_summary_today()
        await notify_daily_report_async(summary)

    async def _auto_checkout():
        count = auto_checkout_missing()
        print(f"  ✓ Auto checkout: {count} nhân viên chưa check out")

    scheduler.add_job(_daily_report, CronTrigger(hour=18, minute=0),
                      id="daily_report", replace_existing=True)
    scheduler.add_job(_auto_checkout, CronTrigger(hour=23, minute=59),
                      id="auto_checkout", replace_existing=True)
    scheduler.start()
    print(f"  ✓ Scheduler bật — báo cáo ngày gửi lúc 18:00, auto checkout lúc 23:59")
    yield
    scheduler.shutdown(wait=False)
    release_camera()
    print("  FaceAttend — Đã tắt")

    

# ── App ──────────────────────────────────────────────────────────
app = FastAPI(
    title       = "FaceAttend API",
    description = "Hệ thống chấm công nhận diện khuôn mặt",
    version     = "1.0.0",
    lifespan    = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/data",   StaticFiles(directory="data"),   name="data")

templates = Jinja2Templates(directory="templates")

# Routers
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(employees.router)
app.include_router(reports.router)
app.include_router(leave_router)
app.include_router(calendar_router)


# ── Auth pages ───────────────────────────────────────────────────
@app.get("/auth/login-page")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

# ── HTML pages ───────────────────────────────────────────────────
@app.get("/")
async def kiosk_page(request: Request):
    return templates.TemplateResponse("kiosk.html", {"request": request})

@app.get("/me")
async def me_page(request: Request):
    return templates.TemplateResponse("me.html", {"request": request})

@app.get("/register")
async def register_page_face(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.get("/dashboard")
async def dashboard_page(request: Request):
    summary = get_summary_today()
    return templates.TemplateResponse("dashboard.html", {"request": request, "summary": summary})

@app.get("/report")
async def report_page(request: Request):
    return templates.TemplateResponse("reports.html", {"request": request})

@app.get("/users")
async def users_page(request: Request):
    return templates.TemplateResponse("users.html", {"request": request})

# ── Camera stream ────────────────────────────────────────────────
def _placeholder_mjpeg():
    """Stream frame tĩnh khi camera tắt."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[:] = (20, 30, 45)
    cv2.putText(img, "CAMERA DA TAT", (175, 210),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (60, 60, 80), 2)
    cv2.putText(img, "Nhan [Bat Camera] de khoi dong", (110, 260),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (60, 60, 80), 1)
    _, jpeg = cv2.imencode(".jpg", img)
    packet  = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n"
    while True:
        yield packet
        time.sleep(1.0)


@app.get("/video_feed")
def video_feed():
    cam = get_camera()
    if cam is None:
        return StreamingResponse(_placeholder_mjpeg(),
                                 media_type="multipart/x-mixed-replace; boundary=frame")
    return StreamingResponse(cam.generate_mjpeg(),
                             media_type="multipart/x-mixed-replace; boundary=frame")


# ── WebSocket ────────────────────────────────────────────────────
@app.websocket("/ws/attendance")
async def ws_attendance_route(websocket: WebSocket):
    await ws_attendance(websocket)


# ── Camera control ───────────────────────────────────────────────
@app.post("/api/camera/start")
def api_camera_start():
    return start_camera()

@app.post("/api/camera/stop")
def api_camera_stop():
    return stop_camera()

@app.get("/api/camera/status")
def api_camera_status():
    cam = get_camera()
    return {"enabled": is_camera_enabled(), "opened": cam.cap.isOpened() if cam and cam.cap else False}


# ── Misc ─────────────────────────────────────────────────────────

@app.get("/api/health")
def health_check():
    cam = get_camera()
    return {
        "status":      "ok",
        "camera":      cam.cap.isOpened() if cam and cam.cap else False,
        "face_engine": face_engine._initialized,
        "employees":   face_engine.registered_count,
    }

@app.get("/api/config")
def get_config():
    return {
        "threshold":        face_engine.threshold,
        "cooldown_minutes": settings.COOLDOWN_MINUTES,
        "work_start":       settings.WORK_START,
    }

@app.put("/api/config")
async def update_config(payload: dict):
    if "threshold" in payload:
        face_engine.threshold = float(payload["threshold"])
    return {"success": True, "message": "Đã cập nhật cấu hình"}
