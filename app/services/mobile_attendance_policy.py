"""Server-side policy checks for mobile attendance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.utils.geo import haversine_distance_m

if TYPE_CHECKING:
    from app.models.branch import Branch
    from app.models.employee import Employee


@dataclass(frozen=True)
class DeviceProfile:
    kind: str
    is_mobile: bool
    reason: str


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    message: str
    risk_reasons: list[str]
    distance_m: float | None
    device: DeviceProfile
    policy_snapshot: dict[str, Any]


def classify_device(user_agent: str, client_hint_mobile: str = "") -> DeviceProfile:
    ua = (user_agent or "").lower()
    hint = (client_hint_mobile or "").strip().lower()
    mobile_tokens = ("iphone", "ipod", "windows phone")
    tablet_tokens = ("ipad", "tablet")
    desktop_tokens = ("windows nt", "macintosh", "x11", "linux x86_64")

    if hint == "?1":
        return DeviceProfile(kind="mobile", is_mobile=True, reason="client_hint_mobile")
    if "android" in ua and "mobile" in ua:
        return DeviceProfile(kind="mobile", is_mobile=True, reason="user_agent_android_mobile")
    if any(token in ua for token in mobile_tokens):
        return DeviceProfile(kind="mobile", is_mobile=True, reason="user_agent_mobile")
    if any(token in ua for token in tablet_tokens):
        return DeviceProfile(kind="tablet", is_mobile=False, reason="user_agent_tablet")
    if any(token in ua for token in desktop_tokens):
        return DeviceProfile(kind="desktop", is_mobile=False, reason="user_agent_desktop")
    return DeviceProfile(kind="unknown", is_mobile=False, reason="unknown_user_agent")


def evaluate_mobile_policy(
    *,
    emp: "Employee | None",
    branch: "Branch | None",
    latitude: float | None,
    longitude: float | None,
    accuracy_m: float | None,
    user_agent: str,
    client_hint_mobile: str = "",
) -> PolicyDecision:
    device = classify_device(user_agent, client_hint_mobile)
    snapshot = _policy_snapshot(branch)
    risks: list[str] = []

    if not settings.MOBILE_ATTENDANCE_ENABLED:
        return _blocked("mobile_disabled", "Chấm công mobile đang tắt", risks, None, device, snapshot)
    if not device.is_mobile:
        risks.append("non_mobile_device")
        return _blocked("non_mobile_device", "Chấm công mobile chỉ cho phép từ điện thoại", risks, None, device, snapshot)
    if not emp:
        return _blocked("employee_not_linked", "Tài khoản chưa liên kết với hồ sơ nhân viên", risks, None, device, snapshot)
    if not branch or not branch.is_active:
        return _blocked("branch_not_found", "Chưa cấu hình cửa hàng cho nhân viên", risks, None, device, snapshot)
    if not branch.mobile_attendance_enabled:
        return _blocked("branch_mobile_disabled", "Cửa hàng này chưa bật chấm công mobile", risks, None, device, snapshot)
    if branch.latitude is None or branch.longitude is None:
        return _blocked("branch_location_missing", "Cửa hàng chưa cấu hình tọa độ GPS", risks, None, device, snapshot)
    if latitude is None or longitude is None:
        risks.append("gps_missing")
        return _blocked("gps_missing", "Không nhận được vị trí GPS từ điện thoại", risks, None, device, snapshot)
    if accuracy_m is None:
        risks.append("gps_accuracy_missing")
        return _blocked("gps_accuracy_missing", "Không nhận được độ chính xác GPS", risks, None, device, snapshot)
    if accuracy_m > settings.MOBILE_GPS_MAX_ACCURACY_M:
        risks.append("gps_accuracy_too_low")
        return _blocked("gps_accuracy_too_low", f"GPS chưa đủ chính xác ({round(accuracy_m)}m)", risks, None, device, snapshot)

    distance = haversine_distance_m(float(latitude), float(longitude), float(branch.latitude), float(branch.longitude))
    radius = int(branch.geofence_radius_m or settings.MOBILE_GEOFENCE_RADIUS_DEFAULT_M)
    snapshot["computed_distance_m"] = round(distance, 2)
    snapshot["effective_radius_m"] = radius
    if distance > radius:
        risks.append("outside_geofence")
        return _blocked("outside_geofence", f"Bạn đang ngoài bán kính {radius}m của cửa hàng", risks, distance, device, snapshot)

    return PolicyDecision(
        allowed=True,
        reason="allowed",
        message="Policy hợp lệ",
        risk_reasons=risks,
        distance_m=round(distance, 2),
        device=device,
        policy_snapshot=snapshot,
    )


def _blocked(
    reason: str,
    message: str,
    risk_reasons: list[str],
    distance_m: float | None,
    device: DeviceProfile,
    policy_snapshot: dict[str, Any],
) -> PolicyDecision:
    return PolicyDecision(
        allowed=False,
        reason=reason,
        message=message,
        risk_reasons=risk_reasons or [reason],
        distance_m=round(distance_m, 2) if distance_m is not None else None,
        device=device,
        policy_snapshot=policy_snapshot,
    )


def _policy_snapshot(branch: "Branch | None") -> dict[str, Any]:
    return {
        "mobile_enabled": settings.MOBILE_ATTENDANCE_ENABLED,
        "gps_max_accuracy_m": settings.MOBILE_GPS_MAX_ACCURACY_M,
        "default_radius_m": settings.MOBILE_GEOFENCE_RADIUS_DEFAULT_M,
        "branch_id": branch.id if branch else None,
        "branch_mobile_enabled": bool(branch.mobile_attendance_enabled) if branch else False,
        "branch_radius_m": int(branch.geofence_radius_m or settings.MOBILE_GEOFENCE_RADIUS_DEFAULT_M) if branch else None,
        "branch_has_coordinates": bool(branch and branch.latitude is not None and branch.longitude is not None),
    }
