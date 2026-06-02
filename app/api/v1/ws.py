"""
Realtime attendance WebSocket endpoints.

Two modes are supported:
  - /ws/attendance: legacy mode, backend reads a local webcam with OpenCV.
  - /ws/kiosk: split-host mode, kiosk browser sends camera frames to backend.
"""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.attendance import process_attendance
from app.services.attendance_audit import record_attendance_evidence
from app.services.camera import get_camera, save_capture_snapshot
from app.services.face_engine import face_engine
from app.services.notify import notify_late_async
from app.services.presentation_guard import presentation_guard_service

_ai_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="face_ai")
PRESENCE_RESET_SECONDS = 3.0
KIOSK_FRAME_INTERVAL_SECONDS = 0.65

router = APIRouter(tags=["realtime"])


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
                if ws in self.active:
                    self.active.remove(ws)


manager = ConnectionManager()


async def _safe_send(websocket: WebSocket, data: dict) -> bool:
    try:
        await websocket.send_json(data)
        return True
    except Exception:
        return False


def _decode_data_url_frame(raw: str) -> np.ndarray | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        encoded = raw.split(",", 1)[1] if "," in raw else raw
        img_bytes = base64.b64decode(encoded, validate=False)
        arr = np.frombuffer(img_bytes, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


async def _notify_late_if_needed(log: dict) -> None:
    status = log.get("status", "")
    if status and "muộn" in status:
        minutes_late = int("".join(filter(str.isdigit, status)) or 0)
        asyncio.create_task(notify_late_async(
            log["name"], log["emp_code"], log["department"],
            minutes_late, log.get("email", ""),
        ))


async def _handle_face_result(
    *,
    websocket: WebSocket,
    frame: np.ndarray,
    result: dict,
    processed_until_absent: set[str],
    last_seen_by_emp: dict[str, float],
    now_seen: float,
    broadcast: bool,
    capture_func,
    branch_id: int | None = None,
) -> None:
    if not result.get("recognized"):
        return

    emp_code = result.get("emp_code") or ""
    confidence = float(result.get("similarity") or 0.0)
    if not emp_code or emp_code in processed_until_absent:
        return

    last_seen_by_emp[emp_code] = now_seen
    presentation = presentation_guard_service.check(frame, result, emp_code, now_seen)
    if presentation.get("enabled") and presentation["reason"] == "collecting_frames":
        return

    if presentation.get("enabled") and presentation["reason"] != "clear":
        print(
            f"  ⚠ Presentation guard {presentation['action']} [{emp_code}]: "
            f"{presentation['reason']} risk={presentation['risk']} metrics={presentation['metrics']}"
        )

    if presentation.get("should_block"):
        payload = {
            "type": "attendance_error",
            "ok": False,
            "reason": "presentation_attack",
            "emp_code": emp_code,
            "confidence": round(confidence, 4),
            "presentation_guard": presentation,
            "message": "Phát hiện ảnh hoặc màn hình gần khuôn mặt",
            "voice_message": "Không thể chấm công. Vui lòng đứng trực tiếp trước camera.",
        }
        if broadcast:
            await manager.broadcast(payload)
        else:
            await websocket.send_json(payload)
        processed_until_absent.add(emp_code)
        presentation_guard_service.reset(emp_code)
        return

    loop = asyncio.get_running_loop()
    try:
        log = await loop.run_in_executor(_ai_executor, process_attendance, emp_code, confidence, "", branch_id)
    except Exception as exc:
        print(f"  ✗ process_attendance lỗi [{emp_code}]: {exc}")
        return

    if log and log.get("ok", True):
        capture = await loop.run_in_executor(
            _ai_executor,
            capture_func,
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
        if capture:
            async def _update(
                lid=log["id"],
                cap=capture,
                eid=log.get("event_id"),
                pres=presentation,
            ):
                await loop.run_in_executor(_ai_executor, record_attendance_evidence, lid, cap, eid, pres)
            asyncio.create_task(_update())

        payload = {**log, "type": "attendance"}
        print(f"  → {log.get('name')} {log.get('check_type')}")
        if broadcast:
            await manager.broadcast(payload)
        else:
            await websocket.send_json(payload)
        processed_until_absent.add(emp_code)
        presentation_guard_service.reset(emp_code)
        await _notify_late_if_needed(log)
    elif log:
        payload = {**log, "type": "attendance_error"}
        print(f"  ⚠ {log.get('message', 'Không chấm công')} [{emp_code}]")
        if broadcast:
            await manager.broadcast(payload)
        else:
            await websocket.send_json(payload)
        processed_until_absent.add(emp_code)
        presentation_guard_service.reset(emp_code)
    else:
        processed_until_absent.add(emp_code)
        presentation_guard_service.reset(emp_code)


async def ws_attendance(websocket: WebSocket):
    """Legacy mode: backend machine owns the webcam."""
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

            frame, results, _emp_map = await loop.run_in_executor(_ai_executor, cam.run_recognition)
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

            for result in results or []:
                await _handle_face_result(
                    websocket=websocket,
                    frame=frame,
                    result=result,
                    processed_until_absent=processed_until_absent,
                    last_seen_by_emp=last_seen_by_emp,
                    now_seen=now_seen,
                    broadcast=True,
                    capture_func=cam.capture_snapshot,
                )
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"  ✗ ws_attendance lỗi: {exc}")
    finally:
        manager.disconnect(websocket)


async def ws_kiosk(websocket: WebSocket):
    """Split-host mode: kiosk browser sends webcam frames to backend."""
    await websocket.accept()
    branch_id = None
    raw_branch_id = websocket.query_params.get("branch_id")
    if raw_branch_id:
        try:
            branch_id = int(raw_branch_id)
        except ValueError:
            await websocket.send_json({"type": "frame_error", "message": "branch_id không hợp lệ"})
            await websocket.close()
            return
    loop = asyncio.get_running_loop()
    processed_until_absent: set[str] = set()
    last_seen_by_emp: dict[str, float] = {}
    last_processed_at = 0.0
    try:
        await websocket.send_json({"type": "kiosk_ready"})
        while True:
            payload = await websocket.receive_json()
            if payload.get("type") != "frame":
                continue

            now_seen = loop.time()
            if now_seen - last_processed_at < KIOSK_FRAME_INTERVAL_SECONDS:
                continue
            last_processed_at = now_seen

            frame = await loop.run_in_executor(
                _ai_executor,
                _decode_data_url_frame,
                payload.get("image", ""),
            )
            if frame is None:
                await websocket.send_json({"type": "frame_error", "message": "Frame không hợp lệ"})
                continue

            results = await loop.run_in_executor(_ai_executor, face_engine.recognize, frame)
            recognized_codes = {
                r.get("emp_code") for r in (results or [])
                if r.get("recognized") and r.get("emp_code")
            }
            for emp_code in recognized_codes:
                last_seen_by_emp[emp_code] = now_seen
            for emp_code in list(processed_until_absent):
                if now_seen - last_seen_by_emp.get(emp_code, 0) >= PRESENCE_RESET_SECONDS:
                    processed_until_absent.remove(emp_code)
                    last_seen_by_emp.pop(emp_code, None)

            await websocket.send_json({
                "type": "faces",
                "faces": len(results or []),
                "recognized": len(recognized_codes),
            })
            for result in results or []:
                await _handle_face_result(
                    websocket=websocket,
                    frame=frame,
                    result=result,
                    processed_until_absent=processed_until_absent,
                    last_seen_by_emp=last_seen_by_emp,
                    now_seen=now_seen,
                    broadcast=False,
                    capture_func=lambda emp, frm, meta: save_capture_snapshot(frm, emp, meta),
                    branch_id=branch_id,
                )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"  ✗ ws_kiosk lỗi: {exc}")


@router.websocket("/ws/attendance")
async def ws_attendance_route(websocket: WebSocket):
    await ws_attendance(websocket)


@router.websocket("/ws/kiosk")
async def ws_kiosk_route(websocket: WebSocket):
    await ws_kiosk(websocket)
