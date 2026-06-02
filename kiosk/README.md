# Kiosk Static Host

Host rieng giao dien kiosk bang Node.js, con backend FastAPI chay rieng.

## Chay backend

```powershell
cd G:\face_attendance
conda activate face_attendance
python run.py
```

## Chay kiosk cung may

```powershell
cd G:\face_attendance
node kiosk\server.js
```

Mo:

```text
http://127.0.0.1:5500/
http://127.0.0.1:5500/register
```

Mac dinh kiosk se gui API/WebSocket ve:

```text
http://127.0.0.1:8000
```

## Chay kiosk tro den backend may khac

```powershell
cd G:\face_attendance
$env:BACKEND_URL="http://192.168.1.10:8000"
node kiosk\server.js
```

Mo tren may kiosk:

```text
http://127.0.0.1:5500/
http://127.0.0.1:5500/register
```

Trong do:

- `/` la man hinh cham cong kiosk.
- `/register` la trang dang ky khuon mat bang camera tren may kiosk.

Luu y: camera browser chi duoc phep tren `localhost` hoac HTTPS. Vi vay may kiosk
nen host UI local o `127.0.0.1:5500`, sau do gui frame/API ve backend qua `BACKEND_URL`.

Neu doi cong/domain kiosk, cap nhat `CORS_ORIGINS` cua backend de cho phep origin
do goi API.
