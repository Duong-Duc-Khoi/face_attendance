import asyncio
from fastapi import WebSocket, WebSocketDisconnect

from app.camera import get_camera
from app.attendance import process_attendance
from app.notify import notify_late_async


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
            frame, results, emp_map = await loop.run_in_executor(None, cam.run_recognition)

            # Broadcast bbox overlay data cho client vẽ lên video stream
            if results is not None:
                faces_payload = []
                for r in results:
                    faces_payload.append({
                        "bbox":       r["bbox"],
                        "recognized": r["recognized"],
                        "name":       emp_map.get(r["emp_code"], r["emp_code"]) if r["recognized"] else "",
                        "confidence": r["similarity"],
                    })
                # Gửi kèm kích thước frame để client scale đúng tọa độ bbox
                fw = frame.shape[1] if frame is not None else 1280
                fh = frame.shape[0] if frame is not None else 720
                await manager.broadcast({
                    "type": "faces", "faces": faces_payload,
                    "fw": fw, "fh": fh,
                })

            for r in (results or []):
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
                    status = log.get("status", "")
                    if status and "muộn" in status:
                        minutes_late = int("".join(filter(str.isdigit, status)) or 0)
                        asyncio.create_task(notify_late_async(
                            log["name"], log["emp_code"], log["department"],
                            minutes_late, log.get("email", ""),
                        ))

            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
