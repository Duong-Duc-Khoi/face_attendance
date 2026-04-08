import cv2
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.camera import get_camera, release_camera, start_camera, stop_camera, is_camera_enabled
from app.database import init_db
from app.face_engine import face_engine
from app.attendance import get_summary_today
from app.notify import notify_daily_report_async
from app.routes import employees, reports
from app.routes.auth import router as auth_router
from app.ws import ws_attendance

scheduler = AsyncIOScheduler()


# ──────────────────────────────────────────
# Startup / Shutdown
# ──────────────────────────────────────────
async def _send_daily_report():
    summary = get_summary_today()
    await notify_daily_report_async(summary)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n  FaceAttend — Khoi dong he thong")
    init_db()
    print(f"  ✓ Database san sang (bao gom bang auth)")
    print(f"  ✓ Face engine: {face_engine.registered_count} nhan vien da dang ky")
    print(f"  ✓ Camera se mo khi co nguoi truy cap")
    scheduler.add_job(_send_daily_report, CronTrigger(hour=18, minute=0),
                      id="daily_report", replace_existing=True)
    scheduler.start()
    print(f"  ✓ Scheduler bat dau — bao cao ngay se gui luc 18:00")
    yield
    scheduler.shutdown(wait=False)
    release_camera()
    print("  FaceAttend — Da tat")


# ──────────────────────────────────────────
# App
# ──────────────────────────────────────────
app = FastAPI(
    title       = "FaceAttend API",
    description = "He thong cham cong nhan dien khuon mat",
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

# Dang ky routers
app.include_router(auth_router)
app.include_router(employees.router)
app.include_router(reports.router)


# ──────────────────────────────────────────
# Auth pages (public)
# ──────────────────────────────────────────
@app.get("/auth/login-page")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/auth/register-page")
async def register_page_auth(request: Request):
    return templates.TemplateResponse("user_register.html", {"request": request})


# ──────────────────────────────────────────
# Pages HTML
# ──────────────────────────────────────────
@app.get("/")
async def kiosk_page(request: Request):
    """Man hinh cham cong kiosk — public"""
    return templates.TemplateResponse("kiosk.html", {"request": request})


@app.get("/register")
async def register_page(request: Request):
    """Trang dang ky khuon mat nhan vien — can dang nhap (guard o JS)"""
    return templates.TemplateResponse("register.html", {"request": request})


@app.get("/dashboard")
async def dashboard_page(request: Request):
    """Dashboard quan ly — can dang nhap (guard o JS)"""
    summary = get_summary_today()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "summary": summary,
    })


@app.get("/report")
async def report_page(request: Request):
    """Trang bao cao — can dang nhap (guard o JS)"""
    return templates.TemplateResponse("reports.html", {"request": request})


# ──────────────────────────────────────────
# Camera stream
# ──────────────────────────────────────────
def _placeholder_mjpeg():
    """Stream anh placeholder khi camera tat — tao frame 1 lan, yield lai nhieu lan"""
    import numpy as np
    import time

    # Tạo và encode 1 lần duy nhất — frame tĩnh không cần rebuild mỗi giây
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[:] = (20, 30, 45)
    cv2.putText(img, "CAMERA DA TAT", (175, 210),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (60, 60, 80), 2)
    cv2.putText(img, "Nhan [Bat Camera] de khoi dong", (110, 260),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (60, 60, 80), 1)
    _, jpeg = cv2.imencode(".jpg", img)
    frame_bytes = (
        b'--frame\r\n'
        b'Content-Type: image/jpeg\r\n\r\n'
        + jpeg.tobytes()
        + b'\r\n'
    )

    while True:
        yield frame_bytes
        time.sleep(1.0)


@app.get("/video_feed")
def video_feed():
    cam = get_camera()
    if cam is None:
        return StreamingResponse(
            _placeholder_mjpeg(),
            media_type="multipart/x-mixed-replace; boundary=frame"
        )
    return StreamingResponse(
        cam.generate_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


# ──────────────────────────────────────────
# WebSocket — Nhan dien realtime
# ──────────────────────────────────────────
@app.websocket("/ws/attendance")
async def ws_attendance_route(websocket: WebSocket):
    await ws_attendance(websocket)


# ──────────────────────────────────────────
# Camera control API
# ──────────────────────────────────────────
@app.post("/api/camera/start")
def api_camera_start():
    """Bat camera"""
    return start_camera()

@app.post("/api/camera/stop")
def api_camera_stop():
    """Tat camera"""
    return stop_camera()

@app.get("/api/camera/status")
def api_camera_status():
    """Trang thai camera hien tai"""
    cam = get_camera()
    return {
        "enabled": is_camera_enabled(),
        "opened":  cam.cap.isOpened() if cam and cam.cap else False,
    }


# ──────────────────────────────────────────
# Misc API
# ──────────────────────────────────────────
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
    """Cau hinh hien tai cua he thong"""
    return {
        "threshold":        face_engine.threshold,
        "cooldown_minutes": 5,
        "work_start":       "08:30",
    }

@app.put("/api/config")
async def update_config(payload: dict):
    """Cap nhat cau hinh"""
    if "threshold" in payload:
        face_engine.threshold = float(payload["threshold"])
    return {"success": True, "message": "Da cap nhat cau hinh"}