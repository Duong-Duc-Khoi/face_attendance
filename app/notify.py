import os
import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

EMAIL_HOST     = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT     = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USER     = os.getenv("EMAIL_USER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")


# ──────────────────────────────────────────
# Core — sync (chạy trong thread pool)
# ──────────────────────────────────────────
def _send_email(to: str, subject: str, body_html: str) -> bool:
    if not EMAIL_USER or not EMAIL_PASSWORD:
        print("  ⚠ Email chưa cấu hình — bỏ qua")
        return False
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"FaceAttend <{EMAIL_USER}>"
        msg["To"]      = to
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USER, to, msg.as_string())
        print(f"  ✓ Email gửi tới {to}")
        return True
    except Exception as e:
        print(f"  ✗ Lỗi gửi email tới {to}: {e}")
        return False


async def _send_email_async(to: str, subject: str, body_html: str) -> bool:
    """Async wrapper — chạy SMTP trong thread pool, không block event loop"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _send_email, to, subject, body_html)


# ──────────────────────────────────────────
# Thông báo đi muộn — gửi tới email nhân viên
# ──────────────────────────────────────────
async def notify_late_async(emp_name: str, emp_code: str,
                            department: str, minutes_late: int,
                            emp_email: str):
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
        <p style="color:#4a5568;margin:0 0 20px">
          Hệ thống ghi nhận bạn đã vào làm muộn hôm nay.
        </p>
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


# ──────────────────────────────────────────
# Báo cáo cuối ngày — gửi về chính EMAIL_USER
# ──────────────────────────────────────────
async def notify_daily_report_async(summary: dict):
    if not EMAIL_USER:
        return
    date    = summary.get("date", datetime.now().strftime("%d/%m/%Y"))
    subject = f"[FaceAttend] Báo cáo chấm công ngày {date}"
    absent  = summary.get("absent", 0)
    absent_color = "#e53e3e" if absent > 0 else "#38a169"
    html    = f"""
    <div style="font-family:Arial,sans-serif;background:#f0f4f8;padding:32px">
      <div style="max-width:460px;margin:auto;background:#fff;border-radius:10px;
                  padding:28px;border-left:4px solid #3182ce">
        <h2 style="margin:0 0 4px;color:#2d3748">📊 Báo cáo chấm công</h2>
        <p style="margin:0 0 20px;color:#718096;font-size:14px">Ngày {date}</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:10px 4px;color:#718096">Tổng nhân viên</td>
            <td style="padding:10px 4px;font-weight:700;font-size:18px;color:#2d3748">
              {summary.get("total_emp", 0)}
            </td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:10px 4px;color:#718096">Đã vào làm</td>
            <td style="padding:10px 4px;font-weight:700;font-size:18px;color:#38a169">
              {summary.get("checked_in", 0)}
            </td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:10px 4px;color:#718096">Đã ra về</td>
            <td style="padding:10px 4px;font-weight:700;font-size:18px;color:#3182ce">
              {summary.get("checked_out", 0)}
            </td>
          </tr>
          <tr>
            <td style="padding:10px 4px;color:#718096">Vắng mặt</td>
            <td style="padding:10px 4px;font-weight:700;font-size:18px;color:{absent_color}">
              {absent}
            </td>
          </tr>
        </table>
        <p style="margin:20px 0 0;font-size:12px;color:#a0aec0">
          Email tự động từ hệ thống FaceAttend — vui lòng không reply.
        </p>
      </div>
    </div>
    """
    await _send_email_async(EMAIL_USER, subject, html)

def notify_account_created(to_email: str, emp_name: str, emp_code: str, temp_password: str):
    """Gửi thông tin tài khoản vừa tạo cho nhân viên"""
    if not EMAIL_USER or not EMAIL_PASSWORD:
        print("  ⚠ Email chưa cấu hình — bỏ qua")
        return False
    
    subject = "[FaceAttend] Tài khoản của bạn đã được tạo"
    html = f"""
    <div style="font-family:Arial;padding:20px;background:#f5f5f5">
      <div style="background:#fff;border-radius:8px;padding:24px;max-width:480px">
        <h2 style="color:#00d4aa">✅ Chào mừng đến FaceAttend</h2>
        <p>Xin chào <strong>{emp_name}</strong>,</p>
        <p>Tài khoản của bạn đã được tạo thành công. Thông tin đăng nhập:</p>
        <table style="width:100%;border-collapse:collapse;background:#f9f9f9;border-radius:6px">
          <tr><td style="padding:10px;color:#666">Mã nhân viên</td>
              <td style="font-weight:bold">{emp_code}</td></tr>
          <tr><td style="padding:10px;color:#666">Tên đăng nhập</td>
              <td style="font-weight:bold">{to_email}</td></tr>
          <tr><td style="padding:10px;color:#666">Mật khẩu tạm</td>
              <td style="font-weight:bold;color:#e53e3e;letter-spacing:2px">{temp_password}</td></tr>
        </table>
        <p style="color:#e53e3e;font-size:13px">⚠ Vui lòng đổi mật khẩu ngay sau lần đăng nhập đầu tiên.</p>
        <p style="color:#999;font-size:12px">Nếu bạn không thực hiện đăng ký này, vui lòng liên hệ HR.</p>
      </div>
    </div>
    """
    
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_USER
        msg["To"]      = to_email          # ← gửi đến nhân viên, không phải admin
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USER, to_email, msg.as_string())
        
        print(f"  ✓ Đã gửi tài khoản đến {to_email}")
        return True
    except Exception as e:
        print(f"  ✗ Lỗi gửi email: {e}")
        return False
