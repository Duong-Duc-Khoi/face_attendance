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
                self.active.discard(ws) if hasattr(self.active, 'discard') else None
                if ws in self.active:
                    self.active.remove(ws)


manager = ConnectionManager()


async def ws_attendance(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            cam = get_camera()
            if cam is None:
                await websocket.send_json({"type": "camera_off"})
                await asyncio.sleep(2.0)
                continue

            _, results = await asyncio.get_event_loop().run_in_executor(
                None, cam.run_recognition
            )

            for r in results:
                if not r["recognized"]:
                    continue
                emp_code   = r["emp_code"]
                confidence = r["similarity"]
                capture    = cam.capture_snapshot(emp_code)
                log        = process_attendance(emp_code, confidence, capture)
                if log:
                    await manager.broadcast(log)
                    asyncio.create_task(telegram_checkin(log))

            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
