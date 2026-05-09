"""
app/services/notify.py
Gửi email thông báo: đi muộn + báo cáo cuối ngày.
Config email đọc từ settings — không load dotenv tại đây.
"""

import asyncio
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings


# ── Core sync (chạy trong thread pool) ──────────────────────────
def _send_email(to: str, subject: str, body_html: str) -> bool:
    if not settings.EMAIL_USER or not settings.EMAIL_PASSWORD:
        print("  ⚠ Email chưa cấu hình — bỏ qua")
        return False
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"FaceAttend <{settings.EMAIL_USER}>"
        msg["To"]      = to
        msg.attach(MIMEText(body_html, "html", "utf-8"))
        with smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT) as server:
            server.starttls()
            server.login(settings.EMAIL_USER, settings.EMAIL_PASSWORD)
            server.sendmail(settings.EMAIL_USER, to, msg.as_string())
        print(f"  ✓ Email gửi tới {to}")
        return True
    except Exception as e:
        print(f"  ✗ Lỗi gửi email tới {to}: {e}")
        return False


async def _send_email_async(to: str, subject: str, body_html: str) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _send_email, to, subject, body_html)


# ── Thông báo đi muộn ────────────────────────────────────────────
async def notify_late_async(emp_name: str, emp_code: str,
                            department: str, minutes_late: int, emp_email: str):
    if not emp_email:
        print(f"  ⚠ {emp_code} không có email — bỏ qua notify_late")
        return
    now     = datetime.now().strftime("%H:%M — %d/%m/%Y")
    subject = f"[FaceAttend] Bạn đã vào làm muộn {minutes_late} phút"
    html    = f"""
    <div style="font-family:Arial,sans-serif;background:#f0f4f8;padding:32px">
      <div style="max-width:460px;margin:auto;background:#fff;border-radius:10px;
                  padding:28px;border-left:4px solid #e53e3e">
        <h2 style="margin:0 0 16px;color:#e53e3e">⚠ Thông báo đi muộn</h2>
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096;width:40%">Nhân viên</td>
            <td style="padding:8px 4px;font-weight:600;color:#2d3748">{emp_name} ({emp_code})</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096">Phòng ban</td>
            <td style="padding:8px 4px;color:#2d3748">{department}</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096">Đi muộn</td>
            <td style="padding:8px 4px;font-weight:700;color:#e53e3e">{minutes_late} phút</td>
          </tr>
          <tr>
            <td style="padding:8px 4px;color:#718096">Thời gian vào</td>
            <td style="padding:8px 4px;color:#2d3748">{now}</td>
          </tr>
        </table>
        <p style="margin:20px 0 0;font-size:12px;color:#a0aec0">
          Email tự động từ hệ thống FaceAttend — vui lòng không reply.
        </p>
      </div>
    </div>
    """
    await _send_email_async(emp_email, subject, html)


# ── Báo cáo cuối ngày ────────────────────────────────────────────
async def notify_daily_report_async(summary: dict):
    recipient = settings.EMAIL_TO or settings.EMAIL_USER
    if not recipient:
        return
    date         = summary.get("date", datetime.now().strftime("%d/%m/%Y"))
    absent       = summary.get("absent", 0)
    absent_color = "#e53e3e" if absent > 0 else "#38a169"
    subject      = f"[FaceAttend] Báo cáo chấm công ngày {date}"
    html         = f"""
    <div style="font-family:Arial,sans-serif;background:#f0f4f8;padding:32px">
      <div style="max-width:460px;margin:auto;background:#fff;border-radius:10px;
                  padding:28px;border-left:4px solid #3182ce">
        <h2 style="margin:0 0 4px;color:#2d3748">📊 Báo cáo chấm công</h2>
        <p style="margin:0 0 20px;color:#718096;font-size:14px">Ngày {date}</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:10px 4px;color:#718096">Tổng nhân viên</td>
            <td style="padding:10px 4px;font-weight:700;font-size:18px;color:#2d3748">{summary.get("total_emp", 0)}</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:10px 4px;color:#718096">Đã vào làm</td>
            <td style="padding:10px 4px;font-weight:700;font-size:18px;color:#38a169">{summary.get("checked_in", 0)}</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:10px 4px;color:#718096">Đã ra về</td>
            <td style="padding:10px 4px;font-weight:700;font-size:18px;color:#3182ce">{summary.get("checked_out", 0)}</td>
          </tr>
          <tr>
            <td style="padding:10px 4px;color:#718096">Vắng mặt</td>
            <td style="padding:10px 4px;font-weight:700;font-size:18px;color:{absent_color}">{absent}</td>
          </tr>
        </table>
        <p style="margin:20px 0 0;font-size:12px;color:#a0aec0">
          Email tự động từ hệ thống FaceAttend.
        </p>
      </div>
    </div>
    """
    await _send_email_async(recipient, subject, html)


# ── Thông báo xin nghỉ ───────────────────────────────────────────
async def notify_leave_request_async(payload: dict):
    """Gửi email thông báo đơn xin nghỉ tới quản lý."""
    recipient = settings.EMAIL_TO or settings.EMAIL_USER
    if not recipient:
        print("  ⚠ EMAIL_TO chưa cấu hình — lưu log đơn xin nghỉ:")
        print(f"  {payload}")
        return
    emp_name  = payload.get("name", "—")
    emp_code  = payload.get("emp_code", "—")
    from_date = payload.get("from_date", "—")
    to_date   = payload.get("to_date", "—")
    ltype     = payload.get("leave_type", "—")
    reason    = payload.get("reason", "")
    submitted = datetime.now().strftime("%H:%M — %d/%m/%Y")
    subject   = f"[FaceAttend] Đơn xin nghỉ — {emp_name} ({emp_code})"
    html      = f"""
    <div style="font-family:Arial,sans-serif;background:#f0f4f8;padding:32px">
      <div style="max-width:480px;margin:auto;background:#fff;border-radius:10px;
                  padding:28px;border-left:4px solid #805ad5">
        <h2 style="margin:0 0 16px;color:#805ad5">📋 Đơn xin nghỉ phép</h2>
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096;width:40%">Nhân viên</td>
            <td style="padding:8px 4px;font-weight:600;color:#2d3748">{emp_name} ({emp_code})</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096">Ngày nghỉ từ</td>
            <td style="padding:8px 4px;font-weight:700;color:#2d3748">{from_date}</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096">Đến ngày</td>
            <td style="padding:8px 4px;font-weight:700;color:#2d3748">{to_date}</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096">Loại nghỉ</td>
            <td style="padding:8px 4px;color:#2d3748">{ltype}</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096">Lý do</td>
            <td style="padding:8px 4px;color:#2d3748">{reason or '(Không ghi)'}</td>
          </tr>
          <tr>
            <td style="padding:8px 4px;color:#718096">Gửi lúc</td>
            <td style="padding:8px 4px;color:#a0aec0;font-size:12px">{submitted}</td>
          </tr>
        </table>
        <p style="margin:20px 0 0;font-size:12px;color:#a0aec0">
          Nhân viên đã gửi đơn qua hệ thống FaceAttend. Vui lòng xem xét và phản hồi.
        </p>
      </div>
    </div>
    """
    await _send_email_async(recipient, subject, html)
    print(f"  ✓ Đơn xin nghỉ của {emp_name} đã thông báo tới {recipient}")
