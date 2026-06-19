# FaceAttend - He thong cham cong nhan dien khuon mat


<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=for-the-badge&logo=python" />
  <img src="https://img.shields.io/badge/FastAPI-0.104+-green?style=for-the-badge&logo=fastapi" />
  <img src="https://img.shields.io/badge/InsightFace-ArcFace-purple?style=for-the-badge" />
  <img src="https://img.shields.io/badge/OpenCV-4.8+-red?style=for-the-badge&logo=opencv" />
  <img src="https://img.shields.io/badge/PostgreSQL-Database-blue?style=for-the-badge&logo=postgresql" />
</p>

FaceAttend la ung dung cham cong thoi gian thuc cho nhan vien, su dung FastAPI, PostgreSQL, OpenCV va InsightFace. He thong co kiosk nhan dien khuon mat qua webcam, quan ly nhan vien, tai khoan nguoi dung, ca lam viec, lich lam viec, don nghi phep, bao cao cham cong va audit bang chung cham cong.

## Tính năng chính

- Kiosk chấm công bằng webcam, gửi frame qua WebSocket về backend.
- Đăng ký và cập nhật ảnh khuôn mặt nhân viên từ camera kiosk.
- Nhận diện khuôn mặt bằng InsightFace/ArcFace, lưu embedding theo mã nhân viên.
- Tự động check-in/check-out, cooldown chống chấm lặp, tính đi muộn/về sớm theo ca.
- Quản lý nhân viên, tài khoản, vai trò hệ thống, cửa hàng/chi nhánh và lịch sử chuyển chi nhánh.
- Quản lý ca làm, phân ca lẻ/hàng loạt, import/export Excel, lịch vận hành và ngày đặc biệt.
- Đơn nghỉ phép/remote, duyệt/từ chối/hủy đơn và thông báo email.
- Báo cáo chấm công, thống kê theo ngày/khoảng ngày và xuất Excel.
- Lưu ảnh bằng chứng chấm công, audit rủi ro và luồng review cho manager/admin.
- AI đề xuất phân ca nếu cấu hình OpenAI/Gemini; nếu không có khóa API sẽ dùng heuristic.
- Scheduler backend: báo cáo ngày 18:00, auto checkout mỗi 15 phút, dọn bằng chứng quá hạn lúc 02:30.

## Kiến trúc chạy local

FaceAttend đang tách 3 tiến trình:

| Cổng | Thành phần | Lệnh chạy | Vai trò |
| --- | --- | --- | --- |
| `8000` | Backend FastAPI | `python run.py` | API/Auth/WebSocket, database, camera backend, scheduler |
| `5600` | Web host FastAPI | `python run_web.py` | Render giao diện quản lý/nhân viên và proxy HTTP về backend |
| `5500` | Kiosk Node.js | `node kiosk/server.js` | Host màn hình kiosk và đăng ký khuôn mặt trên `localhost` |

Backend `:8000` vẫn giữ các route trang cũ dưới dạng redirect sang web/kiosk để tương thích link cũ. Giao diện quản lý nên truy cập qua `:5600`, kiosk nên truy cập qua `:5500`.

## Công nghệ

| Thành phần | Công nghệ |
| --- | --- |
| Backend | FastAPI, Uvicorn |
| Database | PostgreSQL, SQLAlchemy 2.x |
| AI nhận diện | InsightFace, ONNX Runtime |
| Camera | OpenCV, browser camera, MJPEG/WebSocket |
| Auth | PyJWT, bcrypt, OTP qua email |
| Frontend | Jinja2 templates, vanilla JavaScript, CSS |
| Web host proxy | FastAPI, HTTPX |
| Báo cáo | OpenPyXL |
| Scheduler | APScheduler |
| AI planning/audit | OpenAI, Google Gemini |

## Yêu cầu

- Python 3.10+.
- Node.js 18+ để chạy kiosk host.
- PostgreSQL 14+.
- Webcam USB hoặc camera tích hợp.
- RAM khuyến nghị tối thiểu 8 GB khi chạy model InsightFace `buffalo_l`.
- Trên Windows, `run.py` tối ưu timer cho luồng MJPEG/camera.

## Cài đặt

Tạo môi trường Python:

```powershell
cd G:\face_attendance
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

Tạo database PostgreSQL:

```sql
CREATE DATABASE face_attendance;
CREATE USER face_user WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE face_attendance TO face_user;
```

Tạo file cấu hình:

```powershell
Copy-Item .env.example .env
```

Cập nhật các biến chính trong `.env`:

```env
DB_USER=face_user
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5432
DB_NAME=face_attendance

JWT_SECRET=change_this_to_a_random_32_char_string

BASE_URL=http://127.0.0.1:5600
BACKEND_URL=http://127.0.0.1:8000
WEB_URL=http://127.0.0.1:5600
KIOSK_URL=http://127.0.0.1:5500
KIOSK_BRANCH_ID=2
CORS_ORIGINS=http://127.0.0.1:5500,http://127.0.0.1:5600,http://localhost:5500,http://localhost:5600

EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USER=your@gmail.com
EMAIL_PASSWORD=app_password_here
EMAIL_TO=admin@company.com
DAILY_REPORT_HOUR=18
DAILY_REPORT_MINUTE=0
LOGIN_OTP_ENABLED=true
CONSECUTIVE_SHIFT_GAP_MINUTES=30
```

`KIOSK_BRANCH_ID` là ID chi nhánh/cửa hàng gắn cố định với máy kiosk. Nếu chưa cấu hình, màn hình kiosk sẽ báo thiếu cấu hình và không cho bật camera chấm công.

Chạy migration khi triển khai database thật:

```powershell
cd G:\face_attendance
.\venv\Scripts\activate
alembic upgrade head
```

`Base.metadata.create_all()` vẫn hỗ trợ chạy local/dev, nhưng môi trường production nên dùng Alembic để cập nhật schema.

## Chạy ứng dụng

Mở 3 terminal riêng.

Backend API/Auth/WebSocket:

```powershell
cd G:\face_attendance
.\venv\Scripts\activate
python run.py
```

Web quản lý/nhân viên:

```powershell
cd G:\face_attendance
.\venv\Scripts\activate
python run_web.py
```

Kiosk chấm công và đăng ký khuôn mặt:

```powershell
cd G:\face_attendance
$env:KIOSK_BRANCH_ID="2"
node kiosk\server.js
```

Truy cập:

- Backend/API docs: `http://127.0.0.1:8000/docs`
- Web quản lý: `http://127.0.0.1:5600/dashboard`
- Đăng nhập: `http://127.0.0.1:5600/auth/login-page`
- Nhân viên tự xem lịch: `http://127.0.0.1:5600/me`
- Kiosk chấm công: `http://127.0.0.1:5500/`
- Đăng ký khuôn mặt: `http://127.0.0.1:5500/register`

Khi backend khởi động, hệ thống sẽ tạo bảng nếu chưa có, seed vai trò/ca mặc định, tạo thư mục `data/faces`, `data/captures`, `data/exports` và load model InsightFace. Lần đầu chạy model có thể tải dữ liệu về thư mục cache của InsightFace.

## Cấu trúc thư mục

```text
face_attendance/
├── app/
│   ├── main.py              # FastAPI backend API/Auth/WebSocket
│   ├── api/v1/              # Routers API: auth, employees, reports, shifts, leave...
│   ├── core/                # Config, database, security, lifespan
│   ├── models/              # SQLAlchemy models
│   ├── schemas/             # Pydantic schemas và normalize helpers
│   ├── services/            # Business logic: attendance, face, camera, shift, notify...
│   └── web/                 # Legacy page redirects từ backend sang web/kiosk host
├── templates/               # Jinja pages dùng bởi web host và kiosk host
│   └── dashboard_tabs/      # Các tab con của dashboard
├── static/
│   ├── css/                 # CSS theo layout/màn hình
│   └── js/                  # auth guard, toast
├── kiosk/
│   ├── server.js            # Node static host kiosk ở :5500
│   └── README.md
├── web_host/
│   ├── server.py            # FastAPI web host ở :5600
│   └── README.md
├── data/                    # Runtime data, không commit
│   ├── embeddings.pkl
│   ├── faces/
│   ├── captures/
│   └── exports/
├── .env.example
├── requirements.txt
├── run.py
├── run_web.py
└── Readme.md
```

## API chính

Swagger UI đầy đủ ở `http://127.0.0.1:8000/docs`.

| Nhóm | Endpoint chính |
| --- | --- |
| Auth | `/auth/register`, `/auth/login`, `/auth/login/verify-otp`, `/auth/refresh`, `/auth/me` |
| Người dùng | `/api/users`, `/api/users/pending`, `/api/users/{id}/approve` |
| Nhân viên | `/api/employees`, `/api/employees/{id}`, `/api/employees/{id}/face` |
| Cửa hàng | `/api/branches`, `/api/branches/public` |
| Ca làm | `/api/shifts`, `/api/shifts/assignments`, `/api/shifts/assignments/import` |
| Lịch vận hành | `/api/calendar`, `/api/calendar/config` |
| Nghỉ phép | `/api/leave`, `/api/leave/pending-count` |
| Báo cáo | `/api/attendance`, `/api/summary`, `/api/reports/export` |
| Khóa kỳ công | `/api/attendance/period-locks` |
| Camera | `/video_feed`, `/api/camera/start`, `/api/camera/stop`, `/api/camera/status` |
| Realtime | `/ws/attendance`, `/ws/kiosk` |
| Cấu hình | `/api/health`, `/api/config`, `/api/integrations/ai-keys` |

## Biến môi trường quan trọng

| Biến | Mô tả |
| --- | --- |
| `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_NAME` | Kết nối PostgreSQL |
| `JWT_SECRET`, `ACCESS_TOKEN_EXP`, `REFRESH_TOKEN_EXP`, `OTP_EXP_MINUTES`, `LOGIN_OTP_ENABLED` | Xác thực, phiên đăng nhập và bật/tắt OTP đăng nhập |
| `BASE_URL` | URL web dùng trong email xác minh/khôi phục mật khẩu |
| `BACKEND_URL` | Backend thật để web host/kiosk proxy hoặc gọi API |
| `WEB_URL` | URL web host dùng cho redirect legacy từ backend |
| `KIOSK_URL` | URL kiosk dùng cho link đăng ký và redirect |
| `KIOSK_BRANCH_ID` | ID cửa hàng gắn với máy kiosk |
| `CORS_ORIGINS` | Origin web/kiosk được phép gọi backend |
| `CAMERA_ID` | Camera backend mặc định cho mode legacy |
| `FACE_THRESHOLD`, `MIN_FACE_SIZE` | Ngưỡng nhận diện và kích thước mặt tối thiểu |
| `PRESENTATION_GUARD_*` | Cấu hình chống giả mạo/bằng chứng review |
| `CONSECUTIVE_SHIFT_GAP_MINUTES` | Khoảng nghỉ tối đa giữa hai ca để tính là liên ca |
| `OPENAI_API_KEY`, `GEMINI_API_KEY` | Tùy chọn để bật AI phân ca/audit |
| `EMAIL_*` | SMTP gửi OTP, xác minh email và thông báo; `EMAIL_TO` là fallback khi chi nhánh chưa có quản lý nhận mail |
| `DAILY_REPORT_HOUR`, `DAILY_REPORT_MINUTE` | Giờ gửi báo cáo cuối ngày theo từng chi nhánh |

## Lưu ý vận hành

- Browser chỉ cho phép camera trên `localhost` hoặc HTTPS, nên kiosk nên host local ở `127.0.0.1:5500`.
- Nếu chạy web/kiosk ở domain hoặc cổng khác, cập nhật `CORS_ORIGINS` ở backend.
- Nếu nhận diện nhầm, tăng `FACE_THRESHOLD`; nếu khó nhận ra, giảm nhẹ và kiểm tra ánh sáng/camera.
- Dữ liệu người dùng nằm trong `data/` và `.env`; các file này đã được ignore, không nên commit.
- Không cài đồng thời `opencv-python` và `opencv-python-headless`.
