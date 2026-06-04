# FaceAttend Web Host

Web host là FastAPI app riêng chạy ở `:5600`. App này render các trang quản lý/nhân viên từ `templates/`, mount `static/` và proxy HTTP `/api/*`, `/auth/*`, `/data/*` về backend thật ở `:8000`.

Business logic, database, auth, scheduler, camera backend và WebSocket vẫn nằm trong backend `app.main`.

## Cách chạy

Chạy backend trước:

```powershell
cd G:\face_attendance
.\venv\Scripts\activate
python run.py
```

Chạy web host:

```powershell
cd G:\face_attendance
.\venv\Scripts\activate
python run_web.py
```

Mở:

```text
http://127.0.0.1:5600/auth/login-page
http://127.0.0.1:5600/dashboard
http://127.0.0.1:5600/me
```

Trang đăng ký khuôn mặt không render ở web host. Route `/register` sẽ chuyển sang kiosk:

```text
http://127.0.0.1:5500/register
```

## Biến môi trường

| Biến | Mặc định | Mô tả |
| --- | --- | --- |
| `WEB_HOST` | `127.0.0.1` | Host lắng nghe của web host |
| `WEB_PORT` | `5600` | Cổng web host |
| `BACKEND_URL` | `http://127.0.0.1:8000` | Backend để proxy API/Auth/Data |
| `KIOSK_URL` | `http://127.0.0.1:5500` | Kiosk để mở chấm công/đăng ký khuôn mặt |

Chạy với URL khác:

```powershell
cd G:\face_attendance
$env:BACKEND_URL="http://127.0.0.1:8000"
$env:KIOSK_URL="http://127.0.0.1:5500"
python run_web.py
```

## Luồng proxy

- `GET /dashboard`, `/attendance`, `/employees`, `/leave`, `/roster`, `/work-calendar`, `/report`, `/users`, `/settings`, `/shifts`, `/integrations` render Jinja template.
- `/api/*`, `/auth/*`, `/data/*` được proxy nguyên method/body/header về `BACKEND_URL`.
- Web host không proxy WebSocket. Kiosk kết nối WebSocket trực tiếp về backend qua `BACKEND_URL`.
- Nếu chạy web host ở origin khác, thêm origin đó vào `CORS_ORIGINS` của backend.

Backend `:8000` vẫn giữ redirect legacy cho các route trang cũ. Người dùng nên vào web quản lý qua `:5600`.
