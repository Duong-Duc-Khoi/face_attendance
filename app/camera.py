import cv2
import threading
import time
import numpy as np
from app.face_engine import face_engine
from app.database import SessionLocal, Employee


class CameraStream:
    def __init__(self, camera_id: int = 0):
        self.camera_id        = camera_id
        self.cap              = None
        self.frame            = None       # Frame thô mới nhất
        self.lock             = threading.Lock()
        self.running          = False
        self._capture_thread  = None

        # ── Cache kết quả nhận diện (cập nhật ~1 lần/giây bởi WebSocket) ──
        self._last_results    = []         # list kết quả recognize()
        self._last_emp_map    = {}         # { emp_code: name }
        self._result_lock     = threading.Lock()

        self._connect()

    # ──────────────────────────────────────────
    # Kết nối & thread đọc frame
    # ──────────────────────────────────────────
    def _connect(self):
        """Kết nối camera — dùng CAP_DSHOW trên Windows tránh lag khởi tạo"""
        self.cap = cv2.VideoCapture(self.camera_id, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            # Fallback không dùng DSHOW (Linux/Mac)
            self.cap = cv2.VideoCapture(self.camera_id)

        if self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self.cap.set(cv2.CAP_PROP_FPS,          60)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)   # Luôn lấy frame mới nhất
            self.cap.set(cv2.CAP_PROP_AUTOFOCUS,    1)   # Bật autofocus
            print(f"  ✓ Camera {self.camera_id} đã kết nối")
            self._start_capture_thread()
        else:
            print(f"  ⚠ Không thể kết nối camera {self.camera_id}")

    def _start_capture_thread(self):
        self.running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True)
        self._capture_thread.start()

    def _capture_loop(self):
        """Thread 1 — chỉ đọc frame, KHÔNG nhận diện"""
        while self.running:
            if self.cap and self.cap.isOpened():
                ret, frame = self.cap.read()
                if ret:
                    with self.lock:
                        self.frame = frame
            # Không sleep — đọc nhanh nhất có thể để giảm lag buffer
        
    # ──────────────────────────────────────────
    # Đọc frame
    # ──────────────────────────────────────────
    def read(self):
        """Đọc frame thô mới nhất"""
        with self.lock:
            if self.frame is not None:
                return True, self.frame.copy()
        return False, None

    # ──────────────────────────────────────────
    # Cache kết quả nhận diện từ WebSocket loop
    # ──────────────────────────────────────────
    def update_recognition_results(self, results: list, emp_map: dict):
        """
        Được gọi từ WebSocket loop (~1 lần/giây).
        Lưu kết quả để MJPEG stream vẽ lên mà không cần chạy AI.
        """
        with self._result_lock:
            self._last_results = results
            self._last_emp_map = emp_map

    def get_recognition_results(self):
        with self._result_lock:
            return self._last_results.copy(), dict(self._last_emp_map)

    # ──────────────────────────────────────────
    # Nhận diện (chỉ gọi từ WebSocket loop)
    # ──────────────────────────────────────────
    def _employee_map(self) -> dict:
        db = SessionLocal()
        try:
            emps = db.query(Employee).filter_by(is_active=True).all()
            return {e.emp_code: e.name for e in emps}
        finally:
            db.close()

    def run_recognition(self):
        """
        Thread 3 (WebSocket) gọi hàm này ~1 lần/giây.
        Chạy AI, cache kết quả, KHÔNG vẽ lên frame.
        Trả về: (frame_thô, results)
        """
        ret, frame = self.read()
        if not ret or frame is None:
            return None, []

        results = face_engine.recognize(frame)
        emp_map = self._employee_map() if results else {}
        self.update_recognition_results(results, emp_map)
        return frame, results

    # ──────────────────────────────────────────
    # MJPEG stream — KHÔNG chạy AI
    # ──────────────────────────────────────────
    def generate_mjpeg(self):
        """
        Thread 2 — stream nhanh 25fps.
        Lấy frame thô + overlay kết quả nhận diện đã cache → KHÔNG lag.
        """
        while self.running:
            ret, frame = self.read()
            if not ret or frame is None:
                frame = self._make_placeholder()
            else:
                # Vẽ kết quả nhận diện đã cache (không gọi AI)
                results, emp_map = self.get_recognition_results()
                if results:
                    face_engine.draw_results(frame, results, emp_map)

            _, jpeg = cv2.imencode('.jpg', frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, 90])
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n'
                   + jpeg.tobytes()
                   + b'\r\n')
            time.sleep(1 / 25)   # 25 FPS

    # ──────────────────────────────────────────
    # Placeholder & snapshot
    # ──────────────────────────────────────────
    def _make_placeholder(self) -> np.ndarray:
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        img[:] = (20, 30, 45)
        cv2.putText(img, "CAMERA OFFLINE", (160, 220),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (60, 60, 80), 2)
        return img

    def capture_snapshot(self, emp_code: str) -> str:
        ret, frame = self.read()
        if not ret:
            return ""
        ts   = time.strftime("%Y%m%d_%H%M%S")
        path = f"data/captures/{emp_code}_{ts}.jpg"
        cv2.imwrite(path, frame)
        return path

    def release(self):
        self.running = False
        if self.cap:
            self.cap.release()


# ──────────────────────────────────────────
# Singleton + Camera ON/OFF control
# ──────────────────────────────────────────
_camera_instance = None
_camera_enabled  = False


def is_camera_enabled() -> bool:
    return _camera_enabled


def start_camera(camera_id: int = 0) -> dict:
    global _camera_instance, _camera_enabled
    if _camera_enabled and _camera_instance is not None:
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
    if _camera_instance is not None:
        _camera_instance.release()
        _camera_instance = None
    return {"success": True, "message": "Đã tắt camera"}


def get_camera():
    return _camera_instance if _camera_enabled else None


def release_camera():
    global _camera_instance, _camera_enabled
    _camera_enabled = False
    if _camera_instance is not None:
        _camera_instance.release()
        _camera_instance = None