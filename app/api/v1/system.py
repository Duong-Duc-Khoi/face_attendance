"""System health and runtime configuration endpoints."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.config import settings
from app.core.security import get_current_user
from app.models.user import User
from app.services.camera import get_camera
from app.services.face_engine import face_engine
from app.services.presentation_guard import presentation_guard_service

router = APIRouter(prefix="/api", tags=["system"])


CONFIG_GROUPS = {
    "auth": ("LOGIN_OTP_ENABLED",),
    "recognition": ("FACE_THRESHOLD", "MIN_FACE_SIZE"),
    "presentation_guard": (
        "PRESENTATION_GUARD_ENABLED",
        "PRESENTATION_GUARD_ACTION",
        "PRESENTATION_GUARD_RISK_THRESHOLD",
        "PRESENTATION_GUARD_MIN_FRAMES",
        "PRESENTATION_GUARD_WINDOW_SECONDS",
    ),
    "evidence": ("EVIDENCE_RETENTION_DAYS", "AI_AUDIT_MAX_IMAGES_PER_RUN"),
    "attendance": (
        "COOLDOWN_MINUTES",
        "CHECKIN_GRACE_MINUTES",
        "OVERTIME_APPROVAL_THRESHOLD_MINUTES",
        "CONSECUTIVE_SHIFT_GAP_MINUTES",
    ),
    "calendar": ("WORK_DAYS",),
    "notifications": ("NOTIFY_LEAVE_CANCEL", "DAILY_REPORT_HOUR", "DAILY_REPORT_MINUTE"),
}

ALLOWED_CONFIG_KEYS = {key for keys in CONFIG_GROUPS.values() for key in keys}
CONFIG_LABELS = {
    "FACE_THRESHOLD": "Độ khớp tối thiểu",
    "MIN_FACE_SIZE": "Kích thước mặt tối thiểu",
    "PRESENTATION_GUARD_ENABLED": "Kiểm tra chống giả mạo",
    "PRESENTATION_GUARD_ACTION": "Cách xử lý rủi ro",
    "PRESENTATION_GUARD_RISK_THRESHOLD": "Mức rủi ro cần xử lý",
    "PRESENTATION_GUARD_MIN_FRAMES": "Số khung hình xác minh",
    "PRESENTATION_GUARD_WINDOW_SECONDS": "Thời gian quan sát",
    "EVIDENCE_RETENTION_DAYS": "Thời gian giữ ảnh bằng chứng",
    "AI_AUDIT_MAX_IMAGES_PER_RUN": "Số ảnh tối đa mỗi lượt kiểm tra AI",
    "COOLDOWN_MINUTES": "Thời gian chờ giữa hai lần chấm công",
    "CHECKIN_GRACE_MINUTES": "Số phút cho phép vào muộn",
    "OVERTIME_APPROVAL_THRESHOLD_MINUTES": "Ngưỡng cần duyệt tăng ca",
    "CONSECUTIVE_SHIFT_GAP_MINUTES": "Khoảng nghỉ tối đa tính liên ca",
    "WORK_DAYS": "Ngày vận hành mặc định",
    "NOTIFY_LEAVE_CANCEL": "Thông báo khi hủy đơn nghỉ",
    "DAILY_REPORT_HOUR": "Giờ gửi báo cáo cuối ngày",
    "DAILY_REPORT_MINUTE": "Phút gửi báo cáo cuối ngày",
    "LOGIN_OTP_ENABLED": "OTP đăng nhập",
}


def _label(key: str) -> str:
    return CONFIG_LABELS.get(key, key)


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(403, "Yêu cầu quyền admin")


def _project_env_path() -> Path:
    return Path(__file__).resolve().parents[3] / ".env"


def _bool_value(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
    raise HTTPException(400, f"{_label(key)} phải là bật hoặc tắt")


def _int_value(value: Any, key: str, min_value: int, max_value: int) -> int:
    if isinstance(value, bool):
        raise HTTPException(400, f"{_label(key)} phải là số nguyên")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{_label(key)} phải là số nguyên")
    if parsed < min_value or parsed > max_value:
        raise HTTPException(400, f"{_label(key)} phải trong khoảng {min_value}-{max_value}")
    return parsed


def _float_value(value: Any, key: str, min_value: float, max_value: float) -> float:
    if isinstance(value, bool):
        raise HTTPException(400, f"{_label(key)} phải là số")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{_label(key)} phải là số")
    if parsed < min_value or parsed > max_value:
        raise HTTPException(400, f"{_label(key)} phải trong khoảng {min_value}-{max_value}")
    return parsed


def _work_days_value(value: Any) -> str:
    if isinstance(value, list):
        raw_parts = value
    else:
        raw_parts = str(value or "").split(",")
    days: list[int] = []
    for part in raw_parts:
        try:
            day = int(str(part).strip())
        except ValueError:
            raise HTTPException(400, "Ngày vận hành mặc định chỉ nhận các ngày trong tuần")
        if day < 1 or day > 7:
            raise HTTPException(400, "Ngày vận hành mặc định chỉ nhận các ngày trong tuần")
        if day in days:
            raise HTTPException(400, "Ngày vận hành mặc định không được trùng ngày")
        days.append(day)
    if not days:
        raise HTTPException(400, "Cần chọn ít nhất một ngày vận hành")
    return ",".join(str(day) for day in sorted(days))


def _validate_config_value(key: str, value: Any) -> Any:
    if key == "LOGIN_OTP_ENABLED":
        return _bool_value(value, key)
    if key == "FACE_THRESHOLD":
        return round(_float_value(value, key, 0.0, 1.0), 4)
    if key == "MIN_FACE_SIZE":
        return _int_value(value, key, 20, 800)
    if key == "PRESENTATION_GUARD_ENABLED":
        return _bool_value(value, key)
    if key == "PRESENTATION_GUARD_ACTION":
        action = str(value or "").strip().lower()
        if action not in ("review", "block"):
            raise HTTPException(400, "Cách xử lý rủi ro phải là đưa vào danh sách cần xem lại hoặc chặn chấm công")
        return action
    if key == "PRESENTATION_GUARD_RISK_THRESHOLD":
        return round(_float_value(value, key, 0.0, 1.0), 4)
    if key == "PRESENTATION_GUARD_MIN_FRAMES":
        return _int_value(value, key, 1, 20)
    if key == "PRESENTATION_GUARD_WINDOW_SECONDS":
        return round(_float_value(value, key, 1.0, 30.0), 2)
    if key == "EVIDENCE_RETENTION_DAYS":
        return _int_value(value, key, 1, 3650)
    if key == "AI_AUDIT_MAX_IMAGES_PER_RUN":
        return _int_value(value, key, 1, 1000)
    if key == "COOLDOWN_MINUTES":
        return _int_value(value, key, 0, 1440)
    if key == "CHECKIN_GRACE_MINUTES":
        return _int_value(value, key, 0, 240)
    if key == "OVERTIME_APPROVAL_THRESHOLD_MINUTES":
        return _int_value(value, key, 0, 720)
    if key == "CONSECUTIVE_SHIFT_GAP_MINUTES":
        return _int_value(value, key, 0, 240)
    if key == "WORK_DAYS":
        return _work_days_value(value)
    if key == "NOTIFY_LEAVE_CANCEL":
        return _bool_value(value, key)
    if key == "DAILY_REPORT_HOUR":
        return _int_value(value, key, 0, 23)
    if key == "DAILY_REPORT_MINUTE":
        return _int_value(value, key, 0, 59)
    raise HTTPException(400, f"Không hỗ trợ cấu hình {_label(key)}")


def _flatten_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        raise HTTPException(400, "Payload cấu hình trống")
    result: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in payload.items():
        if key in ALLOWED_CONFIG_KEYS:
            result[key] = value
        elif key in CONFIG_GROUPS and isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if nested_key in CONFIG_GROUPS[key]:
                    result[nested_key] = nested_value
                else:
                    unknown.append(nested_key)
        else:
            unknown.append(key)
    if unknown:
        raise HTTPException(400, "Có cấu hình không được hỗ trợ")
    if not result:
        raise HTTPException(400, "Payload cấu hình trống")
    return result


def _env_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_env_values(values: dict[str, Any]) -> None:
    env_path = _project_env_path()
    if not env_path.exists():
        raise HTTPException(500, "Không tìm thấy file .env để lưu cấu hình")

    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    updated: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        replaced = False
        for key, value in values.items():
            if line.startswith(key + "="):
                if line.endswith("\r\n"):
                    ending = "\r\n"
                elif line.endswith("\n"):
                    ending = "\n"
                else:
                    ending = ""
                new_lines.append(f"{key}={_env_string(value)}{ending}")
                updated.add(key)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)

    if new_lines and not new_lines[-1].endswith(("\n", "\r")):
        new_lines[-1] += "\n"
    for key, value in values.items():
        if key not in updated:
            new_lines.append(f"{key}={_env_string(value)}\n")

    env_path.write_text("".join(new_lines), encoding="utf-8")


def _apply_runtime_config(values: dict[str, Any]) -> None:
    for key, value in values.items():
        setattr(settings, key, value)

    if "FACE_THRESHOLD" in values:
        face_engine.threshold = float(values["FACE_THRESHOLD"])

    guard_keys = {
        "PRESENTATION_GUARD_ENABLED",
        "PRESENTATION_GUARD_ACTION",
        "PRESENTATION_GUARD_RISK_THRESHOLD",
        "PRESENTATION_GUARD_MIN_FRAMES",
        "PRESENTATION_GUARD_WINDOW_SECONDS",
    }
    if guard_keys.intersection(values):
        presentation_guard_service.enabled = settings.PRESENTATION_GUARD_ENABLED
        presentation_guard_service.action = settings.PRESENTATION_GUARD_ACTION
        presentation_guard_service.threshold = settings.PRESENTATION_GUARD_RISK_THRESHOLD
        presentation_guard_service.min_frames = max(1, settings.PRESENTATION_GUARD_MIN_FRAMES)
        presentation_guard_service.window_seconds = max(1.0, settings.PRESENTATION_GUARD_WINDOW_SECONDS)
        presentation_guard_service._samples.clear()

    if {"DAILY_REPORT_HOUR", "DAILY_REPORT_MINUTE"}.intersection(values):
        _reschedule_daily_report()


def _reschedule_daily_report() -> None:
    try:
        from apscheduler.triggers.cron import CronTrigger
        from app.core.lifespan import scheduler

        if not scheduler.get_job("daily_report"):
            return
        hour = max(0, min(23, int(settings.DAILY_REPORT_HOUR)))
        minute = max(0, min(59, int(settings.DAILY_REPORT_MINUTE)))
        scheduler.reschedule_job("daily_report", trigger=CronTrigger(hour=hour, minute=minute))
    except Exception as exc:
        print(f"[warn] Khong reschedule duoc daily_report: {exc}")


def _config_response() -> dict[str, dict[str, Any]]:
    return {
        group: {key: getattr(settings, key) for key in keys}
        for group, keys in CONFIG_GROUPS.items()
    }


@router.get("/health")
def health_check():
    cam = get_camera()
    return {
        "status": "ok",
        "camera": cam.cap.isOpened() if cam and cam.cap else False,
        "face_engine": face_engine._initialized,
        "employees": face_engine.registered_count,
    }


@router.get("/config")
def get_config(current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    return _config_response()


@router.put("/config")
async def update_config(
    payload: dict[str, Any],
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    raw_values = _flatten_payload(payload)
    values = {key: _validate_config_value(key, value) for key, value in raw_values.items()}
    _write_env_values(values)
    _apply_runtime_config(values)
    return {
        "success": True,
        "message": "Đã cập nhật cấu hình",
        "config": _config_response(),
    }
