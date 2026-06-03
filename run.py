"""
run.py
Entry point khởi động server.
"""

import ctypes
import os
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
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    print(f"  Truy cập : http://{host}:{port}")
    print(f"  API Docs : http://{host}:{port}/docs")
    print("=" * 50)

    uvicorn.run(
        "app.main:app",
        host      = host,
        port      = port,
        reload    = True,
        log_level = "info",
    )
