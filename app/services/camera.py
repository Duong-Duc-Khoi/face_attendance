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
_CAMERA_LEASE_TIMEOUT = 10.0


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
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self.cap.set(cv2.CAP_PROP_FPS,          30)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)
            self.cap.set(cv2.CAP_PROP_AUTOFOCUS,    1)
            actual_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            actual_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            actual_fps = self.cap.get(cv2.CAP_PROP_FPS) or 0
            fourcc = int(self.cap.get(cv2.CAP_PROP_FOURCC) or 0)
            fourcc_text = "".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4))
            fourcc_text = "".join(ch for ch in fourcc_text if ch.isprintable()).strip()
            print(
                f"  ✓ Camera {self.camera_id} đã kết nối — "
                f"{actual_width}x{actual_height}@{actual_fps:.1f}fps codec={fourcc_text or fourcc}"
            )
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
        INTERVAL      = 1.0 / 30
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
                _, jpeg  = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 94])
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


def save_capture_snapshot(
    frame: np.ndarray,
    emp_code: str,
    metadata: dict | None = None,
) -> dict:
    """Save an attendance evidence image from a frame supplied by a kiosk client."""
    if frame is None:
        return {}
    metadata = metadata or {}
    captured_at = CameraStream._metadata_datetime(metadata)
    day_dir = settings.CAPTURES_DIR / captured_at.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    log_id = CameraStream._safe_name(metadata.get("log_id") or metadata.get("id") or "noid")
    safe_emp = CameraStream._safe_name(emp_code)
    safe_type = CameraStream._safe_name(metadata.get("check_type") or "attendance")
    ts = captured_at.strftime("%Y%m%d_%H%M%S")
    path = day_dir / f"{log_id}_{safe_emp}_{safe_type}_{ts}.jpg"
    evidence = CameraStream._draw_capture_overlay(frame.copy(), metadata | {"emp_code": emp_code}, captured_at)
    ok = cv2.imwrite(str(path), evidence, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        return {}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": str(path),
        "image_hash": digest,
        "captured_at": captured_at.isoformat(),
    }


# ── Singleton & ON/OFF control ──────────────────────────────────
_camera_instance: CameraStream | None = None
_camera_enabled: bool = False
_camera_owner_id: str = ""
_camera_last_heartbeat: float = 0.0
_camera_lock = threading.Lock()
_watchdog_started = False


def is_camera_enabled() -> bool:
    _expire_camera_lease()
    return _camera_enabled


def _release_camera_unlocked():
    global _camera_instance, _camera_enabled, _camera_owner_id, _camera_last_heartbeat
    _camera_enabled = False
    _camera_owner_id = ""
    _camera_last_heartbeat = 0.0
    if _camera_instance:
        _camera_instance.release()
        _camera_instance = None


def _expire_camera_lease():
    with _camera_lock:
        if (
            _camera_enabled
            and _camera_owner_id
            and time.monotonic() - _camera_last_heartbeat > _CAMERA_LEASE_TIMEOUT
        ):
            print("  ⚠ Camera lease hết hạn — tự tắt camera")
            _release_camera_unlocked()


def _watchdog_loop():
    while True:
        time.sleep(2.0)
        _expire_camera_lease()


def _ensure_watchdog():
    global _watchdog_started
    with _camera_lock:
        if _watchdog_started:
            return
        _watchdog_started = True
    threading.Thread(target=_watchdog_loop, daemon=True).start()


def _camera_status_unlocked(client_id: str = "") -> dict:
    opened = _camera_instance.cap.isOpened() if _camera_instance and _camera_instance.cap else False
    return {
        "enabled": _camera_enabled,
        "opened": opened,
        "owner_id": _camera_owner_id,
        "owned_by_current": bool(client_id and _camera_owner_id == client_id),
    }


def camera_status(client_id: str = "") -> dict:
    _expire_camera_lease()
    with _camera_lock:
        return _camera_status_unlocked(client_id)


def start_camera(camera_id: int = settings.CAMERA_ID, owner_id: str = "") -> dict:
    global _camera_instance, _camera_enabled, _camera_owner_id, _camera_last_heartbeat
    _ensure_watchdog()
    owner_id = str(owner_id or "")
    with _camera_lock:
        if _camera_enabled and _camera_instance:
            if _camera_owner_id and owner_id and _camera_owner_id != owner_id:
                return {
                    "success": False,
                    "message": "Camera đang được dùng bởi tab khác",
                    **_camera_status_unlocked(owner_id),
                }
            if owner_id:
                _camera_owner_id = owner_id
                _camera_last_heartbeat = time.monotonic()
            return {"success": True, "message": "Camera đang chạy", **_camera_status_unlocked(owner_id)}
    try:
        cam = CameraStream(camera_id=camera_id)
        with _camera_lock:
            _camera_instance = cam
            _camera_enabled = True
            _camera_owner_id = owner_id
            _camera_last_heartbeat = time.monotonic() if owner_id else 0.0
            return {"success": True, "message": "Đã bật camera", **_camera_status_unlocked(owner_id)}
    except Exception as e:
        with _camera_lock:
            _release_camera_unlocked()
        return {"success": False, "message": f"Lỗi khi bật camera: {e}"}


def heartbeat_camera(owner_id: str = "") -> dict:
    global _camera_last_heartbeat
    owner_id = str(owner_id or "")
    _expire_camera_lease()
    with _camera_lock:
        if not _camera_enabled:
            return {"success": False, "message": "Camera đang tắt", **_camera_status_unlocked(owner_id)}
        if _camera_owner_id and owner_id != _camera_owner_id:
            return {"success": False, "message": "Tab hiện tại không sở hữu camera", **_camera_status_unlocked(owner_id)}
        _camera_last_heartbeat = time.monotonic()
        return {"success": True, "message": "Camera heartbeat ok", **_camera_status_unlocked(owner_id)}


def stop_camera(owner_id: str = "") -> dict:
    owner_id = str(owner_id or "")
    with _camera_lock:
        if _camera_owner_id and owner_id and _camera_owner_id != owner_id:
            return {
                "success": False,
                "message": "Tab hiện tại không sở hữu camera",
                **_camera_status_unlocked(owner_id),
            }
        _release_camera_unlocked()
        return {"success": True, "message": "Đã tắt camera", **_camera_status_unlocked(owner_id)}


def get_camera() -> CameraStream | None:
    _expire_camera_lease()
    return _camera_instance if _camera_enabled else None


def release_camera():
    with _camera_lock:
        _release_camera_unlocked()
