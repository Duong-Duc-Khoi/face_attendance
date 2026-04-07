# FaceAttend — Hệ Thống Chấm Công Nhận Diện Khuôn Mặt

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=for-the-badge&logo=python" />
  <img src="https://img.shields.io/badge/FastAPI-0.104+-green?style=for-the-badge&logo=fastapi" />
  <img src="https://img.shields.io/badge/InsightFace-ArcFace-purple?style=for-the-badge" />
  <img src="https://img.shields.io/badge/OpenCV-4.8+-red?style=for-the-badge&logo=opencv" />
  <img src="https://img.shields.io/badge/PostgreSQL-Database-blue?style=for-the-badge&logo=postgresql" />
</p>

Ứng dụng chấm công thời gian thực sử dụng AI nhận diện khuôn mặt (InsightFace ArcFace), chạy trên phần cứng thông thường (PC + webcam). Nhân viên chỉ cần đứng trước camera — hệ thống tự động nhận diện và ghi nhận thời gian vào/ra trong vòng dưới 1 giây.

---

## Mục Lục

- [Tính năng](#tính-năng)
- [Công nghệ](#công-nghệ)
- [Kiến trúc](#kiến-trúc)
- [Cài đặt](#cài-đặt)
- [Cấu trúc thư mục](#cấu-trúc-thư-mục)
- [API](#api)

---

## Tính Năng

**Chấm công (Kiosk)**
- Nhận diện khuôn mặt thời gian thực qua webcam (MJPEG + WebSocket)
- Tự động phân biệt check-in (lần đầu trong ngày) và check-out
- Phát hiện đi muộn so với giờ làm cấu hình
- Chống chấm công lặp (cooldown 5 phút)
- Thông báo Telegram tức thời khi có sự kiện

**Quản lý nhân viên**
- Đăng ký khuôn mặt từ camera (nhiều ảnh, nhiều góc) hoặc upload file
- CRUD nhân viên: mã NV, tên, phòng ban, chức vụ, email, điện thoại
- Soft delete (vô hiệu hóa, không xóa dữ liệu lịch sử)

**Báo cáo**
- Lịch sử chấm công theo ngày, khoảng thời gian, phòng ban
- Thống kê tổng hợp: đã vào, đã ra, vắng mặt, đi muộn
- Biểu đồ theo ngày và theo phòng ban (Chart.js)
- Xuất báo cáo Excel (.xlsx) có định dạng màu sắc

**Xác thực & Phân quyền**
- Đăng ký tài khoản với xác minh email
- Đăng nhập 2 bước: mật khẩu → OTP gửi qua email
- 3 vai trò: `admin`, `manager`, `staff`
- JWT access token (15 phút) + Refresh token (7 ngày, có thu hồi)
- Phân quyền chi tiết theo từng action (`attendance:read_own`, `employee:write`, v.v.)

---

## Công Nghệ

| Thành phần | Công nghệ |
|---|---|
| AI nhận diện | InsightFace `buffalo_l` (ArcFace 512-dim, cosine similarity) |
| Backend | FastAPI + Uvicorn |
| Camera stream | OpenCV → MJPEG (threading) + WebSocket (recognition) |
| Database | PostgreSQL + SQLAlchemy 2.0 |
| Auth | JWT (PyJWT) + bcrypt (passlib) + OTP email |
| Frontend | Jinja2 + Vanilla JS + Custom CSS (tách file theo component) |
| Thông báo | Gmail SMTP + Telegram Bot API (aiohttp) |
| Xuất báo cáo | OpenPyXL |

---

## Kiến Trúc

```
Browser (localhost:8000)
  ├── GET /              → kiosk.html     (public)
  ├── GET /video_feed    → MJPEG stream
  ├── WS  /ws/attendance → realtime recognition
  ├── GET /dashboard     → quản lý (cần đăng nhập)
  ├── GET /register      → đăng ký nhân viên
  └── GET /report        → báo cáo

FastAPI Backend
  ├── app/main.py        → routes HTML, camera, health
  ├── app/ws.py          → WebSocket handler + ConnectionManager
  ├── app/face_engine.py → InsightFace singleton (load model 1 lần)
  ├── app/camera.py      → CameraStream (3 thread: capture / MJPEG / recognition)
  ├── app/attendance.py  → business logic check-in/out, cooldown, late detection
  ├── app/auth.py        → JWT, OTP, RBAC, email verification
  ├── app/notify.py      → Email SMTP + Telegram
  ├── app/database.py    → SQLAlchemy models (Employee, AttendanceLog, User, ...)
  └── app/routes/
       ├── auth.py       → /auth/* endpoints
       ├── employees.py  → /api/employees/*
       └── reports.py    → /api/attendance, /api/summary, /api/reports/export

Luồng chấm công:
Camera frame (60 FPS capture)
  → MJPEG stream (25 FPS, không AI, dùng cached bbox)
  → WebSocket loop (~1/giây): face_engine.recognize()
      → cosine similarity ≥ 0.50 → process_attendance()
          → lưu AttendanceLog vào PostgreSQL
          → broadcast WebSocket → UI cập nhật
          → telegram_checkin() async
```

---

## Cài Đặt

### Yêu cầu

- Python 3.10+
- PostgreSQL 14+
- Webcam (USB hoặc tích hợp)
- RAM tối thiểu 4GB (khuyến nghị 8GB cho model AI)

### Bước 1 — Clone và tạo môi trường ảo

```bash
git clone https://github.com/your-username/face-attendance.git
cd face-attendance

python -m venv venv

# Windows
venv\Scripts\activate

# Linux / macOS
source venv/bin/activate
```

### Bước 2 — Cài đặt thư viện

```bash
pip install -r requirements.txt
```

### Bước 3 — Tạo database PostgreSQL

```sql
CREATE DATABASE face_attendance;
CREATE USER face_user WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE face_attendance TO face_user;
```

### Bước 4 — Cấu hình môi trường

Tạo file `.env` từ mẫu:

```bash
cp .env.example .env
```

Chỉnh sửa `.env`:

```env
# Database
DATABASE_URL=postgresql+psycopg2://face_user:your_password@localhost:5432/face_attendance

# JWT
JWT_SECRET=your-32-character-secret-key-here

# Camera
CAMERA_ID=0
FACE_THRESHOLD=0.50
COOLDOWN_MINUTES=5
WORK_START=08:30

# Email (Gmail, cần bật App Password)
EMAIL_USER=your-email@gmail.com
EMAIL_PASSWORD=your-app-password
EMAIL_HOST=smtp.gmail.com

# Telegram (tuỳ chọn)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### Bước 5 — Chạy ứng dụng

```bash
python run.py
```

Ứng dụng tự động:
- Tạo các bảng database nếu chưa có
- Tạo thư mục `data/faces`, `data/captures`, `data/exports`
- Load model InsightFace lần đầu (tải ~300MB nếu chưa có)

Truy cập: **http://localhost:8000**

---

## Cấu Trúc Thư Mục

```
face_attendance/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI app, page routes, camera endpoints
│   ├── ws.py              # WebSocket handler, ConnectionManager
│   ├── face_engine.py     # InsightFace singleton, register & recognize
│   ├── camera.py          # CameraStream (3-thread: capture/MJPEG/recognition)
│   ├── attendance.py      # Business logic check-in/out, late detection
│   ├── database.py        # SQLAlchemy models: Employee, AttendanceLog
│   ├── auth.py            # JWT, bcrypt, OTP, RBAC, email verification
│   ├── notify.py          # Gmail SMTP + Telegram notifications
│   └── routes/
│       ├── __init__.py
│       ├── auth.py        # POST /auth/register, login, OTP, refresh...
│       ├── employees.py   # GET/POST/PUT/DELETE /api/employees
│       └── reports.py     # GET /api/attendance, /api/summary, /api/reports/export
├── templates/
│   ├── kiosk.html         # Màn hình chấm công (public)
│   ├── dashboard.html     # Quản lý nhân viên + lịch sử
│   ├── register.html      # Đăng ký khuôn mặt nhân viên
│   ├── reports.html       # Báo cáo & xuất Excel
│   ├── login.html         # Đăng nhập (email + OTP 2 bước)
│   └── user_register.html # Tạo tài khoản quản lý
├── static/
│   ├── css/
│   │   ├── base.css        # CSS variables, reset, grid background
│   │   ├── nav.css         # Header, brand, navigation
│   │   ├── components.css  # Panel, table, badge, button, toast, spinner
│   │   ├── auth.css        # Layout auth, card, form fields
│   │   ├── dashboard.css   # Stats, modal, employee cell
│   │   ├── reports.css     # Charts, filter row, quick buttons
│   │   ├── login.css       # Step tabs, OTP input boxes
│   │   └── user_register.css # Password strength, success screen
│   ├── js/
│   │   └── toast.js        # showToast() dùng chung
│   └── sounds/             # Âm thanh thông báo chấm công
├── data/
│   ├── embeddings.pkl      # Face embeddings (tự động tạo khi đăng ký)
│   ├── faces/              # Ảnh đăng ký theo {emp_code}/
│   ├── captures/           # Ảnh chụp lúc chấm công (bằng chứng)
│   └── exports/            # File Excel xuất ra
├── .env                    # Cấu hình (không commit)
├── .env.example            # Mẫu cấu hình
├── requirements.txt
├── run.py                  # Entry point (uvicorn)
└── Readme.md
```

---

## API

Swagger UI đầy đủ tại **http://localhost:8000/docs**

**Auth**

| Method | Endpoint | Mô tả |
|---|---|---|
| `POST` | `/auth/register` | Tạo tài khoản mới |
| `GET` | `/auth/verify-email` | Xác minh email qua link |
| `POST` | `/auth/login` | Gửi OTP đến email |
| `POST` | `/auth/login/verify-otp` | Xác nhận OTP → trả JWT |
| `POST` | `/auth/refresh` | Lấy access token mới |
| `POST` | `/auth/logout` | Thu hồi refresh token |
| `GET` | `/auth/me` | Thông tin user hiện tại |

**Nhân viên**

| Method | Endpoint | Mô tả |
|---|---|---|
| `GET` | `/api/employees` | Danh sách nhân viên |
| `POST` | `/api/employees` | Tạo mới + đăng ký khuôn mặt (multipart) |
| `PUT` | `/api/employees/{id}` | Cập nhật thông tin |
| `DELETE` | `/api/employees/{id}` | Vô hiệu hóa (soft delete) |

**Chấm công & Báo cáo**

| Method | Endpoint | Mô tả |
|---|---|---|
| `GET` | `/api/attendance` | Lịch sử chấm công (query: `date`, `days`) |
| `GET` | `/api/summary` | Thống kê hôm nay |
| `GET` | `/api/summary/range` | Thống kê theo khoảng thời gian |
| `GET` | `/api/reports/export` | Xuất Excel (query: `from_date`, `to_date`) |

**Camera**

| Method | Endpoint | Mô tả |
|---|---|---|
| `GET` | `/video_feed` | MJPEG stream |
| `WS` | `/ws/attendance` | WebSocket nhận diện realtime |
| `POST` | `/api/camera/start` | Bật camera |
| `POST` | `/api/camera/stop` | Tắt camera |
| `GET` | `/api/camera/status` | Trạng thái camera |

---

## Lưu Ý Vận Hành

- **Camera lần đầu:** Model InsightFace `buffalo_l` (~300MB) sẽ tự tải về `~/.insightface/` lần đầu chạy
- **GPU:** Nếu có CUDA, hệ thống tự dùng GPU; nếu không, fallback sang CPU (chậm hơn ~3x)
- **Ánh sáng:** Đặt camera ở vị trí đủ sáng, ngang tầm mặt để đạt độ chính xác tối đa
- **Ngưỡng nhận diện:** `FACE_THRESHOLD=0.50` — tăng lên nếu nhận nhầm người, giảm xuống nếu không nhận ra
- **Gmail OTP:** Cần bật [App Password](https://myaccount.google.com/apppasswords) trong tài khoản Google (không dùng mật khẩu thường)
