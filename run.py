"""
run.py
Entry point khởi động server.
"""

import ctypes
import uvicorn
from app.core.config import settings

if __name__ == "__main__":
    # Nâng độ phân giải timer Windows từ 15.6ms → 1ms (giảm jitter MJPEG)
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

    # Tạo thư mục cần thiết
    for d in [settings.FACES_DIR, settings.CAPTURES_DIR, settings.EXPORTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    print("=" * 50)
    print("  FaceAttend System — Khởi động...")
    print(f"  Truy cập : http://{settings.HOST}:{settings.PORT}")
    print(f"  API Docs : http://{settings.HOST}:{settings.PORT}/docs")
    print("=" * 50)

    uvicorn.run(
        "app.main:app",
        host      = settings.HOST,
        port      = settings.PORT,
        reload    = True,
        log_level = "info",
    )
