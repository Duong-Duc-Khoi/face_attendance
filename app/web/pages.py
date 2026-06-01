"""Server-rendered HTML pages.

Frontend hien tai dung Jinja template. Dat cac page route o day giup kien truc
tach bach hon: `app/api` cho JSON/WebSocket API, `app/web` cho man hinh HTML.
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.attendance import get_summary_today

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="templates")


@router.get("/auth/login-page")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/")
async def kiosk_page(request: Request):
    return templates.TemplateResponse("kiosk.html", {"request": request})


@router.get("/me")
async def me_page(request: Request):
    return templates.TemplateResponse("me.html", {"request": request})


@router.get("/register")
async def register_page_face(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


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
        return RedirectResponse(legacy_routes[legacy_tab], status_code=307)
    summary = get_summary_today()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "summary": summary, "active_page": "overview"},
    )


@router.get("/attendance")
async def attendance_page(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "summary": {}, "active_page": "attendance"},
    )


@router.get("/employees")
async def employees_page(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "summary": {}, "active_page": "employees"},
    )


@router.get("/branches")
async def branches_page(request: Request):
    return templates.TemplateResponse("branches.html", {"request": request})


@router.get("/leave")
async def leave_page(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "summary": {}, "active_page": "leave"},
    )


@router.get("/roster")
async def roster_page(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "summary": {}, "active_page": "roster"},
    )


@router.get("/work-calendar")
async def work_calendar_page(request: Request):
    return templates.TemplateResponse("work_calendar.html", {"request": request})


@router.get("/calendar")
async def calendar_page():
    return RedirectResponse("/work-calendar", status_code=307)


@router.get("/report")
async def report_page(request: Request):
    return templates.TemplateResponse("reports.html", {"request": request})


@router.get("/users")
async def users_page(request: Request):
    return templates.TemplateResponse("users.html", {"request": request})


@router.get("/shifts")
async def shifts_page(request: Request):
    return templates.TemplateResponse("shifts.html", {"request": request})


@router.get("/integrations")
async def integrations_page(request: Request):
    return templates.TemplateResponse("integrations.html", {"request": request})
