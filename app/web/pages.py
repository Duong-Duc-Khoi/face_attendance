"""Legacy page redirects for the API backend.

The management UI is served by `run_web.py` on a separate port. Keep these
routes only as compatibility redirects so port 8000 behaves like an API host.
"""

import os

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["pages"])
WEB_URL = os.getenv("WEB_URL", "http://127.0.0.1:5600").rstrip("/")
KIOSK_URL = os.getenv("KIOSK_URL", "http://127.0.0.1:5500").rstrip("/")


def _redirect_to_web(path: str, status_code: int = 307):
    return RedirectResponse(f"{WEB_URL}{path}", status_code=status_code)


@router.get("/auth/login-page")
async def login_page(request: Request):
    suffix = f"?{request.url.query}" if request.url.query else ""
    return _redirect_to_web(f"/auth/login-page{suffix}")


@router.get("/")
async def api_root():
    return {
        "service": "FaceAttend API",
        "docs": "/docs",
        "health": "/api/health",
        "kiosk": f"{KIOSK_URL}/",
        "web": f"{WEB_URL}/dashboard",
    }


@router.get("/me")
async def me_page(request: Request):
    return _redirect_to_web("/me")


@router.get("/register")
async def register_page_face(request: Request):
    return _redirect_to_web("/register")


@router.get("/dashboard")
async def dashboard_page(request: Request):
    legacy_tab = request.query_params.get("tab")
    legacy_routes = {
        "attendance": "/attendance",
        "employees": "/employees",
        "leave": "/leave",
        "calendar": "/work-calendar",
    }
    if legacy_tab in legacy_routes:
        return _redirect_to_web(legacy_routes[legacy_tab])
    return _redirect_to_web("/dashboard")


@router.get("/attendance")
async def attendance_page(request: Request):
    return _redirect_to_web("/attendance")


@router.get("/employees")
async def employees_page(request: Request):
    return _redirect_to_web("/employees")


@router.get("/branches")
async def branches_page(request: Request):
    return _redirect_to_web("/branches")


@router.get("/leave")
async def leave_page(request: Request):
    return _redirect_to_web("/leave")


@router.get("/roster")
async def roster_page(request: Request):
    return _redirect_to_web("/roster")


@router.get("/work-calendar")
async def work_calendar_page(request: Request):
    return _redirect_to_web("/work-calendar")


@router.get("/calendar")
async def calendar_page():
    return _redirect_to_web("/work-calendar")


@router.get("/report")
async def report_page(request: Request):
    return _redirect_to_web("/report")


@router.get("/users")
async def users_page(request: Request):
    return _redirect_to_web("/users")


@router.get("/settings")
async def settings_page(request: Request):
    return _redirect_to_web("/settings")


@router.get("/shifts")
async def shifts_page(request: Request):
    return _redirect_to_web("/shifts")


@router.get("/integrations")
async def integrations_page(request: Request):
    return _redirect_to_web("/integrations")
