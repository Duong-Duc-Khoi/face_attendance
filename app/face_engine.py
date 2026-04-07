import os
import pickle
import cv2
import numpy as np
from numpy.linalg import norm
from datetime import datetime

try:
    import insightface
    from insightface.app import FaceAnalysis
    INSIGHTFACE_AVAILABLE = True
except ImportError:
    INSIGHTFACE_AVAILABLE = False
    print("  ⚠ InsightFace chưa được cài. Chạy: pip install insightface onnxruntime")


EMBEDDINGS_PATH = "data/embeddings.pkl"
FACES_DIR       = "data/faces"
THRESHOLD       = 0.50   # Ngưỡng cosine similarity (0.0 - 1.0)
MIN_FACE_SIZE   = 40     # Bỏ qua khuôn mặt nhỏ hơn 40px


class FaceEngine:
    def __init__(self):
        self.model        = None
        self.embeddings   = {}   # { emp_code: [embedding_array, ...] }
        self.threshold    = THRESHOLD
        self._initialized = False

        # ── Ma trận tìm kiếm nhanh (rebuild khi embeddings thay đổi) ──
        # Mỗi hàng = mean embedding (đã normalize) của 1 nhân viên
        self._emb_matrix: np.ndarray | None = None  # shape: (N, 512)
        self._emb_codes:  list[str]         = []    # tương ứng với từng hàng

        self._init_model()
        self._load_embeddings()

    # ──────────────────────────────────────────
    # Khởi tạo model
    # ──────────────────────────────────────────
    def _init_model(self):
        if not INSIGHTFACE_AVAILABLE:
            print("  ✗ InsightFace không khả dụng — dùng chế độ demo")
            return
        try:
            self.model = FaceAnalysis(
                name="buffalo_l",
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
            )
            self.model.prepare(ctx_id=0, det_size=(640, 640))
            self._initialized = True
            print("  ✓ InsightFace model đã sẵn sàng")
        except Exception as e:
            print(f"  ✗ Lỗi khởi tạo model: {e}")

    # ──────────────────────────────────────────
    # Load / Save embeddings
    # ──────────────────────────────────────────
    def _load_embeddings(self):
        if os.path.exists(EMBEDDINGS_PATH):
            with open(EMBEDDINGS_PATH, "rb") as f:
                self.embeddings = pickle.load(f)
            print(f"  ✓ Đã load {len(self.embeddings)} nhân viên từ embeddings")
        else:
            self.embeddings = {}
        self._rebuild_matrix()

    def _save_embeddings(self):
        with open(EMBEDDINGS_PATH, "wb") as f:
            pickle.dump(self.embeddings, f)

    # ──────────────────────────────────────────
    # Ma trận tìm kiếm (rebuild sau mỗi thay đổi)
    # ──────────────────────────────────────────
    def _rebuild_matrix(self):
        """
        Tính mean embedding (đã normalize) cho mỗi nhân viên
        và xếp thành ma trận (N, 512) để tìm kiếm bằng 1 lần matrix multiply.

        Gọi sau: _load_embeddings(), register(), delete()
        """
        if not self.embeddings:
            self._emb_matrix = None
            self._emb_codes  = []
            return

        codes, vecs = [], []
        for code, embs in self.embeddings.items():
            mean_emb = np.mean(embs, axis=0)
            n = norm(mean_emb)
            if n > 0:
                mean_emb = mean_emb / n   # đảm bảo unit vector
            codes.append(code)
            vecs.append(mean_emb.astype(np.float32))

        self._emb_matrix = np.array(vecs, dtype=np.float32)  # (N, 512)
        self._emb_codes  = codes

    # ──────────────────────────────────────────
    # Đăng ký nhân viên mới
    # ──────────────────────────────────────────
    def register(self, emp_code: str, images: list) -> dict:
        """
        Đăng ký khuôn mặt nhân viên.
        images: list ảnh BGR (numpy array) từ camera.
        Trả về: { success, count, message }
        """
        if not self._initialized:
            # Demo mode: giả lập đăng ký thành công
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
                    w = face.bbox[2] - face.bbox[0]
                    if w >= MIN_FACE_SIZE:
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

        # Lưu ảnh mẫu đại diện
        face_dir = os.path.join(FACES_DIR, emp_code)
        os.makedirs(face_dir, exist_ok=True)
        for i, img in enumerate(images[:5]):
            cv2.imwrite(os.path.join(face_dir, f"{i}.jpg"), img)

        return {
            "success": True,
            "count":   len(embeddings),
            "message": f"Đã đăng ký {len(embeddings)} ảnh khuôn mặt"
        }

    def delete(self, emp_code: str):
        """Xóa embedding khi xóa nhân viên"""
        if emp_code in self.embeddings:
            del self.embeddings[emp_code]
            self._save_embeddings()
            self._rebuild_matrix()

    # ──────────────────────────────────────────
    # Nhận diện từ frame camera
    # ──────────────────────────────────────────
    def recognize(self, frame: np.ndarray) -> list:
        """
        Nhận diện khuôn mặt trong 1 frame.
        Trả về list kết quả cho mỗi khuôn mặt phát hiện được.
        """
        if not self._initialized:
            return []

        try:
            faces = self.model.get(frame)
        except Exception:
            return []

        results = []
        for face in faces:
            x1, y1, x2, y2 = face.bbox.astype(int)
            w = x2 - x1
            if w < MIN_FACE_SIZE:
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
        """
        Tìm nhân viên khớp nhất bằng 1 lần matrix multiply thay vì Python loop.

        embedding: unit vector float32 (512,)
        _emb_matrix: (N, 512) — mỗi hàng là mean embedding (unit vector) của 1 nhân viên

        cosine_similarity = dot(q, e) khi cả 2 đã normalize → không cần chia norm.
        """
        if self._emb_matrix is None or len(self._emb_codes) == 0:
            return "Unknown", 0.0

        # (N,) — cosine similarity với tất cả nhân viên trong 1 phép nhân
        sims = self._emb_matrix @ embedding

        idx = int(np.argmax(sims))
        return self._emb_codes[idx], float(sims[idx])

    # ──────────────────────────────────────────
    # Vẽ kết quả lên frame
    # ──────────────────────────────────────────
    def draw_results(self, frame: np.ndarray, results: list,
                     employee_map: dict = None) -> np.ndarray:
        """Vẽ bounding box + tên lên frame."""
        for r in results:
            x1, y1, x2, y2 = r["bbox"]
            recognized      = r["recognized"]
            emp_code        = r["emp_code"]
            sim             = r["similarity"]

            color  = (0, 200, 100) if recognized else (60, 60, 220)
            label  = employee_map.get(emp_code, emp_code) if (recognized and employee_map) else emp_code
            sublbl = f"{sim:.0%}"

            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.65, 2)
            cv2.rectangle(frame, (x1, y1 - th - 14), (x1 + tw + 12, y1), color, -1)

            cv2.putText(frame, label,  (x1 + 6, y1 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255,255,255), 2, cv2.LINE_AA)
            cv2.putText(frame, sublbl, (x1 + 6, y2 + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.48, color, 1, cv2.LINE_AA)
        return frame

    @property
    def registered_count(self) -> int:
        return len(self.embeddings)


# Singleton — dùng chung toàn app
face_engine = FaceEngine()
