import uvicorn
import os
import ctypes

if __name__ == "__main__":
    # Nâng độ phân giải timer Windows từ 15.6ms → 1ms
    # Giúp time.sleep() trong MJPEG stream chính xác hơn, giảm jitter
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
    # Tạo thư mục cần thiết nếu chưa có
    os.makedirs("data/faces", exist_ok=True)
    os.makedirs("data/captures", exist_ok=True)
    os.makedirs("data/exports", exist_ok=True)

    print("=" * 50)
    print("  FaceAttend System - Khởi động...")
    print("  Truy cập: http://localhost:8000")
    print("  API Docs: http://localhost:8000/docs")
    print("=" * 50)

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
