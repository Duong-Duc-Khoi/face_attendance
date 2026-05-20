"""
app/api/v1/ws.py
WebSocket endpoint — nhận diện realtime và broadcast sự kiện chấm công.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import WebSocket, WebSocketDisconnect

from app.services.camera import get_camera
from app.services.attendance import process_attendance,update_capture_path
from app.services.notify import notify_late_async
from app.services.presentation_guard import presentation_guard_service

_ai_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="face_ai")
PRESENCE_RESET_SECONDS = 3.0


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
                self.active.remove(ws) if hasattr(self.active, "discard") else None


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
    processed_until_absent: set[str] = set()
    last_seen_by_emp: dict[str, float] = {}
    try:
        while True:
            cam = get_camera()
            if cam is None:
                alive = await _safe_send(websocket, {"type": "camera_off"})
                if not alive:
                    break
                await asyncio.sleep(2.0)
                continue

            frame, results, _emp_map = await loop.run_in_executor(
                _ai_executor, cam.run_recognition
            )

            now_seen = loop.time()
            recognized_codes = {
                r["emp_code"] for r in (results or [])
                if r.get("recognized") and r.get("emp_code")
            }
            for emp_code in recognized_codes:
                last_seen_by_emp[emp_code] = now_seen
            for emp_code in list(processed_until_absent):
                if now_seen - last_seen_by_emp.get(emp_code, 0) >= PRESENCE_RESET_SECONDS:
                    processed_until_absent.remove(emp_code)
                    last_seen_by_emp.pop(emp_code, None)

            # Xử lý chấm công
            for r in (results or []):
                if not r["recognized"]:
                    continue
                emp_code   = r["emp_code"]
                confidence = r["similarity"]
                if emp_code in processed_until_absent:
                    continue

                presentation = presentation_guard_service.check(frame, r, emp_code, now_seen)
                if presentation.get("enabled") and presentation["reason"] == "collecting_frames":
                    print(
                        f"  … Presentation guard [{emp_code}]: "
                        f"đang lấy mẫu {presentation['metrics']['frames']}/{presentation['metrics']['needed_frames']}"
                    )
                    continue
                if presentation.get("enabled") and presentation["reason"] != "clear":
                    print(
                        f"  ⚠ Presentation guard {presentation['action']} [{emp_code}]: "
                        f"{presentation['reason']} risk={presentation['risk']} metrics={presentation['metrics']}"
                    )
                if presentation.get("should_block"):
                    await manager.broadcast({
                        "type": "attendance_error",
                        "ok": False,
                        "reason": "presentation_attack",
                        "emp_code": emp_code,
                        "confidence": round(confidence, 4),
                        "presentation_guard": presentation,
                        "message": "Phát hiện ảnh hoặc màn hình gần khuôn mặt",
                        "voice_message": "Không thể chấm công. Vui lòng đứng trực tiếp trước camera.",
                    })
                    processed_until_absent.add(emp_code)
                    presentation_guard_service.reset(emp_code)
                    continue
               
                try:
                    log = await loop.run_in_executor(
                        _ai_executor, process_attendance, emp_code, confidence
                    )
                except Exception as e:
                    print(f"  ✗ process_attendance lỗi [{emp_code}]: {e}")
                    continue

                if log and log.get("ok", True):
                    capture = await loop.run_in_executor(
                        _ai_executor,
                        cam.capture_snapshot,
                        emp_code,
                        frame,
                        {
                            "log_id": log.get("id"),
                            "emp_code": emp_code,
                            "name": log.get("name", ""),
                            "check_type": log.get("check_type", ""),
                            "timestamp": log.get("timestamp", ""),
                            "confidence": confidence,
                        },
                    )
                    # Update path vào DB (non-blocking, không cần await kết quả)
                    if capture:
                        async def _update(
                            lid=log["id"],
                            cp=capture.get("path", ""),
                            eid=log.get("event_id"),
                            ih=capture.get("image_hash", ""),
                        ):
                            await loop.run_in_executor(_ai_executor, update_capture_path, lid, cp, eid, ih)
                        asyncio.create_task(_update())
                    print(f"  → {log.get('name')} {log.get('check_type')}")
                    await manager.broadcast({**log, "type": "attendance"})
                    processed_until_absent.add(emp_code)
                    presentation_guard_service.reset(emp_code)

                    status = log.get("status", "")
                    if status and "muộn" in status:
                        minutes_late = int("".join(filter(str.isdigit, status)) or 0)
                        asyncio.create_task(notify_late_async(
                            log["name"], log["emp_code"], log["department"],
                            minutes_late, log.get("email", ""),
                        ))
                elif log:
                    print(f"  ⚠ {log.get('message', 'Không chấm công')} [{emp_code}]")
                    await manager.broadcast({**log, "type": "attendance_error"})
                    processed_until_absent.add(emp_code)
                    presentation_guard_service.reset(emp_code)
                else:
                    print(f"  ⚠ Cooldown hoặc lỗi logic [{emp_code}]")
                    processed_until_absent.add(emp_code)
                    presentation_guard_service.reset(emp_code)

            await asyncio.sleep(1.0)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"  ✗ ws_attendance lỗi: {e}")
    finally:
        manager.disconnect(websocket)
