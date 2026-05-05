"""
app/services/face_engine.py
FaceEngine — nhận diện khuôn mặt qua InsightFace.

Thay đổi so với code cũ:
  - Font path Windows hardcode → dùng font mặc định, log cảnh báo 1 lần
  - EMBEDDINGS_PATH, FACES_DIR lấy từ settings thay vì hardcode string
  - Không còn hằng số rải rác ở đầu file — tất cả vào settings
"""

import os
import pickle

import cv2
import numpy as np
from numpy.linalg import norm

from app.core.config import settings

# ── PIL (optional, hỗ trợ tiếng Việt) ──────────────────────────
try:
    from PIL import Image, ImageDraw, ImageFont
    # Tìm font mặc định — không hardcode đường dẫn Windows
    _font_name = _font_conf = None
    for _path in [
        r"C:\Windows\Fonts\arial.ttf",          # Windows
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
        "/System/Library/Fonts/Helvetica.ttc",   # macOS
    ]:
        if os.path.exists(_path):
            _font_name = ImageFont.truetype(_path, 15)
            _font_conf = ImageFont.truetype(_path, 12)
            break
    PIL_AVAILABLE = _font_name is not None
    if not PIL_AVAILABLE:
        print("  ⚠ PIL: không tìm thấy font phù hợp — text sẽ dùng OpenCV")
except ImportError:
    PIL_AVAILABLE = False


def _put_text_pil(frame, text, pos, font, bg_color, text_color=(255, 255, 255)):
    """Vẽ text Unicode lên frame qua PIL (hỗ trợ tiếng Việt)."""
    img_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw    = ImageDraw.Draw(img_pil)
    bbox    = draw.textbbox(pos, text, font=font)
    pad     = 4
    draw.rectangle((bbox[0]-pad, bbox[1]-pad, bbox[2]+pad, bbox[3]+pad), fill=bg_color)
    draw.text(pos, text, font=font, fill=text_color)
    frame[:] = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)


# ── InsightFace (optional) ───────────────────────────────────────
try:
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=FutureWarning, module="insightface")
        from insightface.app import FaceAnalysis
    INSIGHTFACE_AVAILABLE = True
except ImportError:
    INSIGHTFACE_AVAILABLE = False
    print("  ⚠ InsightFace chưa cài. Chạy: pip install insightface onnxruntime")


class FaceEngine:
    def __init__(self):
        self.model        = None
        self.embeddings   = {}     # { emp_code: [embedding, ...] }
        self.threshold    = settings.FACE_THRESHOLD
        self._initialized = False

        # Ma trận tìm kiếm nhanh O(1) thay vì Python loop
        self._emb_matrix: np.ndarray | None = None  # (N, 512)
        self._emb_codes:  list[str]          = []

        self._init_model()
        self._load_embeddings()

    # ── Khởi tạo model ──────────────────────────────────────────
    def _init_model(self):
        if not INSIGHTFACE_AVAILABLE:
            print("  ✗ InsightFace không khả dụng — chạy demo mode")
            return
        try:
            self.model = FaceAnalysis(
                name="buffalo_l",
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            )
            self.model.prepare(ctx_id=0, det_size=(640, 640))
            self._initialized = True
            print("  ✓ InsightFace model sẵn sàng")
        except Exception as e:
            print(f"  ✗ Lỗi khởi tạo model: {e}")

    # ── Load / Save embeddings ───────────────────────────────────
    def _load_embeddings(self):
        path = settings.EMBEDDINGS_PATH
        if path.exists():
            with open(path, "rb") as f:
                self.embeddings = pickle.load(f)
            print(f"  ✓ Đã load {len(self.embeddings)} nhân viên từ embeddings")
        else:
            self.embeddings = {}
        self._rebuild_matrix()

    def _save_embeddings(self):
        with open(settings.EMBEDDINGS_PATH, "wb") as f:
            pickle.dump(self.embeddings, f)

    # ── Ma trận tìm kiếm ────────────────────────────────────────
    def _rebuild_matrix(self):
        """Rebuild sau mỗi register() / delete() để giữ matrix đồng bộ."""
        if not self.embeddings:
            self._emb_matrix = None
            self._emb_codes  = []
            return
        codes, vecs = [], []
        for code, embs in self.embeddings.items():
            mean_emb = np.mean(embs, axis=0)
            n = norm(mean_emb)
            if n > 0:
                mean_emb = mean_emb / n
            codes.append(code)
            vecs.append(mean_emb.astype(np.float32))
        self._emb_matrix = np.array(vecs, dtype=np.float32)
        self._emb_codes  = codes

    # ── Đăng ký ─────────────────────────────────────────────────
    def register(self, emp_code: str, images: list) -> dict:
        if not self._initialized:
            # Demo mode
            self.embeddings[emp_code] = [np.random.rand(512).astype(np.float32)]
            self._save_embeddings()
            self._rebuild_matrix()
            return {"success": True, "count": 1, "message": "Demo mode — đăng ký ảo"}

        embeddings = []
        for img in images:
            try:
                faces = self.model.get(img)
                if faces:
                    face = max(faces, key=lambda f: (f.bbox[2]-f.bbox[0]) * (f.bbox[3]-f.bbox[1]))
                    if (face.bbox[2] - face.bbox[0]) >= settings.MIN_FACE_SIZE:
                        emb = face.embedding
                        n   = norm(emb)
                        if n > 0:
                            embeddings.append((emb / n).astype(np.float32))
            except Exception:
                continue

        if not embeddings:
            return {"success": False, "count": 0, "message": "Không phát hiện khuôn mặt trong ảnh"}

        self.embeddings[emp_code] = embeddings
        self._save_embeddings()
        self._rebuild_matrix()

        # Lưu ảnh mẫu
        face_dir = settings.FACES_DIR / emp_code
        face_dir.mkdir(parents=True, exist_ok=True)
        for i, img in enumerate(images[:5]):
            cv2.imwrite(str(face_dir / f"{i}.jpg"), img)

        return {"success": True, "count": len(embeddings), "message": f"Đã đăng ký {len(embeddings)} ảnh"}

    def delete(self, emp_code: str):
        if emp_code in self.embeddings:
            del self.embeddings[emp_code]
            self._save_embeddings()
            self._rebuild_matrix()

    # ── Nhận diện ───────────────────────────────────────────────
    def recognize(self, frame: np.ndarray) -> list:
        if not self._initialized:
            return []
        try:
            faces = self.model.get(frame)
        except Exception:
            return []

        results = []
        for face in faces:
            x1, y1, x2, y2 = face.bbox.astype(int)
            if (x2 - x1) < settings.MIN_FACE_SIZE:
                continue
            emb = face.embedding
            n   = norm(emb)
            if n == 0:
                continue
            emb = (emb / n).astype(np.float32)
            emp_code, similarity = self._match(emb)
            results.append({
                "bbox":       [x1, y1, x2, y2],
                "emp_code":   emp_code,
                "similarity": round(float(similarity), 4),
                "recognized": similarity >= self.threshold,
                "landmarks":  face.kps.tolist() if face.kps is not None else [],
            })
        return results

    def _match(self, embedding: np.ndarray) -> tuple[str, float]:
        if self._emb_matrix is None or not self._emb_codes:
            return "Unknown", 0.0
        sims = self._emb_matrix @ embedding
        idx  = int(np.argmax(sims))
        return self._emb_codes[idx], float(sims[idx])

    # ── Vẽ kết quả ──────────────────────────────────────────────
    def draw_results(self, frame: np.ndarray, results: list, employee_map: dict = None) -> np.ndarray:
        for r in results:
            x1, y1, x2, y2 = r["bbox"]
            recognized = r["recognized"]
            color_bgr  = (0, 200, 100) if recognized else (60, 60, 220)
            label  = (employee_map.get(r["emp_code"]) or r["emp_code"]) if recognized and employee_map else "Unknown"
            sublbl = f"{r['similarity']:.0%}"
            cv2.rectangle(frame, (x1, y1), (x2, y2), color_bgr, 2)
            if PIL_AVAILABLE:
                color_pil = (color_bgr[2], color_bgr[1], color_bgr[0])
                _put_text_pil(frame, label,  (x1+4, y1-22), _font_name, color_pil)
                _put_text_pil(frame, sublbl, (x1+4, y2+4),  _font_conf, color_pil)
            else:
                cv2.putText(frame, label,  (x1+6, y1-6),  cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255,255,255), 2, cv2.LINE_AA)
                cv2.putText(frame, sublbl, (x1+6, y2+18), cv2.FONT_HERSHEY_SIMPLEX, 0.48, color_bgr,     1, cv2.LINE_AA)
        return frame

    @property
    def registered_count(self) -> int:
        return len(self.embeddings)


# Singleton — dùng chung toàn app
face_engine = FaceEngine()
