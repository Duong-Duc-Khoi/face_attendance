"""
app/services/camera.py
CameraStream — đọc frame từ webcam và stream MJPEG.

Tách singleton quản lý (start/stop/get) ra cuối file thành module-level functions.
"""

import time
import threading

import cv2
import numpy as np

from app.core.config import settings
from app.services.face_engine import face_engine

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

    def capture_snapshot(self, emp_code: str, frame: np.ndarray | None = None) -> str:
        if frame is None:
            ret, frame = self.read()
            if not ret or frame is None:
                return ""
        ts   = time.strftime("%Y%m%d_%H%M%S")
        path = str(settings.CAPTURES_DIR / f"{emp_code}_{ts}.jpg")
        cv2.imwrite(path, frame)
        return path

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
