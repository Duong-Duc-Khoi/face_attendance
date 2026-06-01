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
```

Luu y: camera browser chi duoc phep tren `localhost` hoac HTTPS. Vi vay may kiosk
nen host UI local o `127.0.0.1:5500`, sau do gui frame ve backend qua `BACKEND_URL`.
