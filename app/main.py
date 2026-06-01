"""FastAPI application entry point.

`main.py` chi lap ghep ung dung:
  1. Tao FastAPI app va middleware
  2. Mount static assets
  3. Dang ky API routers va HTML page routers

Business logic, scheduler, camera route va page route nam trong cac module
rieng de so do kien truc ro rang hon cho do an.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1 import employees, reports
from app.api.v1.auth import router as auth_router
from app.api.v1.branches import router as branches_router
from app.api.v1.calendar import router as calendar_router
from app.api.v1.camera import router as camera_router
from app.api.v1.employee_roles import router as employee_roles_router
from app.api.v1.integrations import router as integrations_router
from app.api.v1.leave import router as leave_router
from app.api.v1.shifts import router as shifts_router
from app.api.v1.system import router as system_router
from app.api.v1.users import router as users_router
from app.api.v1.ws import router as realtime_router
from app.core.lifespan import lifespan
from app.web.pages import router as pages_router


app = FastAPI(
    title="FaceAttend API",
    description="He thong cham cong nhan dien khuon mat",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/data/faces", StaticFiles(directory="data/faces"), name="faces")


# API layer
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(employees.router)
app.include_router(employee_roles_router)
app.include_router(branches_router)
app.include_router(reports.router)
app.include_router(leave_router)
app.include_router(calendar_router)
app.include_router(shifts_router)
app.include_router(integrations_router)
app.include_router(camera_router)
app.include_router(system_router)
app.include_router(realtime_router)


# Frontend/page layer
app.include_router(pages_router)
