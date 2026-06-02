"""Dashboard/frontend host separated from the API backend.

This app serves the existing Jinja pages on a different port and proxies
browser API calls to the real FastAPI backend. It is intentionally thin:
business logic stays in `app.main` on the backend port.
"""

import os
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
KIOSK_URL = os.getenv("KIOSK_URL", "http://127.0.0.1:5500").rstrip("/")

app = FastAPI(
    title="FaceAttend Web Host",
    description="Frontend host for dashboard/login pages. Proxies API calls to backend.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


def _template_context(request: Request, **extra):
    return {
        "request": request,
        "backend_url": BACKEND_URL,
        "kiosk_url": KIOSK_URL,
        **extra,
    }


@app.get("/")
async def root():
    return RedirectResponse(KIOSK_URL, status_code=307)


@app.get("/auth/login-page")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", _template_context(request))


@app.get("/me")
async def me_page(request: Request):
    return templates.TemplateResponse("me.html", _template_context(request))


@app.get("/register")
async def register_page_face(request: Request):
    return RedirectResponse(f"{KIOSK_URL}/register", status_code=307)


@app.get("/dashboard")
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
    return templates.TemplateResponse(
        "dashboard.html",
        _template_context(request, summary={}, active_page="overview"),
    )


@app.get("/attendance")
async def attendance_page(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        _template_context(request, summary={}, active_page="attendance"),
    )


@app.get("/employees")
async def employees_page(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        _template_context(request, summary={}, active_page="employees"),
    )


@app.get("/branches")
async def branches_page(request: Request):
    return templates.TemplateResponse("branches.html", _template_context(request))


@app.get("/leave")
async def leave_page(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        _template_context(request, summary={}, active_page="leave"),
    )


@app.get("/roster")
async def roster_page(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        _template_context(request, summary={}, active_page="roster"),
    )


@app.get("/work-calendar")
async def work_calendar_page(request: Request):
    return templates.TemplateResponse("work_calendar.html", _template_context(request))


@app.get("/calendar")
async def calendar_page():
    return RedirectResponse("/work-calendar", status_code=307)


@app.get("/report")
async def report_page(request: Request):
    return templates.TemplateResponse("reports.html", _template_context(request))


@app.get("/users")
async def users_page(request: Request):
    return templates.TemplateResponse("users.html", _template_context(request))


@app.get("/shifts")
async def shifts_page(request: Request):
    return templates.TemplateResponse("shifts.html", _template_context(request))


@app.get("/integrations")
async def integrations_page(request: Request):
    return templates.TemplateResponse("integrations.html", _template_context(request))


async def _proxy(request: Request, prefix: str, path: str):
    target_path = f"/{prefix}/{path}" if path else f"/{prefix}"
    query = f"?{request.url.query}" if request.url.query else ""
    target_url = urljoin(BACKEND_URL + "/", target_path.lstrip("/")) + query
    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }

    try:
        async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
            upstream = await client.request(
                request.method,
                target_url,
                content=body,
                headers=headers,
            )
    except httpx.RequestError:
        return Response(
            content=f"Backend unavailable: {BACKEND_URL}",
            status_code=502,
            media_type="text/plain; charset=utf-8",
        )

    excluded = {
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
    }
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in excluded
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_api(request: Request, path: str):
    return await _proxy(request, "api", path)


@app.api_route("/auth/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_auth(request: Request, path: str):
    return await _proxy(request, "auth", path)


@app.api_route("/data/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_data(request: Request, path: str):
    return await _proxy(request, "data", path)
