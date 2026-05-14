"""
app/core/config.py
Toàn bộ cấu hình của app được load từ .env tại đây.
Không load dotenv rải rác ở nhiều file — chỉ load 1 lần duy nhất tại đây.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env từ thư mục gốc project (1 cấp trên app/)
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


class Settings:
    # ── Database ───────────────────────────────────────
    DB_USER:     str = os.getenv("DB_USER",     "postgres")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_HOST:     str = os.getenv("DB_HOST",     "localhost")
    DB_PORT:     str = os.getenv("DB_PORT",     "5432")
    DB_NAME:     str = os.getenv("DB_NAME",     "face_attendance")

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    # ── JWT / Auth ─────────────────────────────────────
    JWT_SECRET:        str = os.getenv("JWT_SECRET", "CHANGE_THIS_SECRET_IN_PRODUCTION_32CHARS")
    JWT_ALGORITHM:     str = "HS256"
    ACCESS_TOKEN_EXP:  int = int(os.getenv("ACCESS_TOKEN_EXP",  "15"))      # phút
    REFRESH_TOKEN_EXP: int = int(os.getenv("REFRESH_TOKEN_EXP", "10080"))   # phút (7 ngày)
    OTP_EXP_MINUTES:   int = int(os.getenv("OTP_EXP_MINUTES",   "10"))

    # ── OpenAI / AI planning ───────────────────────────
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL:   str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL:   str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    # ── Email ──────────────────────────────────────────
    EMAIL_HOST:     str = os.getenv("EMAIL_HOST",     "smtp.gmail.com")
    EMAIL_PORT:     int = int(os.getenv("EMAIL_PORT", "587"))
    EMAIL_USER:     str = os.getenv("EMAIL_USER",     "")
    EMAIL_PASSWORD: str = os.getenv("EMAIL_PASSWORD", "")
    EMAIL_TO:       str = os.getenv("EMAIL_TO",       "")
    APP_NAME:       str = "FaceAttend"
    BASE_URL:       str = os.getenv("BASE_URL", "http://localhost:8000")

    # ── Camera ─────────────────────────────────────────
    CAMERA_ID: int = int(os.getenv("CAMERA_ID", "0"))

    # ── Face Engine ────────────────────────────────────
    FACE_THRESHOLD:  float = float(os.getenv("FACE_THRESHOLD",  "0.50"))
    MIN_FACE_SIZE:   int   = 40     # px — bỏ qua khuôn mặt quá nhỏ

    # ── Chấm công ──────────────────────────────────────
    COOLDOWN_MINUTES: int = int(os.getenv("COOLDOWN_MINUTES", "5"))
    WORK_START:             str  = os.getenv("WORK_START", "08:30")
    WORK_END:               str  = os.getenv("WORK_END",   "17:30")
    LATE_THRESHOLD:         int  = int(os.getenv("LATE_THRESHOLD_MINUTES", "15"))
    LATE_THRESHOLD_MINUTES: int  = int(os.getenv("LATE_THRESHOLD_MINUTES", "15"))
    WORK_DAYS:              str  = os.getenv("WORK_DAYS", "1,2,3,4,5")
    HALF_DAY_CUTOFF:        str  = os.getenv("HALF_DAY_CUTOFF", "12:00")
    NOTIFY_LEAVE_CANCEL:    bool = os.getenv("NOTIFY_LEAVE_CANCEL", "true").lower() == "true"

    # ── Paths ──────────────────────────────────────────
    DATA_DIR:          Path = Path("data")
    EMBEDDINGS_PATH:   Path = Path("data/embeddings.pkl")
    FACES_DIR:         Path = Path("data/faces")
    CAPTURES_DIR:      Path = Path("data/captures")
    EXPORTS_DIR:       Path = Path("data/exports")

    # ── Telegram ───────────────────────────────────────
    TELEGRAM_TOKEN:   str = os.getenv("TELEGRAM_TOKEN",   "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Server ─────────────────────────────────────────
    HOST:  str = os.getenv("HOST",  "0.0.0.0")
    PORT:  int = int(os.getenv("PORT", "8000"))


settings = Settings()
