"""
AI-assisted shift planning.

The model only creates a draft. Real assignments are written after a manager
explicitly applies the draft.
"""

import json
import urllib.error
import urllib.request
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.employee import Employee
from app.models.shift import Shift, ShiftAssignment, ShiftPlanDraft, ShiftPlanDraftAssignment
from app.services.integration_settings import get_ai_provider_runtime_configs
from app.services.shift_service import assign_shift


def _date_range(start: date, end: date) -> list[date]:
    days = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def _json_loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _shift_dict(s: Shift) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "code": s.code,
        "work_start": s.work_start,
        "work_end": s.work_end,
        "required_position": s.required_position or "",
        "is_overnight": bool(s.is_overnight),
        "break_minutes": s.break_minutes or 0,
    }


def _employee_dict(e: Employee) -> dict:
    return {
        "emp_code": e.emp_code,
        "name": e.name,
        "department": e.department or "",
        "position": e.position or "",
    }


def _draft_to_dict(draft: ShiftPlanDraft, db: Session) -> dict:
    rows = (
        db.query(ShiftPlanDraftAssignment)
        .filter_by(draft_id=draft.id)
        .order_by(ShiftPlanDraftAssignment.work_date, ShiftPlanDraftAssignment.shift_id, ShiftPlanDraftAssignment.emp_code)
        .all()
    )
    shifts = {s.id: _shift_dict(s) for s in db.query(Shift).all()}
    employees = {e.emp_code: _employee_dict(e) for e in db.query(Employee).all()}
    return {
        "id": draft.id,
        "from_date": draft.from_date.isoformat(),
        "to_date": draft.to_date.isoformat(),
        "status": draft.status,
        "source": draft.source,
        "summary": draft.summary or "",
        "warnings": _json_loads(draft.warnings, []),
        "created_by": draft.created_by or "",
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "applied_at": draft.applied_at.isoformat() if draft.applied_at else None,
        "assignments": [
            {
                "id": r.id,
                "emp_code": r.emp_code,
                "employee": employees.get(r.emp_code),
                "shift_id": r.shift_id,
                "shift": shifts.get(r.shift_id),
                "work_date": r.work_date.isoformat(),
                "reason": r.reason or "",
                "validation_status": r.validation_status,
            }
            for r in rows
        ],
    }


def get_shift_plan_draft(draft_id: int, db: Session) -> Optional[dict]:
    draft = db.query(ShiftPlanDraft).filter_by(id=draft_id).first()
    return _draft_to_dict(draft, db) if draft else None


def create_shift_plan_draft(
    db: Session,
    from_date: date,
    to_date: date,
    created_by: str,
    instructions: str = "",
    default_min_staff: int = 1,
    min_staff_per_shift: Optional[dict[int, int]] = None,
    emp_codes: Optional[list[str]] = None,
    use_ai: bool = True,
) -> dict:
    employees_q = db.query(Employee).filter_by(is_active=True)
    if emp_codes:
        employees_q = employees_q.filter(Employee.emp_code.in_(emp_codes))
    employees = employees_q.order_by(Employee.emp_code).all()
    shifts = db.query(Shift).filter_by(is_active=True).order_by(Shift.work_start).all()
    dates = _date_range(from_date, to_date)
    if not employees:
        raise ValueError("Không có nhân viên hoạt động để phân ca")
    if not shifts:
        raise ValueError("Chưa có ca hoạt động")

    existing = (
        db.query(ShiftAssignment)
        .filter(ShiftAssignment.work_date >= from_date, ShiftAssignment.work_date <= to_date)
        .filter(ShiftAssignment.status != "cancelled")
        .all()
    )
    context = {
        "dates": [d.isoformat() for d in dates],
        "employees": [_employee_dict(e) for e in employees],
        "shifts": [_shift_dict(s) for s in shifts],
        "existing_assignments": [
            {"emp_code": a.emp_code, "shift_id": a.shift_id, "work_date": a.work_date.isoformat()}
            for a in existing
        ],
        "rules": {
            "default_min_staff_per_shift": max(default_min_staff, 0),
            "min_staff_per_shift": {str(k): v for k, v in (min_staff_per_shift or {}).items()},
            "max_shifts_per_employee_per_day": 2,
            "manager_instructions": instructions or "",
        },
    }

    source = "heuristic"
    ai_warnings: list[str] = []
    plan = None
    if use_ai:
        for provider in get_ai_provider_runtime_configs(db):
            try:
                if provider["provider"] == "openai":
                    plan = _call_openai_planner(context, provider["api_key"], provider["model"])
                elif provider["provider"] == "gemini":
                    plan = _call_gemini_planner(context, provider["api_key"], provider["model"])
                else:
                    continue
                source = provider["provider"]
                break
            except Exception as exc:
                ai_warnings.append(f"{provider['provider']} không tạo được lịch: {exc}")

    if not plan:
        plan = _heuristic_plan(context)

    valid_assignments, validation_warnings = _validate_plan(plan.get("assignments", []), employees, shifts, dates)
    warnings = ai_warnings + plan.get("warnings", []) + validation_warnings
    summary = plan.get("summary") or f"Đề xuất {len(valid_assignments)} lượt phân ca."

    draft = ShiftPlanDraft(
        from_date=from_date,
        to_date=to_date,
        status="draft",
        source=source,
        prompt=json.dumps(context, ensure_ascii=False),
        summary=summary,
        warnings=json.dumps(warnings, ensure_ascii=False),
        created_by=created_by,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)

    for item in valid_assignments:
        db.add(ShiftPlanDraftAssignment(
            draft_id=draft.id,
            emp_code=item["emp_code"],
            shift_id=item["shift_id"],
            work_date=date.fromisoformat(item["work_date"]),
            reason=item.get("reason", ""),
            validation_status="valid",
        ))
    db.commit()
    return _draft_to_dict(draft, db)


def apply_shift_plan_draft(draft_id: int, applied_by: str, db: Session) -> dict:
    draft = db.query(ShiftPlanDraft).filter_by(id=draft_id).first()
    if not draft:
        raise ValueError("Không tìm thấy bản nháp")
    if draft.status == "applied":
        return {"applied": 0, "message": "Bản nháp đã được áp dụng trước đó"}

    rows = db.query(ShiftPlanDraftAssignment).filter_by(draft_id=draft.id, validation_status="valid").all()
    count = 0
    for row in rows:
        assign_shift(
            emp_code=row.emp_code,
            shift_id=row.shift_id,
            work_date=row.work_date,
            assigned_by=applied_by,
            note=f"AI đề xuất: {row.reason}"[:255],
            db=db,
        )
        count += 1

    draft.status = "applied"
    draft.applied_by = applied_by
    draft.applied_at = datetime.now()
    db.commit()
    return {"applied": count, "message": f"Đã áp dụng {count} lượt phân ca từ bản nháp"}


def _validate_plan(items: list[dict], employees: list[Employee], shifts: list[Shift], dates: list[date]) -> tuple[list[dict], list[str]]:
    emp_codes = {e.emp_code for e in employees}
    shift_ids = {s.id for s in shifts}
    date_set = {d.isoformat() for d in dates}
    seen: set[tuple[str, str, int]] = set()
    per_emp_day: dict[tuple[str, str], int] = {}
    valid = []
    warnings = []

    for idx, raw in enumerate(items, 1):
        emp_code = str(raw.get("emp_code", "")).strip()
        work_date = str(raw.get("work_date", "")).strip()
        try:
            shift_id = int(raw.get("shift_id"))
        except Exception:
            warnings.append(f"Dòng {idx}: shift_id không hợp lệ")
            continue
        if emp_code not in emp_codes:
            warnings.append(f"Dòng {idx}: nhân viên {emp_code or '?'} không hợp lệ hoặc không hoạt động")
            continue
        if shift_id not in shift_ids:
            warnings.append(f"Dòng {idx}: ca #{shift_id} không hợp lệ hoặc đã tắt")
            continue
        if work_date not in date_set:
            warnings.append(f"Dòng {idx}: ngày {work_date or '?'} nằm ngoài khoảng lập lịch")
            continue
        key = (emp_code, work_date, shift_id)
        if key in seen:
            continue
        day_key = (emp_code, work_date)
        per_emp_day[day_key] = per_emp_day.get(day_key, 0) + 1
        if per_emp_day[day_key] > 2:
            warnings.append(f"{emp_code} có hơn 2 ca trong ngày {work_date}; bỏ đề xuất dư")
            continue
        seen.add(key)
        valid.append({
            "emp_code": emp_code,
            "shift_id": shift_id,
            "work_date": work_date,
            "reason": str(raw.get("reason", "")).strip()[:500],
        })
    return valid, warnings


def _heuristic_plan(context: dict) -> dict:
    employees = context["employees"]
    shifts = context["shifts"]
    dates = context["dates"]
    existing = context["existing_assignments"]
    rules = context["rules"]
    default_min = max(int(rules.get("default_min_staff_per_shift") or 1), 0)
    per_shift = {int(k): int(v) for k, v in (rules.get("min_staff_per_shift") or {}).items()}

    existing_keys = {(a["emp_code"], a["work_date"], int(a["shift_id"])) for a in existing}
    existing_by_date_shift: dict[tuple[str, int], int] = {}
    daily_count: dict[tuple[str, str], int] = {}
    load: dict[str, int] = {e["emp_code"]: 0 for e in employees}

    for emp_code, work_date, shift_id in existing_keys:
        existing_by_date_shift[(work_date, shift_id)] = existing_by_date_shift.get((work_date, shift_id), 0) + 1
        daily_count[(emp_code, work_date)] = daily_count.get((emp_code, work_date), 0) + 1
        if emp_code in load:
            load[emp_code] += 1

    assignments = []
    warnings = []
    for work_date in dates:
        day = date.fromisoformat(work_date)
        weekend_boost = 1 if day.weekday() >= 5 and default_min > 0 else 0
        for shift in shifts:
            shift_id = int(shift["id"])
            need = max(per_shift.get(shift_id, default_min + weekend_boost) - existing_by_date_shift.get((work_date, shift_id), 0), 0)
            required_position = _normalize_role(shift.get("required_position", ""))
            for _ in range(need):
                pool = [
                    e for e in employees
                    if not required_position or required_position in _normalize_role(e.get("position", ""))
                ]
                if required_position and not pool:
                    warnings.append(f"Không có nhân viên vị trí {shift.get('required_position')} cho {shift['name']}")
                    break
                candidates = sorted(
                    pool,
                    key=lambda e: (daily_count.get((e["emp_code"], work_date), 0), load.get(e["emp_code"], 0), e["emp_code"]),
                )
                chosen = None
                for emp in candidates:
                    code = emp["emp_code"]
                    if (code, work_date, shift_id) in existing_keys:
                        continue
                    if daily_count.get((code, work_date), 0) >= 2:
                        continue
                    chosen = emp
                    break
                if not chosen:
                    warnings.append(f"Không đủ nhân viên cho {shift['name']} ngày {work_date}")
                    break
                code = chosen["emp_code"]
                existing_keys.add((code, work_date, shift_id))
                daily_count[(code, work_date)] = daily_count.get((code, work_date), 0) + 1
                load[code] = load.get(code, 0) + 1
                assignments.append({
                    "emp_code": code,
                    "shift_id": shift_id,
                    "work_date": work_date,
                    "reason": "Phân bổ tự động để đủ số người tối thiểu và cân bằng tải",
                })

    return {
        "summary": f"Thuật toán đề xuất {len(assignments)} lượt phân ca còn thiếu.",
        "warnings": warnings,
        "assignments": assignments,
    }


def _planner_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "emp_code": {"type": "string"},
                        "shift_id": {"type": "integer"},
                        "work_date": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["emp_code", "shift_id", "work_date", "reason"],
                },
            },
        },
        "required": ["summary", "warnings", "assignments"],
    }


def _normalize_role(value: str) -> str:
    raw = (value or "").replace("đ", "d").replace("Đ", "D")
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", raw.lower())
        if not unicodedata.combining(ch)
    ).strip()


def _planner_instruction() -> str:
    return (
        "Bạn là trợ lý lập lịch ca nhà hàng. Chỉ tạo bản nháp phân ca, "
        "không xoá lịch hiện có. Ưu tiên đủ người mỗi ca, chia đều tải, "
        "không xếp quá 2 ca/người/ngày, chỉ gán nhân viên có position phù hợp "
        "khi ca có required_position, và tôn trọng dữ liệu đầu vào. "
        "Chỉ trả JSON đúng schema."
    )


def _call_openai_planner(context: dict, api_key: str, model: str) -> dict:
    schema = {
        **_planner_schema()
    }
    payload = {
        "model": model or settings.OPENAI_MODEL,
        "input": [
            {
                "role": "system",
                "content": [{
                    "type": "input_text",
                    "text": _planner_instruction(),
                }],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": json.dumps(context, ensure_ascii=False)}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "shift_plan",
                "schema": schema,
                "strict": True,
            }
        },
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as res:
            data = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI API lỗi {exc.code}: {detail[:300]}") from exc

    text = data.get("output_text") or _extract_response_text(data)
    if not text:
        raise RuntimeError("OpenAI không trả nội dung JSON")
    return json.loads(text)


def _extract_response_text(data: dict) -> str:
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") in ("output_text", "text"):
                return content.get("text", "")
    return ""


def _call_gemini_planner(context: dict, api_key: str, model: str) -> dict:
    prompt = _planner_instruction() + "\n\nDữ liệu lập lịch:\n" + json.dumps(context, ensure_ascii=False)
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": _planner_schema(),
        },
    }
    safe_model = model or settings.GEMINI_MODEL
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{safe_model}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as res:
            data = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Gemini API lỗi {exc.code}: {detail[:300]}") from exc

    text = _extract_gemini_text(data)
    if not text:
        raise RuntimeError("Gemini không trả nội dung JSON")
    return json.loads(text)


def _extract_gemini_text(data: dict) -> str:
    for candidate in data.get("candidates", []) or []:
        content = candidate.get("content") or {}
        for part in content.get("parts", []) or []:
            if "text" in part:
                return part["text"]
    return ""
