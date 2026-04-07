import asyncio
from fastapi import WebSocket, WebSocketDisconnect

from app.camera import get_camera
from app.attendance import process_attendance
from app.notify import telegram_checkin


class ConnectionManager:
    """Quan ly nhieu WebSocket client cung luc"""
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        for ws in self.active.copy():
            try:
                await ws.send_json(data)
            except Exception:
                if ws in self.active:
                    self.active.remove(ws)


manager = ConnectionManager()


async def ws_attendance(websocket: WebSocket):
    await manager.connect(websocket)
    loop = asyncio.get_running_loop()   # get_event_loop() deprecated từ Python 3.10
    try:
        while True:
            cam = get_camera()
            if cam is None:
                await websocket.send_json({"type": "camera_off"})
                await asyncio.sleep(2.0)
                continue

            # run_recognition là CPU-bound (AI) → chạy trong thread pool
            frame, results = await loop.run_in_executor(None, cam.run_recognition)

            for r in results:
                if not r["recognized"]:
                    continue

                emp_code   = r["emp_code"]
                confidence = r["similarity"]

                # Lưu snapshot trong executor (cv2.imwrite là blocking IO)
                # Dùng frame đã nhận diện thay vì đọc frame mới
                capture = await loop.run_in_executor(
                    None, cam.capture_snapshot, emp_code, frame
                )

                log = process_attendance(emp_code, confidence, capture)
                if log:
                    await manager.broadcast(log)
                    asyncio.create_task(telegram_checkin(log))

            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
