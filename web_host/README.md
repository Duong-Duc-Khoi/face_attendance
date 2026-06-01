# FaceAttend Web Host

Host rieng cho giao dien quan ly/nhan vien bang FastAPI, con backend API chay
rieng o cong khac.

## Chay backend

```powershell
cd G:\face_attendance
conda activate face_attendance
python run.py
```

Mac dinh backend o:

```text
http://127.0.0.1:8000
```

## Chay web host quan ly

```powershell
cd G:\face_attendance
conda activate face_attendance
python run_web.py
```

Mo:

```text
http://127.0.0.1:5600/auth/login-page
http://127.0.0.1:5600/dashboard
```

Mac dinh web host se proxy API/Auth/Data ve:

```text
http://127.0.0.1:8000
```

## Doi backend/kiosk URL

```powershell
$env:BACKEND_URL="http://127.0.0.1:8000"
$env:KIOSK_URL="http://127.0.0.1:5500"
python run_web.py
```

Trong ban tach thu nay, web host van render Jinja template hien co va proxy
`/api/*`, `/auth/*`, `/data/*` ve backend. Cach nay giup tach port FE/BE ma
chua can rewrite dashboard thanh static SPA.
