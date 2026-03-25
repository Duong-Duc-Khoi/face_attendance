import cv2
import threading
import time
import numpy as np
from app.face_engine import face_engine
from app.database import SessionLocal, Employee


class CameraStream:
    def __init__(self, camera_id: int = 0):
        self.camera_id     = camera_id
        self.cap           = None
        self.frame         = None          # Frame gốc mới nhất
        self.lock          = threading.Lock()
        self.running       = False
        self._capture_thread = None
        self._connect()

    def _connect(self):
        """Kết nối camera"""
        self.cap = cv2.VideoCapture(self.camera_id)
        if self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.cap.set(cv2.CAP_PROP_FPS,          60)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)   # Luôn lấy frame mới nhất
            print(f"  ✓ Camera {self.camera_id} đã kết nối")
            self._start_capture_thread()
        else:
            print(f"  ⚠ Không thể kết nối camera {self.camera_id} — dùng ảnh placeholder")

    def _start_capture_thread(self):
        """Thread liên tục đọc frame để tránh buffer lag"""
        self.running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

    def _capture_loop(self):
        while self.running:
            if self.cap and self.cap.isOpened():
                ret, frame = self.cap.read()
                if ret:
                    with self.lock:
                        self.frame = frame
            time.sleep(0.01)   # ~100fps đọc, tránh CPU 100%

    def read(self):
        """Đọc frame mới nhất"""
        with self.lock:
            if self.frame is not None:
                return True, self.frame.copy()
        return False, None

    def _employee_map(self) -> dict:
        """Cache tên nhân viên để vẽ lên frame"""
        db = SessionLocal()
        try:
            emps = db.query(Employee).filter_by(is_active=True).all()
            return {e.emp_code: e.name for e in emps}
        finally:
            db.close()

    def get_processed_frame(self):
        """
        Lấy frame đã qua nhận diện khuôn mặt.
        Trả về: (frame_with_boxes, results_list)
        """
        ret, frame = self.read()
        if not ret or frame is None:
            # Trả về ảnh placeholder nếu không có camera
            placeholder = self._make_placeholder()
            return placeholder, []

        results = face_engine.recognize(frame)
        emp_map = self._employee_map() if results else {}
        face_engine.draw_results(frame, results, emp_map)
        return frame, results

    def _make_placeholder(self) -> np.ndarray:
        """Ảnh placeholder khi không có camera"""
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        img[:] = (20, 30, 45)
        cv2.putText(img, "CAMERA OFFLINE", (160, 220),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (60, 60, 80), 2)
        cv2.putText(img, f"Camera ID: {self.camera_id}", (220, 270),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (60, 60, 80), 1)
        return img

    def generate_mjpeg(self):
        """
        Generator stream MJPEG cho endpoint /video_feed.
        FastAPI dùng StreamingResponse với generator này.
        """
        while True:
            frame, _ = self.get_processed_frame()
            _, jpeg  = cv2.imencode('.jpg', frame,
                                    [cv2.IMWRITE_JPEG_QUALITY, 80])
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n'
                   + jpeg.tobytes()
                   + b'\r\n')
            time.sleep(1/25)   # ~25 FPS

    def capture_snapshot(self, emp_code: str) -> str:
        """Chụp và lưu ảnh tại thời điểm chấm công"""
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


# Lazy singleton
_camera_instance = None

def get_camera() -> CameraStream:
    """Chỉ mở camera khi được gọi lần đầu"""
    global _camera_instance
    if _camera_instance is None:
        _camera_instance = CameraStream(camera_id=0)
    return _camera_instance

def release_camera():
    global _camera_instance
    if _camera_instance is not None:
        _camera_instance.release()
        _camera_instance = None
