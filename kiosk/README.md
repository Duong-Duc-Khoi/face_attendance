# FaceAttend Kiosk Host

Kiosk host là server Node.js nhỏ chạy ở `:5500`, dùng để mở màn hình chấm công và trang đăng ký khuôn mặt trên `localhost`. Browser camera cần `localhost` hoặc HTTPS, nên kiosk được tách khỏi backend/web host.

## Cách chạy

Chạy backend trước:

```powershell
cd G:\face_attendance
.\venv\Scripts\activate
python run.py
```

Chạy kiosk trên cùng máy:

```powershell
cd G:\face_attendance
$env:KIOSK_BRANCH_ID="2"
node kiosk\server.js
```

Mở:

```text
http://127.0.0.1:5500/
http://127.0.0.1:5500/register
```

## Biến môi trường

| Biến | Mặc định | Mô tả |
| --- | --- | --- |
| `KIOSK_HOST` | `127.0.0.1` | Host lắng nghe của kiosk server |
| `KIOSK_PORT` | `5500` | Cổng kiosk |
| `BACKEND_URL` | `http://127.0.0.1:8000` | Backend API/WebSocket mà kiosk gửi frame và request |
| `KIOSK_BRANCH_ID` | rỗng | ID cửa hàng gắn cố định với máy kiosk |

`kiosk/server.js` tự đọc `.env` ở thư mục gốc nếu biến môi trường chưa được set.

## Chạy kiosk trỏ tới backend máy khác

```powershell
cd G:\face_attendance
$env:BACKEND_URL="http://192.168.1.10:8000"
$env:KIOSK_BRANCH_ID="2"
node kiosk\server.js
```

Mở trên máy kiosk:

```text
http://127.0.0.1:5500/
http://127.0.0.1:5500/register
```

Có thể test nhanh bằng URL:

```text
http://127.0.0.1:5500/?branch_id=2
```

Khi lắp máy thật, nên dùng `KIOSK_BRANCH_ID` để khóa chi nhánh cho kiosk thay vì truyền query string.

## Liên kết với backend/web host

- Kiosk gửi API/WebSocket trực tiếp về `BACKEND_URL`.
- Web host `:5600` không proxy WebSocket kiosk.
- Nếu đổi host/cổng kiosk, thêm origin đó vào `CORS_ORIGINS` của backend, ví dụ:

```env
CORS_ORIGINS=http://127.0.0.1:5500,http://127.0.0.1:5600,http://localhost:5500,http://localhost:5600
```

Nếu `KIOSK_BRANCH_ID` chưa có, màn hình kiosk sẽ báo thiếu cấu hình cửa hàng và không cho bật camera chấm công.
