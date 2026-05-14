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


# ── Notify hệ thống Leave mới ────────────────────────────────────

def _leave_dates_str(req) -> str:
    dates = req.get_dates()
    parts = []
    for d in dates:
        s = d["date"]
        if d.get("half") == "am":
            s += " (Sáng)"
        elif d.get("half") == "pm":
            s += " (Chiều)"
        parts.append(s)
    return ", ".join(parts)


def _leave_type_label(req) -> str:
    return "Làm Remote" if req.request_type == "remote" else "Nghỉ phép"


def notify_leave_submitted(req):
    """Gửi email cho manager khi NV gửi đơn."""
    recipient = settings.EMAIL_TO or settings.EMAIL_USER
    if not recipient:
        return
    rtype  = _leave_type_label(req)
    dates  = _leave_dates_str(req)
    total  = req.total_days()
    submitted = req.submitted_at.strftime("%H:%M — %d/%m/%Y") if req.submitted_at else ""
    subject = f"[FaceAttend] Đơn {rtype} — {req.emp_name} ({req.emp_code})"
    html = f"""
    <div style="font-family:Arial,sans-serif;background:#f0f4f8;padding:32px">
      <div style="max-width:500px;margin:auto;background:#fff;border-radius:10px;
                  padding:28px;border-left:4px solid #805ad5">
        <h2 style="margin:0 0 16px;color:#805ad5">📋 Đơn {rtype} mới</h2>
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096;width:40%">Nhân viên</td>
            <td style="padding:8px 4px;font-weight:600">{req.emp_name} ({req.emp_code})</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096">Phòng ban</td>
            <td style="padding:8px 4px">{req.department or "—"}</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096">Ngày</td>
            <td style="padding:8px 4px;font-weight:700">{dates}</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096">Tổng</td>
            <td style="padding:8px 4px">{total} ngày</td>
          </tr>
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096">Lý do</td>
            <td style="padding:8px 4px">{req.reason or "(Không ghi)"}</td>
          </tr>
          <tr>
            <td style="padding:8px 4px;color:#718096">Gửi lúc</td>
            <td style="padding:8px 4px;color:#a0aec0;font-size:12px">{submitted}</td>
          </tr>
        </table>
        <p style="margin:20px 0 0;font-size:12px;color:#a0aec0">
          Vui lòng đăng nhập hệ thống FaceAttend để duyệt hoặc từ chối.
        </p>
      </div>
    </div>"""
    _send_email(recipient, subject, html)


def notify_leave_approved(req):
    """Gửi email cho nhân viên khi đơn được duyệt."""
    if not req.emp_email:
        return
    rtype  = _leave_type_label(req)
    dates  = _leave_dates_str(req)
    subject = f"[FaceAttend] ✅ Đơn {rtype} đã được duyệt"
    html = f"""
    <div style="font-family:Arial,sans-serif;background:#f0f4f8;padding:32px">
      <div style="max-width:500px;margin:auto;background:#fff;border-radius:10px;
                  padding:28px;border-left:4px solid #38a169">
        <h2 style="margin:0 0 16px;color:#38a169">✅ Đơn {rtype} đã được duyệt</h2>
        <p style="color:#4a5568">Xin chào <strong>{req.emp_name}</strong>,</p>
        <p style="color:#4a5568">Đơn {rtype.lower()} của bạn đã được <strong>phê duyệt</strong>.</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;margin:16px 0">
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096;width:40%">Ngày</td>
            <td style="padding:8px 4px;font-weight:700">{dates}</td>
          </tr>
          <tr>
            <td style="padding:8px 4px;color:#718096">Ghi chú</td>
            <td style="padding:8px 4px">{req.note or "—"}</td>
          </tr>
        </table>
        <p style="color:#a0aec0;font-size:12px">Duyệt bởi: {req.reviewed_by or "—"}</p>
      </div>
    </div>"""
    _send_email(req.emp_email, subject, html)


def notify_leave_rejected(req):
    """Gửi email cho nhân viên khi đơn bị từ chối."""
    if not req.emp_email:
        return
    rtype  = _leave_type_label(req)
    dates  = _leave_dates_str(req)
    subject = f"[FaceAttend] ❌ Đơn {rtype} bị từ chối"
    html = f"""
    <div style="font-family:Arial,sans-serif;background:#f0f4f8;padding:32px">
      <div style="max-width:500px;margin:auto;background:#fff;border-radius:10px;
                  padding:28px;border-left:4px solid #e53e3e">
        <h2 style="margin:0 0 16px;color:#e53e3e">❌ Đơn {rtype} bị từ chối</h2>
        <p style="color:#4a5568">Xin chào <strong>{req.emp_name}</strong>,</p>
        <p style="color:#4a5568">Rất tiếc, đơn {rtype.lower()} của bạn đã bị <strong>từ chối</strong>.</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;margin:16px 0">
          <tr style="border-bottom:1px solid #e2e8f0">
            <td style="padding:8px 4px;color:#718096;width:40%">Ngày</td>
            <td style="padding:8px 4px;font-weight:700">{dates}</td>
          </tr>
          <tr>
            <td style="padding:8px 4px;color:#718096">Lý do từ chối</td>
            <td style="padding:8px 4px;color:#e53e3e;font-weight:600">{req.note}</td>
          </tr>
        </table>
        <p style="color:#4a5568;font-size:13px">Nếu có thắc mắc vui lòng liên hệ quản lý trực tiếp.</p>
      </div>
    </div>"""
    _send_email(req.emp_email, subject, html)


def notify_leave_cancelled(req, cancelled_by: str = ""):
    """Gửi email cho manager khi NV hủy đơn."""
    recipient = settings.EMAIL_TO or settings.EMAIL_USER
    if not recipient:
        return
    rtype = _leave_type_label(req)
    dates = _leave_dates_str(req)
    subject = f"[FaceAttend] Hủy đơn {rtype} — {req.emp_name}"
    html = f"""
    <div style="font-family:Arial,sans-serif;background:#f0f4f8;padding:32px">
      <div style="max-width:500px;margin:auto;background:#fff;border-radius:10px;
                  padding:28px;border-left:4px solid #718096">
        <h2 style="margin:0 0 16px;color:#718096">🚫 Đơn {rtype} đã bị hủy</h2>
        <p style="color:#4a5568"><strong>{req.emp_name} ({req.emp_code})</strong>
           đã hủy đơn {rtype.lower()}.</p>
        <p style="color:#4a5568">Ngày: <strong>{dates}</strong></p>
        <p style="color:#a0aec0;font-size:12px">Hủy bởi: {cancelled_by}</p>
      </div>
    </div>"""
    _send_email(recipient, subject, html)
