"""
Evidence and review-first audit helpers for attendance events.

The audit layer never declares fraud automatically. It stores risk signals so a
manager can review the image evidence and make the final call.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.attendance import (
    AttendanceAuditFinding,
    AttendanceAuditRun,
    AttendanceEvent,
    AttendanceEvidence,
    AttendanceLog,
)
from app.services.attendance import LOW_CONFIDENCE_THRESHOLD
from app.services.employee_branch_history import filter_logs_by_branch_ids
from app.services.integration_settings import get_ai_provider_runtime_configs


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _risk_level(score: float) -> str:
    if score >= 0.80:
        return "high"
    if score >= 0.50:
        return "medium"
    if score >= 0.20:
        return "low"
    return "clear"


def _safe_path(path_value: str) -> bool:
    if not path_value:
        return False
    try:
        root = settings.CAPTURES_DIR.resolve()
        path = Path(path_value).resolve()
        path.relative_to(root)
        return path.exists()
    except (OSError, ValueError):
        return False


def _matching_event(db: Session, log: AttendanceLog, event_id: Optional[int] = None) -> AttendanceEvent | None:
    if event_id:
        event = db.query(AttendanceEvent).filter_by(id=event_id).first()
        if event:
            return event
    return (
        db.query(AttendanceEvent)
        .filter(
            AttendanceEvent.employee_id == log.employee_id,
            AttendanceEvent.event_type == log.check_type,
            AttendanceEvent.event_time == log.timestamp,
        )
        .order_by(AttendanceEvent.id.desc())
        .first()
    )


def _evidence_for_log(db: Session, log_id: int) -> AttendanceEvidence | None:
    return (
        db.query(AttendanceEvidence)
        .filter_by(log_id=log_id)
        .order_by(AttendanceEvidence.id.desc())
        .first()
    )


def _ensure_image_evidence(db: Session, log: AttendanceLog) -> AttendanceEvidence | None:
    evidence = _evidence_for_log(db, log.id)
    if evidence and evidence.image_path and _safe_path(evidence.image_path):
        if not evidence.files_available:
            evidence.files_available = True
        return evidence

    if not log.capture_path or not _safe_path(log.capture_path):
        return evidence

    event = _matching_event(db, log)
    if not evidence:
        evidence = AttendanceEvidence(log_id=log.id)
        db.add(evidence)
    evidence.event_id = event.id if event else evidence.event_id
    evidence.session_id = event.session_id if event else evidence.session_id
    evidence.employee_id = log.employee_id
    evidence.emp_code = log.emp_code or ""
    evidence.image_path = log.capture_path
    if not evidence.image_hash:
        try:
            evidence.image_hash = hashlib.sha256(Path(log.capture_path).read_bytes()).hexdigest()
        except OSError:
            evidence.image_hash = event.image_hash if event and event.image_hash else ""
    evidence.files_available = True
    evidence.captured_at = evidence.captured_at or log.timestamp
    return evidence


def _score_log(
    log: AttendanceLog,
    evidence: AttendanceEvidence | None,
    presentation: Optional[dict] = None,
    duplicate_hash: bool = False,
) -> tuple[float, list[str], dict]:
    confidence = float(log.confidence or 0.0)
    score = 0.0
    reasons: list[str] = []
    metrics: dict[str, Any] = {
        "confidence": round(confidence, 4),
        "threshold": LOW_CONFIDENCE_THRESHOLD,
    }

    if 0 < confidence < LOW_CONFIDENCE_THRESHOLD:
        score = max(score, 0.50)
        reasons.append("low_confidence")

    if not evidence or not evidence.files_available or not evidence.image_path:
        score = max(score, 0.30)
        reasons.append("missing_evidence")

    if duplicate_hash:
        score = max(score, 0.70)
        reasons.append("duplicate_image_hash")

    presentation = presentation or {}
    if presentation:
        p_risk = float(presentation.get("risk") or 0.0)
        p_reason = presentation.get("reason") or ""
        p_metrics = presentation.get("metrics") or {}
        metrics["presentation_guard"] = {
            "risk": round(p_risk, 4),
            "reason": p_reason,
            "metrics": p_metrics,
        }
        if p_reason and p_reason not in ("clear", "disabled", "collecting_frames"):
            score = max(score, min(1.0, p_risk))
            reasons.append("presentation_risk")
        for reason in p_metrics.get("reasons", []) or []:
            if reason not in reasons:
                reasons.append(str(reason))

        strong = {"device_rectangle", "phone_edges", "screen_texture", "rigid_screen_motion", "static_photo"}
        strong_hits = strong.intersection(set(reasons))
        if len(strong_hits) >= 2:
            score = max(score, 0.82)

    return min(1.0, round(score, 4)), sorted(set(reasons)), metrics


def _upsert_finding(
    db: Session,
    log: AttendanceLog,
    event: AttendanceEvent | None,
    evidence: AttendanceEvidence | None,
    risk_score: float,
    reasons: list[str],
    metrics: dict,
    source: str,
    audit_run_id: int | None = None,
    persist_clear: bool = False,
) -> AttendanceAuditFinding | None:
    if risk_score < 0.20 and not persist_clear:
        return None

    finding = (
        db.query(AttendanceAuditFinding)
        .filter_by(log_id=log.id, source=source)
        .order_by(AttendanceAuditFinding.id.desc())
        .first()
    )
    duplicates = (
        db.query(AttendanceAuditFinding)
        .filter_by(log_id=log.id, source=source)
        .order_by(AttendanceAuditFinding.id.desc())
        .all()
    )
    if duplicates:
        finding = duplicates[0]
        for duplicate in duplicates[1:]:
            db.delete(duplicate)
    if not finding:
        finding = AttendanceAuditFinding(log_id=log.id, source=source)
        db.add(finding)

    finding.audit_run_id = audit_run_id or finding.audit_run_id
    finding.event_id = event.id if event else None
    finding.evidence_id = evidence.id if evidence else None
    finding.employee_id = log.employee_id
    finding.emp_code = log.emp_code or ""
    finding.emp_name = log.emp_name or ""
    finding.risk_score = risk_score
    finding.risk_level = _risk_level(risk_score)
    finding.reasons = _json_dumps(reasons)
    finding.metrics = _json_dumps(metrics)
    finding.updated_at = datetime.now()
    if finding.review_status not in ("dismissed", "confirmed", "reviewed"):
        finding.review_status = "pending_review"
    return finding


def record_attendance_evidence(
    log_id: int,
    capture: dict,
    event_id: int | None = None,
    presentation: Optional[dict] = None,
) -> None:
    """Persist image evidence and realtime risk signals for one attendance log."""
    db = SessionLocal()
    try:
        log = db.query(AttendanceLog).filter_by(id=log_id).first()
        if not log:
            return
        event = _matching_event(db, log, event_id)

        image_path = capture.get("path", "")
        image_hash = capture.get("image_hash", "")

        log.capture_path = image_path or log.capture_path
        if event:
            event.capture_path = image_path or event.capture_path
            event.image_hash = image_hash or event.image_hash

        evidence = _evidence_for_log(db, log.id)
        if not evidence:
            evidence = AttendanceEvidence(log_id=log.id)
            db.add(evidence)
        evidence.event_id = event.id if event else event_id
        evidence.session_id = event.session_id if event else None
        evidence.employee_id = log.employee_id
        evidence.emp_code = log.emp_code or ""
        evidence.image_path = image_path
        evidence.image_hash = image_hash
        evidence.files_available = bool(_safe_path(image_path))
        try:
            evidence.captured_at = datetime.fromisoformat(str(capture.get("captured_at") or ""))
        except ValueError:
            evidence.captured_at = log.timestamp

        risk_score, reasons, metrics = _score_log(log, evidence, presentation)
        _upsert_finding(db, log, event, evidence, risk_score, reasons, metrics, "realtime")
        db.commit()
    except Exception as exc:
        db.rollback()
        print(f"  ✗ record_attendance_evidence lỗi: {exc}")
    finally:
        db.close()


def _finding_to_dict(
    f: AttendanceAuditFinding,
    evidence: AttendanceEvidence | None = None,
    log: AttendanceLog | None = None,
) -> dict:
    return {
        "id": f.id,
        "audit_run_id": f.audit_run_id,
        "log_id": f.log_id,
        "event_id": f.event_id,
        "evidence_id": f.evidence_id,
        "emp_code": f.emp_code,
        "emp_name": f.emp_name,
        "check_type": log.check_type if log else "",
        "timestamp": log.timestamp.isoformat() if log and log.timestamp else None,
        "time": log.timestamp.strftime("%H:%M:%S") if log and log.timestamp else "",
        "date": log.timestamp.strftime("%d/%m/%Y") if log and log.timestamp else "",
        "risk_score": f.risk_score,
        "risk_level": f.risk_level,
        "reasons": _json_loads(f.reasons, []),
        "metrics": _json_loads(f.metrics, {}),
        "source": f.source,
        "review_status": f.review_status,
        "reviewer_note": f.reviewer_note or "",
        "reviewed_by": f.reviewed_by or "",
        "reviewed_at": f.reviewed_at.isoformat() if f.reviewed_at else None,
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "has_image": bool(evidence and evidence.files_available and evidence.image_path),
    }


def list_audit_findings(
    db: Session,
    date_str: str = "",
    review_status: str = "pending_review",
    risk_level: str = "",
    limit: int = 100,
    from_date: str = "",
    to_date: str = "",
    emp_code: str = "",
    branch_ids: list[int] | None = None,
) -> list[dict]:
    q = db.query(AttendanceAuditFinding)
    if review_status:
        q = q.filter(AttendanceAuditFinding.review_status == review_status)
    if risk_level:
        q = q.filter(AttendanceAuditFinding.risk_level == risk_level)
    if date_str or from_date or to_date or emp_code or branch_ids is not None:
        if date_str and not from_date:
            from_date = date_str
            to_date = date_str
        start_dt = datetime.strptime(from_date or datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d")
        end_dt = datetime.strptime(to_date or from_date or datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d")
        start = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        log_q = db.query(AttendanceLog).filter(AttendanceLog.timestamp >= start, AttendanceLog.timestamp <= end)
        if emp_code:
            log_q = log_q.filter(AttendanceLog.emp_code == emp_code)
        if branch_ids is not None:
            log_ids = [log.id for log in filter_logs_by_branch_ids(db, log_q.all(), branch_ids)]
        else:
            log_ids = [log.id for log in log_q.all()]
        if not log_ids:
            return []
        q = q.filter(AttendanceAuditFinding.log_id.in_(log_ids))
    rows = q.order_by(AttendanceAuditFinding.risk_score.desc(), AttendanceAuditFinding.created_at.desc()).limit(min(max(limit, 1), 500)).all()
    source_rank = {"vision_ai": 3, "realtime": 2, "heuristic": 1}
    best_by_log: dict[int, AttendanceAuditFinding] = {}
    for row in rows:
        current = best_by_log.get(row.log_id)
        if not current:
            best_by_log[row.log_id] = row
            continue
        current_key = (source_rank.get(current.source, 0), float(current.risk_score or 0), current.id)
        row_key = (source_rank.get(row.source, 0), float(row.risk_score or 0), row.id)
        if row_key > current_key:
            best_by_log[row.log_id] = row
    rows = sorted(
        best_by_log.values(),
        key=lambda item: (float(item.risk_score or 0), item.created_at or datetime.min),
        reverse=True,
    )[:min(max(limit, 1), 500)]
    evidence_by_id = {
        e.id: e for e in db.query(AttendanceEvidence).filter(AttendanceEvidence.id.in_([r.evidence_id for r in rows if r.evidence_id])).all()
    } if rows else {}
    log_by_id = {
        log.id: log for log in db.query(AttendanceLog).filter(AttendanceLog.id.in_([r.log_id for r in rows])).all()
    } if rows else {}
    return [_finding_to_dict(row, evidence_by_id.get(row.evidence_id), log_by_id.get(row.log_id)) for row in rows]


def update_audit_finding_review(
    finding_id: int,
    status: str,
    note: str,
    reviewer: str,
    db: Session,
) -> dict | None:
    if status not in ("reviewed", "dismissed", "confirmed", "pending_review"):
        raise ValueError("review_status không hợp lệ")
    finding = db.query(AttendanceAuditFinding).filter_by(id=finding_id).first()
    if not finding:
        return None
    finding.review_status = status
    finding.reviewer_note = note or ""
    finding.reviewed_by = reviewer
    finding.reviewed_at = datetime.now() if status != "pending_review" else None
    finding.updated_at = datetime.now()
    db.commit()
    evidence = db.query(AttendanceEvidence).filter_by(id=finding.evidence_id).first() if finding.evidence_id else None
    log = db.query(AttendanceLog).filter_by(id=finding.log_id).first()
    return _finding_to_dict(finding, evidence, log)


def run_attendance_audit(
    db: Session,
    from_date: date,
    to_date: date,
    run_type: str,
    created_by: str,
    emp_code: str = "",
    branch_ids: list[int] | None = None,
    use_ai: bool = True,
) -> dict:
    run = AttendanceAuditRun(
        run_type=run_type,
        from_date=from_date,
        to_date=to_date,
        emp_code=emp_code or "",
        status="running",
        created_by=created_by,
        created_at=datetime.now(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    warnings: list[str] = []
    start = datetime.combine(from_date, datetime.min.time())
    end = datetime.combine(to_date, datetime.max.time())
    q = db.query(AttendanceLog).filter(
        AttendanceLog.timestamp >= start,
        AttendanceLog.timestamp <= end,
        AttendanceLog.confidence > 0,
    )
    if emp_code:
        q = q.filter(AttendanceLog.emp_code == emp_code)
    if run_type == "low_confidence":
        q = q.filter(AttendanceLog.confidence > 0, AttendanceLog.confidence < LOW_CONFIDENCE_THRESHOLD)
    logs = q.order_by(AttendanceLog.timestamp.asc()).all()
    logs = filter_logs_by_branch_ids(db, logs, branch_ids)
    ai_configs = get_ai_provider_runtime_configs(db) if use_ai else []
    ai_attempted = bool(ai_configs)

    hash_counts: dict[str, int] = {}
    for evidence in db.query(AttendanceEvidence).filter(AttendanceEvidence.image_hash != "").all():
        hash_counts[evidence.image_hash] = hash_counts.get(evidence.image_hash, 0) + 1

    findings: list[AttendanceAuditFinding] = []
    prioritized_logs: list[tuple[float, AttendanceLog]] = []
    for log in logs:
        event = _matching_event(db, log)
        if not event or event.source != "face":
            continue
        evidence = _ensure_image_evidence(db, log)
        duplicate_hash = bool(evidence and evidence.image_hash and hash_counts.get(evidence.image_hash, 0) > 1)
        risk_score, reasons, metrics = _score_log(log, evidence, duplicate_hash=duplicate_hash)
        prioritized_logs.append((risk_score, log))
        if not use_ai:
            finding = _upsert_finding(db, log, event, evidence, risk_score, reasons, metrics, "heuristic", run.id)
            if finding:
                findings.append(finding)

    ai_stats = {"reviewed": 0, "findings": 0}
    if use_ai:
        prioritized_logs.sort(key=lambda item: (item[0], item[1].timestamp), reverse=True)
        ai_stats = _run_optional_vision_ai(db, run, [item[1] for item in prioritized_logs], ai_configs)
        warnings.extend(ai_stats.get("warnings", []))

    db.flush()
    all_findings = db.query(AttendanceAuditFinding).filter_by(audit_run_id=run.id).all()
    vision_findings = len([f for f in all_findings if f.source == "vision_ai"])
    rule_findings = len(all_findings) - vision_findings
    high = len([f for f in all_findings if f.risk_level == "high"])
    medium = len([f for f in all_findings if f.risk_level == "medium"])
    low = len([f for f in all_findings if f.risk_level == "low"])
    run.status = "completed"
    run.completed_at = datetime.now()
    run.total_findings = len(all_findings)
    run.high_count = high
    run.medium_count = medium
    run.low_count = low
    run.warnings = _json_dumps(warnings)
    if ai_attempted:
        run.summary = (
            f"Đã kiểm tra {len(logs)} bản ghi. Vision AI phân tích {ai_stats.get('reviewed', 0)}/{ai_stats.get('eligible', 0)} ảnh, "
            f"lưu {vision_findings} kết quả AI. Rule chỉ dùng để ưu tiên, không thay thế quyết định AI."
        )
    else:
        run.summary = f"Đã kiểm tra {len(logs)} bản ghi nhưng chưa cấu hình Vision AI; không tạo kết quả rule thay thế."
    run.source = "heuristic_ai" if ai_attempted else "heuristic"
    db.commit()
    return _audit_run_to_dict(run)


def _audit_run_to_dict(run: AttendanceAuditRun) -> dict:
    return {
        "id": run.id,
        "run_type": run.run_type,
        "from_date": run.from_date.isoformat() if run.from_date else None,
        "to_date": run.to_date.isoformat() if run.to_date else None,
        "emp_code": run.emp_code or "",
        "status": run.status,
        "source": run.source,
        "summary": run.summary or "",
        "warnings": _json_loads(run.warnings, []),
        "total_findings": run.total_findings,
        "high_count": run.high_count,
        "medium_count": run.medium_count,
        "low_count": run.low_count,
        "created_by": run.created_by or "",
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


def get_audit_run(db: Session, run_id: int) -> dict | None:
    run = db.query(AttendanceAuditRun).filter_by(id=run_id).first()
    return _audit_run_to_dict(run) if run else None


def _run_optional_vision_ai(
    db: Session,
    run: AttendanceAuditRun,
    logs: list[AttendanceLog],
    configs: list[dict] | None = None,
) -> dict:
    configs = configs if configs is not None else get_ai_provider_runtime_configs(db)
    if not configs:
        return {
            "warnings": ["Vision AI chưa cấu hình API key; không tạo kết quả rule thay thế."],
            "reviewed": 0,
            "findings": 0,
            "eligible": 0,
        }
    max_images = max(1, int(getattr(settings, "AI_AUDIT_MAX_IMAGES_PER_RUN", 120)))
    candidates = logs[:max_images]
    warnings: list[str] = []
    ai_count = 0
    ai_findings = 0
    eligible = 0
    for log in candidates:
        evidence = _ensure_image_evidence(db, log)
        if not evidence or not evidence.image_path or not _safe_path(evidence.image_path):
            continue
        eligible += 1
        try:
            result = _call_vision_provider(configs[0], log, evidence)
        except Exception as exc:
            warnings.append(f"{configs[0]['provider']} không audit được log #{log.id}: {str(exc)[:160]}")
            break
        if not result:
            continue
        score = float(result.get("risk_score") or 0.0)
        reasons = [str(r) for r in result.get("reasons", [])]
        if not reasons and score < 0.20:
            reasons = ["ai_reviewed_clear"]
        metrics = {
            "ai": result,
            "provider": configs[0]["provider"],
            "model": configs[0]["model"],
            "confidence": float(log.confidence or 0.0),
            "ai_reviewed": True,
            "candidate_rank": ai_count + 1,
            "max_images_per_run": max_images,
        }
        ai_count += 1
        finding = _upsert_finding(
            db,
            log,
            _matching_event(db, log),
            evidence,
            score,
            reasons,
            metrics,
            "vision_ai",
            run.id,
            persist_clear=True,
        )
        if finding:
            ai_findings += 1
    return {"warnings": warnings, "reviewed": ai_count, "findings": ai_findings, "eligible": eligible}


def _call_vision_provider(provider: dict, log: AttendanceLog, evidence: AttendanceEvidence) -> dict:
    image_path = Path(evidence.image_path)
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    context = {
        "log_id": log.id,
        "emp_code": log.emp_code,
        "employee_name": log.emp_name,
        "check_type": log.check_type,
        "timestamp": log.timestamp.isoformat(),
        "confidence": log.confidence,
    }
    instruction = (
        "Bạn là AI thị giác hỗ trợ quản lý đối soát chấm công. Hãy kiểm tra kỹ ảnh bằng chứng "
        "để tìm dấu hiệu: khuôn mặt hiển thị trên điện thoại/laptop, "
        "ảnh giấy, viền màn hình, glare/moire/screen texture, chuyển động phẳng/đồng nhất, crop bất thường, "
        "hoặc ngữ cảnh không giống người đứng trực tiếp trước camera. Không kết luận gian lận tuyệt đối; "
        "chỉ đưa mức rủi ro để quản lý review. Trả về JSON hợp lệ với các trường: "
        "risk_score number 0..1, reasons array string, visual_observations array string, "
        "recommendation string, note string. Nếu không thấy dấu hiệu rõ, risk_score <= 0.19."
    )
    if provider["provider"] == "openai":
        content = [
            {"type": "input_text", "text": instruction + "\nDữ liệu:\n" + json.dumps(context, ensure_ascii=False)},
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_b64}"},
        ]
        payload = {
            "model": provider["model"],
            "input": [{
                "role": "user",
                "content": content,
            }],
            "text": {"format": {"type": "json_object"}},
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as res:
                data = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(exc.read().decode("utf-8", errors="ignore")[:300]) from exc
        text = data.get("output_text") or ""
        return json.loads(text) if text else {}

    if provider["provider"] == "gemini":
        parts = [
            {"text": instruction + "\nDữ liệu:\n" + json.dumps(context, ensure_ascii=False)},
            {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
        ]
        payload = {
            "contents": [{
                "role": "user",
                "parts": parts,
            }],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{provider['model']}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={"x-goog-api-key": provider["api_key"], "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as res:
                data = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(exc.read().decode("utf-8", errors="ignore")[:300]) from exc
        for candidate in data.get("candidates", []) or []:
            for part in (candidate.get("content") or {}).get("parts", []) or []:
                if "text" in part:
                    return json.loads(part["text"])
    return {}


def cleanup_old_evidence(retention_days: int | None = None) -> dict:
    days = retention_days or settings.EVIDENCE_RETENTION_DAYS
    cutoff = datetime.now() - timedelta(days=max(1, days))
    root = settings.CAPTURES_DIR.resolve()
    deleted = 0
    db = SessionLocal()
    try:
        rows = db.query(AttendanceEvidence).filter(
            AttendanceEvidence.files_available == True,
            AttendanceEvidence.captured_at < cutoff,
        ).all()
        for evidence in rows:
            if evidence.image_path:
                try:
                    path = Path(evidence.image_path).resolve()
                    path.relative_to(root)
                    if path.is_file():
                        path.unlink()
                        deleted += 1
                except (OSError, ValueError):
                    pass
            evidence.files_available = False
            evidence.deleted_at = datetime.now()
        db.commit()
        return {"deleted_files": deleted, "evidence_rows": len(rows), "retention_days": days}
    except Exception as exc:
        db.rollback()
        print(f"  ✗ cleanup_old_evidence lỗi: {exc}")
        return {"deleted_files": deleted, "evidence_rows": 0, "retention_days": days}
    finally:
        db.close()
