"""Run the separated dashboard/frontend host."""

import os

import uvicorn


if __name__ == "__main__":
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "5600"))
    backend_url = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
    kiosk_url = os.getenv("KIOSK_URL", "http://127.0.0.1:5500")

    print("=" * 50)
    print("  FaceAttend Web Host")
    print(f"  Web     : http://{host}:{port}")
    print(f"  Backend : {backend_url}")
    print(f"  Kiosk   : {kiosk_url}")
    print("=" * 50)

    uvicorn.run(
        "web_host.server:app",
        host=host,
        port=port,
        reload=True,
        log_level="info",
    )
