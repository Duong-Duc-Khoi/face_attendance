# FaceAttend - He thong cham cong nhan dien khuon mat


<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=for-the-badge&logo=python" />
  <img src="https://img.shields.io/badge/FastAPI-0.104+-green?style=for-the-badge&logo=fastapi" />
  <img src="https://img.shields.io/badge/InsightFace-ArcFace-purple?style=for-the-badge" />
  <img src="https://img.shields.io/badge/OpenCV-4.8+-red?style=for-the-badge&logo=opencv" />
  <img src="https://img.shields.io/badge/PostgreSQL-Database-blue?style=for-the-badge&logo=postgresql" />
</p>

FaceAttend la ung dung cham cong thoi gian thuc cho nhan vien, su dung FastAPI, PostgreSQL, OpenCV va InsightFace. He thong co kiosk nhan dien khuon mat qua webcam, quan ly nhan vien, tai khoan nguoi dung, ca lam viec, lich lam viec, don nghi phep, bao cao cham cong va audit bang chung cham cong.

## Tinh nang chinh

- Kiosk cham cong bang webcam voi luong MJPEG va WebSocket.
- Nhan dien khuon mat bang InsightFace/ArcFace, luu embedding theo ma nhan vien.
- Tu dong check-in/check-out, cooldown tranh cham lap, tinh di muon/ve som theo ca.
- Quan ly nhan vien, anh khuon mat, trang thai dang lam/nghi viec.
- Xac thuc bang email, mat khau, OTP, refresh token va phan quyen `admin`, `manager`, `staff`.
- Duyet tai khoan moi sau khi nguoi dung xac minh email.
- Quan ly ca lam, phan cong ca don le/hang loat, lich lam viec va ngay dac biet.
- Lap nhap phan ca bang AI neu cau hinh OpenAI/Gemini; fallback bang thuat toan heuristic.
- Don nghi phep/remote, duyet/tu choi/huy don va thong bao email.
- Bao cao cham cong, thong ke theo ngay/khoang ngay, xuat Excel.
- Bang chung anh cham cong, audit diem rui ro va luong review cho manager/admin.
- Scheduler tu dong: bao cao ngay 18:00, auto checkout moi 15 phut, don dep bang chung qua han luc 02:30.

## Cong nghe

| Thanh phan | Cong nghe |
| --- | --- |
| Backend | FastAPI, Uvicorn |
| Database | PostgreSQL, SQLAlchemy 2.x |
| AI nhan dien | InsightFace, ONNX Runtime |
| Camera | OpenCV, MJPEG stream, WebSocket |
| Auth | PyJWT, bcrypt, OTP qua email |
| Frontend | Jinja2 templates, vanilla JavaScript, CSS rieng theo man hinh |
| Bao cao | OpenPyXL |
| Scheduler | APScheduler |
| Tich hop AI | OpenAI Responses API, Google Gemini API |

## Yeu cau

- Python 3.10+.
- PostgreSQL 14+.
- Webcam USB hoac camera tich hop.
- Windows/Linux/macOS. Tren Windows, `run.py` co toi uu timer cho MJPEG.
- RAM khuyen nghi toi thieu 8 GB khi chay model `buffalo_l`.

## Cai dat nhanh

1. Tao va kich hoat moi truong ao:

```powershell
python -m venv venv
.\venv\Scripts\activate
```

Tren Linux/macOS:

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
