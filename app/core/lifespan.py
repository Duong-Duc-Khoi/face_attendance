"""Application startup/shutdown orchestration.

Module nay gom cac tac vu van hanh cua ung dung: khoi tao DB, scheduler va
giai phong camera khi tat app. `main.py` chi can gan lifespan vao FastAPI.
"""

from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from app.core.database import init_db
from app.services.attendance import (
    auto_checkout_missing,
    get_summary_today,
    mark_absent_sessions,
)
from app.services.attendance_audit import cleanup_old_evidence
from app.services.camera import release_camera
from app.services.face_engine import face_engine
from app.services.notify import notify_daily_report_async

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n  FaceAttend - Khoi dong he thong")
    init_db()
    print("  [ok] Database san sang")
    print(f"  [ok] Face engine: {face_engine.registered_count} nhan vien da dang ky")
    print("  [ok] Camera se mo khi co nguoi truy cap")

    async def _daily_report():
        summary = get_summary_today()
        await notify_daily_report_async(summary)

    async def _auto_checkout():
        count = auto_checkout_missing()
        absent_count = mark_absent_sessions()
        print(f"  [ok] Auto checkout: {count} nhan vien chua check out; vang cho duyet: {absent_count}")

    async def _cleanup_evidence():
        result = cleanup_old_evidence()
        print(f"  [ok] Evidence retention: xoa {result['deleted_files']} file qua {result['retention_days']} ngay")

    scheduler.add_job(_daily_report, CronTrigger(hour=18, minute=0),
                      id="daily_report", replace_existing=True)
    scheduler.add_job(_auto_checkout, IntervalTrigger(minutes=15),
                      id="auto_checkout", replace_existing=True)
    scheduler.add_job(_cleanup_evidence, CronTrigger(hour=2, minute=30),
                      id="evidence_retention", replace_existing=True)
    scheduler.start()
    print("  [ok] Scheduler bat - bao cao 18:00, auto checkout moi 15 phut, retention evidence 02:30")
    yield
    scheduler.shutdown(wait=False)
    release_camera()
    print("  FaceAttend - Da tat")
