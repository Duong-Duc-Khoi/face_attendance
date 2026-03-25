import os
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path

# trỏ tới file .env ở root
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# ── Cấu hình — điền vào file .env hoặc config.py ──
EMAIL_HOST     = "smtp.gmail.com"
EMAIL_PORT     = 587
EMAIL_USER     = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")         # App password (không dùng pass thật)
EMAIL_TO       = "ayatoyuuto262@gmail.com"       # admin@company.com

TELEGRAM_TOKEN  = ""         # Bot token từ @BotFather
TELEGRAM_CHAT_ID = ""        # Chat ID nhận thông báo


# ──────────────────────────────────────────
# Email
# ──────────────────────────────────────────
def send_email(subject: str, body_html: str) -> bool:
    """Gửi email thông báo"""
    if not EMAIL_USER or not EMAIL_PASSWORD:
        print("  ⚠ Email chưa cấu hình — bỏ qua")
        return False
    try:
        msg                    = MIMEMultipart("alternative")
        msg["Subject"]         = subject
        msg["From"]            = EMAIL_USER
        msg["To"]              = EMAIL_TO
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USER, EMAIL_TO, msg.as_string())
        return True
    except Exception as e:
        print(f"  ✗ Lỗi gửi email: {e}")
        return False


def notify_late(emp_name: str, emp_code: str,
                department: str, minutes_late: int):
    """Thông báo nhân viên đi muộn"""
    now     = datetime.now().strftime("%H:%M - %d/%m/%Y")
    subject = f"[Chấm công] {emp_name} đi muộn {minutes_late} phút"
    html    = f"""
    <div style="font-family:Arial;padding:20px;background:#f5f5f5">
      <div style="background:#fff;border-radius:8px;padding:24px;max-width:480px">
        <h2 style="color:#e53e3e">⚠ Nhân viên đi muộn</h2>
        <table style="width:100%;border-collapse:collapse">
          <tr><td style="padding:6px 0;color:#666">Nhân viên</td>
              <td style="font-weight:bold">{emp_name} ({emp_code})</td></tr>
          <tr><td style="padding:6px 0;color:#666">Phòng ban</td>
              <td>{department}</td></tr>
          <tr><td style="padding:6px 0;color:#666">Đi muộn</td>
              <td style="color:#e53e3e;font-weight:bold">{minutes_late} phút</td></tr>
          <tr><td style="padding:6px 0;color:#666">Thời gian</td>
              <td>{now}</td></tr>
        </table>
      </div>
    </div>
    """
    send_email(subject, html)


def notify_daily_report(summary: dict):
    """Gửi báo cáo tổng kết cuối ngày"""
    subject = f"[Báo cáo] Tổng kết chấm công {summary.get('date','')}"
    html    = f"""
    <div style="font-family:Arial;padding:20px;background:#f5f5f5">
      <div style="background:#fff;border-radius:8px;padding:24px;max-width:480px">
        <h2 style="color:#2d3748">📊 Báo cáo chấm công hôm nay</h2>
        <table style="width:100%;border-collapse:collapse">
          <tr><td style="padding:8px 0;color:#666">Tổng nhân viên</td>
              <td style="font-weight:bold">{summary.get('total_emp',0)}</td></tr>
          <tr><td style="padding:8px 0;color:#666">Đã vào làm</td>
              <td style="color:#38a169;font-weight:bold">{summary.get('checked_in',0)}</td></tr>
          <tr><td style="padding:8px 0;color:#666">Đã ra về</td>
              <td style="color:#3182ce;font-weight:bold">{summary.get('checked_out',0)}</td></tr>
          <tr><td style="padding:8px 0;color:#666">Vắng mặt</td>
              <td style="color:#e53e3e;font-weight:bold">{summary.get('absent',0)}</td></tr>
        </table>
      </div>
    </div>
    """
    send_email(subject, html)


# ──────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────
async def send_telegram(message: str) -> bool:
    """Gửi tin nhắn Telegram async"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        import aiohttp
        url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                return resp.status == 200
    except Exception as e:
        print(f"  ✗ Lỗi Telegram: {e}")
        return False


async def telegram_checkin(data: dict):
    """Thông báo Telegram khi có chấm công"""
    icon    = "🟢" if data["check_type"] == "check_in" else "🔵"
    action  = "VÀO LÀM" if data["check_type"] == "check_in" else "RA VỀ"
    message = (
        f"{icon} <b>{action}</b>\n"
        f"👤 {data['name']} ({data['emp_code']})\n"
        f"🏢 {data['department']}\n"
        f"🕐 {data['time']} — {data['date']}\n"
        f"📊 Độ chính xác: {data['confidence']:.0%}"
    )
    await send_telegram(message)
