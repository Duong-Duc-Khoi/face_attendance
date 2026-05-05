"""
app/api/v1/ws.py
WebSocket endpoint — nhận diện realtime và broadcast sự kiện chấm công.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import WebSocket, WebSocketDisconnect

from app.services.camera import get_camera
from app.services.attendance import process_attendance
from app.services.notify import notify_late_async

_ai_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="face_ai")


class ConnectionManager:
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
                self.active.discard(ws) if hasattr(self.active, "discard") else None


manager = ConnectionManager()


async def _safe_send(websocket: WebSocket, data: dict) -> bool:
    try:
        await websocket.send_json(data)
        return True
    except Exception:
        return False


async def ws_attendance(websocket: WebSocket):
    await manager.connect(websocket)
    loop = asyncio.get_running_loop()
    try:
        while True:
            cam = get_camera()
            if cam is None:
                alive = await _safe_send(websocket, {"type": "camera_off"})
                if not alive:
                    break
                await asyncio.sleep(2.0)
                continue

            frame, results, emp_map = await loop.run_in_executor(
                _ai_executor, cam.run_recognition
            )

            # Broadcast bbox lên canvas client
            if results:
                fw = frame.shape[1] if frame is not None else 1280
                fh = frame.shape[0] if frame is not None else 720
                faces_payload = [
                    {
                        "bbox":       [fw - r["bbox"][2], r["bbox"][1], fw - r["bbox"][0], r["bbox"][3]],
                        "recognized": r["recognized"],
                        "name":       emp_map.get(r["emp_code"], r["emp_code"]) if r["recognized"] else "",
                        "confidence": r["similarity"],
                    }
                    for r in results
                ]
                await manager.broadcast({"type": "faces", "faces": faces_payload, "fw": fw, "fh": fh})

            # Xử lý chấm công
            for r in (results or []):
                if not r["recognized"]:
                    continue
                emp_code   = r["emp_code"]
                confidence = r["similarity"]

                capture = await loop.run_in_executor(_ai_executor, cam.capture_snapshot, emp_code, frame)

                try:
                    log = await loop.run_in_executor(
                        _ai_executor, process_attendance, emp_code, confidence, capture
                    )
                except Exception as e:
                    print(f"  ✗ process_attendance lỗi [{emp_code}]: {e}")
                    continue

                if log:
                    print(f"  → {log.get('name')} {log.get('check_type')}")
                    await manager.broadcast({**log, "type": "attendance"})

                    status = log.get("status", "")
                    if status and "muộn" in status:
                        minutes_late = int("".join(filter(str.isdigit, status)) or 0)
                        asyncio.create_task(notify_late_async(
                            log["name"], log["emp_code"], log["department"],
                            minutes_late, log.get("email", ""),
                        ))
                else:
                    print(f"  ⚠ Cooldown hoặc lỗi logic [{emp_code}]")

            await asyncio.sleep(1.0)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"  ✗ ws_attendance lỗi: {e}")
    finally:
        manager.disconnect(websocket)
