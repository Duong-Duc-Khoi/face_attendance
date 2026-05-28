"""
Mobile attendance flow.

The browser provides camera frames and GPS, but the server owns the timestamp,
geofence decision, face match decision, and final attendance write.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime
from typing import Any

import cv2
import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.attendance import AttendanceAttempt
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.user import User
from app.services.attendance import process_attendance
from app.services.attendance_audit import record_attendance_evidence
from app.services.camera import CameraStream
from app.services.face_engine import face_engine
from app.services.mobile_attendance_policy import evaluate_mobile_policy
from app.services.shift_service import find_shift_assignment_for_time, get_shift_for_employee


def employee_for_user(db: Session, user: User) -> Employee | None:
    emp = db.query(Employee).filter_by(user_id=user.id, is_active=True).first()
    if emp:
        return emp
    return (
        db.query(Employee)
        .filter(Employee.email == user.email, Employee.is_active == True)
        .order_by(Employee.id.asc())
        .first()
    )


def mobile_context(db: Session, user: User) -> dict:
    now = datetime.now()
    emp = employee_for_user(db, user)
    branch = db.query(Branch).filter_by(id=emp.branch_id).first() if emp and emp.branch_id else None
    if emp:
        assignment, active_shift = find_shift_assignment_for_time(emp.emp_code, now, db)
        if assignment and active_shift:
            shift = {
                "source": "active_window",
                "shift_id": active_shift.id,
                "shift_name": active_shift.name,
                "shift_code": active_shift.code,
                "work_start": active_shift.work_start,
                "work_end": active_shift.work_end,
                "late_threshold_minutes": active_shift.late_threshold_minutes,
                "shifts": [{
                    "assignment_id": assignment.id,
                    "shift_id": active_shift.id,
                    "shift_name": active_shift.name,
                    "shift_code": active_shift.code,
                    "work_start": active_shift.work_start,
                    "work_end": active_shift.work_end,
                    "late_threshold_minutes": active_shift.late_threshold_minutes,
                    "note": assignment.note or "",
                }],
            }
        else:
            shift = get_shift_for_employee(emp.emp_code, date.today(), db)
    else:
        shift = {
        "source": "none",
        "shift_id": None,
        "shift_name": "",
        "shift_code": "",
        "work_start": "",
        "work_end": "",
        "late_threshold_minutes": 0,
        "shifts": [],
        }
    return {
        "server_time": now.isoformat(),
        "policy": {
            "enabled": settings.MOBILE_ATTENDANCE_ENABLED,
            "gps_max_accuracy_m": settings.MOBILE_GPS_MAX_ACCURACY_M,
            "face_threshold": settings.MOBILE_FACE_THRESHOLD,
            "capture_frames": settings.MOBILE_CAPTURE_FRAMES,
        },
        "employee": _employee_dict(emp) if emp else None,
        "branch": _branch_dict(branch) if branch else None,
        "shift": shift,
    }


def process_mobile_attempt(db: Session, user: User, body: dict, request_meta: dict) -> dict:
    now = datetime.now()
    emp = employee_for_user(db, user)
    branch_id = _int_or_none(body.get("branch_id")) or (emp.branch_id if emp else None)
    branch = db.query(Branch).filter_by(id=branch_id).first() if branch_id else None

    lat = _float_or_none(body.get("latitude"))
    lon = _float_or_none(body.get("longitude"))
    accuracy_m = _float_or_none(body.get("accuracy_m"))
    device_id = _safe_device_id(body.get("device_id"))
    policy = evaluate_mobile_policy(
        emp=emp,
        branch=branch,
        latitude=lat,
        longitude=lon,
        accuracy_m=accuracy_m,
        user_agent=request_meta.get("user_agent", ""),
        client_hint_mobile=request_meta.get("sec_ch_ua_mobile", ""),
    )

    attempt = AttendanceAttempt(
        user_id=user.id,
        employee_id=emp.id if emp else None,
        emp_code=emp.emp_code if emp else "",
        branch_id=branch.id if branch else branch_id,
        status="blocked",
        server_time=now,
        latitude=lat,
        longitude=lon,
        accuracy_m=accuracy_m,
        distance_m=policy.distance_m,
        device_id=device_id,
        device_kind=policy.device.kind,
        device_is_mobile=policy.device.is_mobile,
        device_reason=policy.device.reason,
        policy_snapshot=json.dumps(policy.policy_snapshot, ensure_ascii=False),
        ip_hash=_hash_value(request_meta.get("ip", "")),
        user_agent_hash=_hash_value(request_meta.get("user_agent", "")),
    )
    db.add(attempt)
    db.flush()
    db.commit()
    db.refresh(attempt)

    try:
        if not policy.allowed:
            return _finish_attempt(
                db,
                attempt,
                ok=False,
                reason=policy.reason,
                message=policy.message,
                risk_reasons=policy.risk_reasons,
            )

        if emp:
            assignment, shift = find_shift_assignment_for_time(emp.emp_code, now, db)
            if not assignment or not shift:
                return _finish_attempt(
                    db,
                    attempt,
                    ok=False,
                    reason="no_active_shift_assignment",
                    message="Bạn chưa có ca hợp lệ tại thời điểm này",
                )

        frames = _decode_frames(body.get("frames") or [])
        if not frames:
            return _finish_attempt(db, attempt, ok=False, reason="no_valid_frame", message="Không nhận được ảnh camera hợp lệ")

        face = _best_face_match(frames, emp.emp_code)
        attempt.face_confidence = round(face["confidence"], 4)
        attempt.face_emp_code = face.get("emp_code") or ""

        if not face["matched"]:
            reason = "face_mismatch" if face.get("emp_code") and face.get("emp_code") != "Unknown" else "face_not_verified"
            capture = _save_mobile_capture(
                frame=face["frame"] if face.get("frame") is not None else frames[0],
                emp_code=emp.emp_code,
                metadata={
                    "log_id": f"attempt-{attempt.id}",
                    "name": emp.name,
                    "check_type": "check_in",
                    "timestamp": now.isoformat(),
                    "confidence": face["confidence"],
                },
            )
            _apply_capture(attempt, capture)
            return _finish_attempt(
                db,
                attempt,
                ok=False,
                reason=reason,
                message="Không xác minh được đúng khuôn mặt nhân viên",
                risk_reasons=[reason],
            )

        result = process_attendance(
            emp.emp_code,
            face["confidence"],
            source="mobile_hybrid",
            device_id=device_id,
            require_shift=True,
        )
        if not result or not result.get("ok"):
            return _finish_attempt(
                db,
                attempt,
                ok=False,
                reason=(result or {}).get("reason") or "attendance_rejected",
                message=(result or {}).get("message") or "Không thể ghi nhận chấm công",
                extra=result or {},
            )

        capture = _save_mobile_capture(
            frame=face["frame"] if face.get("frame") is not None else frames[0],
            emp_code=emp.emp_code,
            metadata={
                "log_id": result.get("id"),
                "emp_code": emp.emp_code,
                "name": result.get("name", emp.name),
                "check_type": result.get("check_type", ""),
                "timestamp": result.get("timestamp") or now.isoformat(),
                "confidence": face["confidence"],
            },
        )
        if capture and result.get("id"):
            record_attendance_evidence(result["id"], capture, result.get("event_id"))
        _apply_capture(attempt, capture)

        attempt.status = "success"
        attempt.check_type = result.get("check_type") or ""
        attempt.log_id = result.get("id")
        attempt.event_id = result.get("event_id")
        attempt.session_id = result.get("session_id")
        attempt.message = "Chấm công thành công"
        attempt.risk_reasons = "[]"
        db.commit()

        return _response(True, attempt, "success", "Chấm công thành công", result)
    except Exception as exc:
        db.rollback()
        attempt = db.query(AttendanceAttempt).filter_by(id=attempt.id).first()
        if attempt:
            attempt.status = "error"
            attempt.block_reason = "server_error"
            attempt.message = "Có lỗi khi xử lý chấm công mobile"
            attempt.risk_reasons = json.dumps(["server_error"], ensure_ascii=False)
            db.commit()
            return _response(False, attempt, "server_error", attempt.message, {"error": str(exc)[:160]})
        raise


def _best_face_match(frames: list[np.ndarray], expected_emp_code: str) -> dict:
    best_any = {"confidence": 0.0, "emp_code": "", "matched": False, "frame": frames[0] if frames else None}
    best_expected = {"confidence": 0.0, "emp_code": expected_emp_code, "matched": False, "frame": frames[0] if frames else None}
    threshold = settings.MOBILE_FACE_THRESHOLD

    for frame in frames[: max(1, min(settings.MOBILE_CAPTURE_FRAMES, 5))]:
        for result in face_engine.recognize(frame) or []:
            confidence = float(result.get("similarity") or 0.0)
            emp_code = result.get("emp_code") or ""
            if confidence > best_any["confidence"]:
                best_any = {"confidence": confidence, "emp_code": emp_code, "matched": False, "frame": frame}
            if emp_code == expected_emp_code and confidence > best_expected["confidence"]:
                best_expected = {"confidence": confidence, "emp_code": emp_code, "matched": confidence >= threshold, "frame": frame}

    if best_expected["confidence"] >= threshold:
        best_expected["matched"] = True
        return best_expected
    return best_any


def _decode_frames(raw_frames: list[Any]) -> list[np.ndarray]:
    frames: list[np.ndarray] = []
    for raw in raw_frames[:5]:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            encoded = raw.split(",", 1)[1] if "," in raw else raw
            img_bytes = base64.b64decode(encoded, validate=False)
            arr = np.frombuffer(img_bytes, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                frames.append(img)
        except Exception:
            continue
    return frames


def _save_mobile_capture(frame: np.ndarray, emp_code: str, metadata: dict) -> dict:
    captured_at = _metadata_datetime(metadata)
    day_dir = settings.CAPTURES_DIR / "mobile" / captured_at.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    log_id = _safe_name(metadata.get("log_id") or metadata.get("id") or "attempt")
    safe_emp = _safe_name(emp_code)
    safe_type = _safe_name(metadata.get("check_type") or "mobile")
    path = day_dir / f"{log_id}_{safe_emp}_{safe_type}_{captured_at.strftime('%Y%m%d_%H%M%S')}.jpg"
    evidence = CameraStream._draw_capture_overlay(frame.copy(), metadata | {"emp_code": emp_code}, captured_at)
    ok = cv2.imwrite(str(path), evidence, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        return {}
    return {
        "path": str(path),
        "image_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        "captured_at": captured_at.isoformat(),
    }


def _finish_attempt(
    db: Session,
    attempt: AttendanceAttempt,
    ok: bool,
    reason: str,
    message: str,
    risk_reasons: list[str] | None = None,
    extra: dict | None = None,
) -> dict:
    attempt.status = "success" if ok else "blocked"
    attempt.block_reason = "" if ok else reason
    attempt.message = message
    attempt.risk_reasons = json.dumps(risk_reasons or ([reason] if not ok else []), ensure_ascii=False)
    db.commit()
    return _response(ok, attempt, reason, message, extra or {})


def _response(ok: bool, attempt: AttendanceAttempt, status: str, message: str, extra: dict) -> dict:
    return {
        "ok": ok,
        "status": status,
        "check_type": attempt.check_type or extra.get("check_type", ""),
        "server_time": attempt.server_time.isoformat() if attempt.server_time else datetime.now().isoformat(),
        "distance_m": attempt.distance_m,
        "device_kind": attempt.device_kind or "",
        "face_confidence": round(float(attempt.face_confidence or extra.get("confidence") or 0.0), 4),
        "message": message,
        "attempt_id": attempt.id,
        "log_id": attempt.log_id or extra.get("id"),
        "session_id": attempt.session_id or extra.get("session_id"),
        "event_id": attempt.event_id or extra.get("event_id"),
        "employee": {
            "emp_code": attempt.emp_code,
            "name": extra.get("name", ""),
        },
    }


def _employee_dict(emp: Employee | None) -> dict | None:
    if not emp:
        return None
    return {
        "id": emp.id,
        "emp_code": emp.emp_code,
        "name": emp.name,
        "branch_id": emp.branch_id,
        "department": emp.department,
        "position": emp.position,
        "avatar_url": emp.avatar_url or "",
    }


def _branch_dict(branch: Branch | None) -> dict | None:
    if not branch:
        return None
    return {
        "id": branch.id,
        "name": branch.name,
        "address": branch.address or "",
        "latitude": branch.latitude,
        "longitude": branch.longitude,
        "geofence_radius_m": branch.geofence_radius_m or settings.MOBILE_GEOFENCE_RADIUS_DEFAULT_M,
        "mobile_attendance_enabled": bool(branch.mobile_attendance_enabled),
        "is_active": bool(branch.is_active),
    }


def _apply_capture(attempt: AttendanceAttempt, capture: dict) -> None:
    if not capture:
        return
    attempt.capture_path = capture.get("path") or attempt.capture_path
    attempt.image_hash = capture.get("image_hash") or attempt.image_hash


def _hash_value(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(f"{settings.JWT_SECRET}:{value}".encode("utf-8")).hexdigest()


def _safe_device_id(value: Any) -> str:
    return "".join(ch for ch in str(value or "")[:128] if ch.isalnum() or ch in ("-", "_", "."))


def _safe_name(value: Any) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value or ""))
    return safe.strip("_") or "unknown"


def _metadata_datetime(metadata: dict) -> datetime:
    try:
        return datetime.fromisoformat(str((metadata or {}).get("timestamp") or ""))
    except ValueError:
        return datetime.now()


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
