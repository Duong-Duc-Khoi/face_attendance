import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.camera import camera
from app.face_engine import face_engine
from app.attendance import process_attendance, get_summary_today
from app.routes import employees, reports
from app.notify import telegram_checkin


# ──────────────────────────────────────────
# Startup / Shutdown
# ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n  FaceAttend — Khởi động hệ thống")
    init_db()
    print(f"  ✓ Database sẵn sàng")
    print(f"  ✓ Face engine: {face_engine.registered_count} nhân viên đã đăng ký")
    yield
    # Shutdown
    camera.release()
    print("  FaceAttend — Đã tắt")


# ──────────────────────────────────────────
# App
# ──────────────────────────────────────────
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

app.mount("/static", StaticFiles(directory="static"),      name="static")
app.mount("/data",   StaticFiles(directory="data"),        name="data")

templates = Jinja2Templates(directory="templates")

# Đăng ký routes
app.include_router(employees.router)
app.include_router(reports.router)


# ──────────────────────────────────────────
# Pages (HTML)
# ──────────────────────────────────────────
@app.get("/")
async def kiosk_page(request: Request):
    """Màn hình chấm công kiosk"""
    return templates.TemplateResponse("kiosk.html", {"request": request})


@app.get("/register")
async def register_page(request: Request):
    """Trang đăng ký nhân viên mới"""
    return templates.TemplateResponse("register.html", {"request": request})


@app.get("/dashboard")
async def dashboard_page(request: Request):
    """Dashboard quản lý"""
    summary = get_summary_today()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "summary": summary,
    })


@app.get("/report")
async def report_page(request: Request):
    """Trang báo cáo"""
    return templates.TemplateResponse("reports.html", {"request": request})


# ──────────────────────────────────────────
# Camera stream
# ──────────────────────────────────────────
@app.get("/video_feed")
def video_feed():
    """MJPEG stream từ camera — dùng trong <img src='/video_feed'>"""
    return StreamingResponse(
        camera.generate_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


# ──────────────────────────────────────────
# WebSocket — Nhận diện realtime
# ──────────────────────────────────────────
class ConnectionManager:
    """Quản lý nhiều WebSocket client cùng lúc"""
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        for ws in self.active.copy():
            try:
                await ws.send_json(data)
            except Exception:
                self.active.remove(ws)


manager = ConnectionManager()


@app.websocket("/ws/attendance")
async def ws_attendance(websocket: WebSocket):
    """
    WebSocket endpoint — đẩy kết quả nhận diện về browser realtime.
    Browser chỉ cần mở kết nối, server tự đẩy event khi có nhân viên chấm công.
    """
    await manager.connect(websocket)
    last_sent = {}   # { emp_code: timestamp } — chống gửi trùng

    try:
        while True:
            # Lấy frame và nhận diện
            _, results = camera.get_processed_frame()

            for r in results:
                if not r["recognized"]:
                    continue

                emp_code   = r["emp_code"]
                confidence = r["similarity"]

                # Chụp ảnh lưu evidence
                capture = camera.capture_snapshot(emp_code)

                # Xử lý logic chấm công
                log = process_attendance(emp_code, confidence, capture)
                if log:
                    # Gửi về tất cả client đang kết nối
                    await manager.broadcast(log)

                    # Thông báo Telegram (background, không chặn)
                    asyncio.create_task(telegram_checkin(log))

            await asyncio.sleep(0.4)   # Check ~2.5 lần/giây

    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ──────────────────────────────────────────
# Misc API
# ──────────────────────────────────────────
@app.get("/api/health")
def health_check():
    return {
        "status":      "ok",
        "camera":      camera.cap is not None and camera.cap.isOpened() if camera.cap else False,
        "face_engine": face_engine._initialized,
        "employees":   face_engine.registered_count,
    }


@app.get("/api/config")
def get_config():
    """Cấu hình hiện tại của hệ thống"""
    return {
        "threshold":        face_engine.threshold,
        "cooldown_minutes": 5,
        "work_start":       "08:30",
    }


@app.put("/api/config")
async def update_config(payload: dict):
    """Cập nhật cấu hình"""
    if "threshold" in payload:
        face_engine.threshold = float(payload["threshold"])
    return {"success": True, "message": "Đã cập nhật cấu hình"}
