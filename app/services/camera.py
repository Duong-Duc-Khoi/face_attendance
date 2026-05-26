"""
app/services/camera.py
CameraStream — đọc frame từ webcam và stream MJPEG.

Tách singleton quản lý (start/stop/get) ra cuối file thành module-level functions.
"""

import hashlib
import os
import time
import threading
from datetime import datetime

import cv2
import numpy as np

from app.core.config import settings
from app.services.face_engine import face_engine

try:
    from PIL import Image, ImageDraw, ImageFont

    _capture_font = _capture_font_small = None
    for _font_path in [
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]:
        if os.path.exists(_font_path):
            _capture_font = ImageFont.truetype(_font_path, 20)
            _capture_font_small = ImageFont.truetype(_font_path, 16)
            break
    PIL_AVAILABLE = _capture_font is not None
except ImportError:
    PIL_AVAILABLE = False

_EMP_MAP_TTL = 30.0  # giây — làm mới employee map từ DB


class CameraStream:
    def __init__(self, camera_id: int = settings.CAMERA_ID):
        self.camera_id       = camera_id
        self.cap             = None
        self.frame           = None
        self.lock            = threading.Lock()
        self.running         = False
        self._capture_thread = None
        self._frame_id       = 0

        # Cache kết quả nhận diện (do WebSocket cập nhật)
        self._last_results  = []
        self._last_emp_map  = {}
        self._result_lock   = threading.Lock()

        # Cache employee map
        self._emp_map_cache: dict  = {}
        self._emp_map_ts:    float = 0.0

        self._connect()

    # ── Connect ─────────────────────────────────────────────────
    def _connect(self):
        """Thử CAP_DSHOW (Windows) trước, fallback sang backend mặc định."""
        self.cap = cv2.VideoCapture(self.camera_id, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            self.cap = cv2.VideoCapture(self.camera_id)

        if self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self.cap.set(cv2.CAP_PROP_FPS,          30)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)
            self.cap.set(cv2.CAP_PROP_AUTOFOCUS,    1)
            print(f"  ✓ Camera {self.camera_id} đã kết nối")
            self._start_capture_thread()
        else:
            print(f"  ⚠ Không thể kết nối camera {self.camera_id}")

    def _start_capture_thread(self):
        self.running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

    def _capture_loop(self):
        """Thread riêng — chỉ đọc frame, KHÔNG nhận diện."""
        while self.running:
            if self.cap and self.cap.isOpened():
                ret, frame = self.cap.read()
                if ret:
                    with self.lock:
                        self.frame    = frame
                        self._frame_id += 1
            else:
                time.sleep(0.1)

    # ── Đọc frame ───────────────────────────────────────────────
    def read(self):
        with self.lock:
            if self.frame is not None:
                return True, self.frame.copy()
        return False, None

    # ── Cache nhận diện ─────────────────────────────────────────
    def update_recognition_results(self, results: list, emp_map: dict):
        with self._result_lock:
            self._last_results = results
            self._last_emp_map = emp_map

    def get_recognition_results(self):
        with self._result_lock:
            return self._last_results.copy(), dict(self._last_emp_map)

    # ── Employee map có TTL ──────────────────────────────────────
    def _employee_map(self) -> dict:
        now = time.monotonic()
        if now - self._emp_map_ts < _EMP_MAP_TTL and self._emp_map_cache:
            return self._emp_map_cache
        from app.core.database import SessionLocal
        from app.models.employee import Employee
        db = SessionLocal()
        try:
            emps = db.query(Employee).filter_by(is_active=True).all()
            self._emp_map_cache = {e.emp_code: e.name for e in emps}
            self._emp_map_ts    = now
            return self._emp_map_cache
        finally:
            db.close()

    # ── Nhận diện (gọi từ WebSocket) ────────────────────────────
    def run_recognition(self) -> tuple:
        ret, frame = self.read()
        if not ret or frame is None:
            return None, [], {}
        results = face_engine.recognize(frame)
        emp_map = self._employee_map() if results else {}
        self.update_recognition_results(results, emp_map)
        return frame, results, emp_map

    # ── MJPEG stream ────────────────────────────────────────────
    def generate_mjpeg(self):
        INTERVAL      = 1.0 / 25
        last_fid      = -1
        cached_packet = None

        while self.running:
            t0 = time.perf_counter()
            with self.lock:
                fid     = self._frame_id
                has_new = fid != last_fid
                frame   = self.frame.copy() if has_new and self.frame is not None else None

            if has_new:
                last_fid = fid
                frame    = frame if frame is not None else self._make_placeholder()
                frame    = cv2.flip(frame, 1)
                _, jpeg  = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                cached_packet = (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + jpeg.tobytes()
                    + b"\r\n"
                )
            if cached_packet:
                yield cached_packet

            wait = INTERVAL - (time.perf_counter() - t0)
            if wait > 0.001:
                time.sleep(wait)

    # ── Placeholder & Snapshot ───────────────────────────────────
    def _make_placeholder(self) -> np.ndarray:
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        img[:] = (20, 30, 45)
        cv2.putText(img, "CAMERA OFFLINE", (160, 220),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (60, 60, 80), 2)
        return img

    @staticmethod
    def _safe_name(value: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value or ""))
        return safe.strip("_") or "unknown"

    @staticmethod
    def _metadata_datetime(metadata: dict) -> datetime:
        raw = str((metadata or {}).get("timestamp") or "")
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return datetime.now()

    @staticmethod
    def _draw_capture_overlay(frame: np.ndarray, metadata: dict, captured_at: datetime) -> np.ndarray:
        name = metadata.get("name") or metadata.get("emp_code") or ""
        emp_code = metadata.get("emp_code") or ""
        check_type = "Vào làm" if metadata.get("check_type") == "check_in" else "Ra về"
        confidence = metadata.get("confidence")
        try:
            confidence_text = f"{float(confidence) * 100:.0f}%"
        except (TypeError, ValueError):
            confidence_text = "--"
        log_ref = metadata.get("log_id") or metadata.get("id") or "-"
        time_text = captured_at.strftime("%H:%M:%S")
        date_text = captured_at.strftime("%d/%m/%Y")
        iso_text = captured_at.strftime("%Y-%m-%d %H:%M:%S")
        trace_text = f"{settings.APP_NAME} | {iso_text} | {emp_code} | LOG {log_ref}"

        if PIL_AVAILABLE:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb).convert("RGBA")
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            margin = max(14, int(min(image.width, image.height) * 0.018))

            # Audit border and corner ticks make cropping or replacement easier to notice.
            border = (0, 212, 170, 130)
            draw.rectangle((margin, margin, image.width - margin, image.height - margin), outline=border, width=2)
            tick = max(28, int(min(image.width, image.height) * 0.055))
            for x1, y1, x2, y2 in (
                (margin, margin, margin + tick, margin),
                (margin, margin, margin, margin + tick),
                (image.width - margin - tick, margin, image.width - margin, margin),
                (image.width - margin, margin, image.width - margin, margin + tick),
                (margin, image.height - margin, margin + tick, image.height - margin),
                (margin, image.height - margin - tick, margin, image.height - margin),
                (image.width - margin - tick, image.height - margin, image.width - margin, image.height - margin),
                (image.width - margin, image.height - margin - tick, image.width - margin, image.height - margin),
            ):
                draw.line((x1, y1, x2, y2), fill=(255, 255, 255, 150), width=2)

            panel_w = min(image.width - margin * 2, 680)
            panel_h = 104
            panel_x = margin
            panel_y = image.height - margin - panel_h
            for i in range(panel_h):
                alpha = int(210 - (i / panel_h) * 42)
                draw.line((panel_x, panel_y + i, panel_x + panel_w, panel_y + i), fill=(4, 12, 24, alpha))
            draw.rounded_rectangle(
                (panel_x, panel_y, panel_x + panel_w, panel_y + panel_h),
                radius=8,
                outline=(0, 212, 170, 210),
                width=2,
            )
            draw.rectangle((panel_x, panel_y, panel_x + 6, panel_y + panel_h), fill=(0, 212, 170, 230))
            draw.text((panel_x + 20, panel_y + 14), time_text, font=_capture_font, fill=(255, 255, 255, 255))
            draw.text((panel_x + 130, panel_y + 18), date_text, font=_capture_font_small, fill=(203, 213, 225, 255))
            draw.text((panel_x + 20, panel_y + 48), f"{name} ({emp_code})", font=_capture_font_small, fill=(248, 250, 252, 255))
            draw.text((panel_x + 20, panel_y + 73), f"{check_type}  |  Confidence {confidence_text}  |  Log #{log_ref}", font=_capture_font_small, fill=(203, 213, 225, 255))
            image = Image.alpha_composite(image, overlay).convert("RGB")
            return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

        out = frame.copy()
        cv2.rectangle(out, (16, 16), (out.shape[1] - 16, out.shape[0] - 16), (0, 212, 170), 2)
        panel_h = 104
        y0 = out.shape[0] - panel_h - 18
        panel = out.copy()
        cv2.rectangle(panel, (18, y0), (690, y0 + panel_h), (8, 18, 32), -1)
        out = cv2.addWeighted(panel, 0.72, out, 0.28, 0)
        cv2.rectangle(out, (18, y0), (690, y0 + panel_h), (0, 212, 170), 2)
        cv2.rectangle(out, (18, y0), (26, y0 + panel_h), (0, 212, 170), -1)
        cv2.putText(out, time_text, (40, y0 + 34), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(out, date_text, (168, y0 + 34), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (203, 213, 225), 1, cv2.LINE_AA)
        cv2.putText(out, f"{name} ({emp_code})", (40, y0 + 64), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (248, 250, 252), 1, cv2.LINE_AA)
        cv2.putText(out, f"{check_type} | Confidence {confidence_text} | Log #{log_ref}", (40, y0 + 91),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.48, (203, 213, 225), 1, cv2.LINE_AA)
        return out

    def capture_snapshot(
        self,
        emp_code: str,
        frame: np.ndarray | None = None,
        metadata: dict | None = None,
    ) -> dict:
        if frame is None:
            ret, frame = self.read()
            if not ret or frame is None:
                return {}
        metadata = metadata or {}
        captured_at = self._metadata_datetime(metadata)
        day_dir = settings.CAPTURES_DIR / captured_at.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        log_id = self._safe_name(metadata.get("log_id") or metadata.get("id") or "noid")
        safe_emp = self._safe_name(emp_code)
        safe_type = self._safe_name(metadata.get("check_type") or "attendance")
        ts = captured_at.strftime("%Y%m%d_%H%M%S")
        path = day_dir / f"{log_id}_{safe_emp}_{safe_type}_{ts}.jpg"
        evidence = self._draw_capture_overlay(frame.copy(), metadata | {"emp_code": emp_code}, captured_at)
        ok = cv2.imwrite(str(path), evidence, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            return {}
        digest = hashlib.sha256(path.read_bytes()).hexdigest()

        return {
            "path": str(path),
            "image_hash": digest,
            "captured_at": captured_at.isoformat(),
        }

    def release(self):
        self.running = False
        if self.cap:
            self.cap.release()


# ── Singleton & ON/OFF control ──────────────────────────────────
_camera_instance: CameraStream | None = None
_camera_enabled: bool = False


def is_camera_enabled() -> bool:
    return _camera_enabled


def start_camera(camera_id: int = settings.CAMERA_ID) -> dict:
    global _camera_instance, _camera_enabled
    if _camera_enabled and _camera_instance:
        return {"success": True, "message": "Camera đang chạy"}
    try:
        _camera_instance = CameraStream(camera_id=camera_id)
        _camera_enabled  = True
        return {"success": True, "message": "Đã bật camera"}
    except Exception as e:
        return {"success": False, "message": f"Lỗi khi bật camera: {e}"}


def stop_camera() -> dict:
    global _camera_instance, _camera_enabled
    _camera_enabled = False
    if _camera_instance:
        _camera_instance.release()
        _camera_instance = None
    return {"success": True, "message": "Đã tắt camera"}


def get_camera() -> CameraStream | None:
    return _camera_instance if _camera_enabled else None


def release_camera():
    global _camera_instance, _camera_enabled
    _camera_enabled = False
    if _camera_instance:
        _camera_instance.release()
        _camera_instance = None
