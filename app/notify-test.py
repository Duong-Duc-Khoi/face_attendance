import os
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

def send_offer_email():
    subject = "🎉 [Offer Letter] Congratulations from Tech Company"

    body_html = """
    <div style="font-family: Arial, sans-serif; background-color:#f6f9fc; padding:20px;">
        <div style="max-width:600px; margin:auto; background:white; border-radius:10px; padding:30px;">
            
            <h2 style="color:#0b57d0;">🎉 Congratulations!</h2>

            <p>Dear <b>Nguyễn Hữu Duy</b>,</p>

            <p>
                We are excited to offer you the position of 
                <b>Software Engineer</b> at <b>Tech Company</b>.
            </p>

            <div style="background:#f1f3f4; padding:15px; border-radius:8px;">
                <p><b>📍 Location:</b> Southeast Asia Office</p>
                <p><b>💰 Salary:</b> Competitive</p>
                <p><b>📅 Start Date:</b> To be confirmed</p>
            </div>

            <p>Please reply to this email to confirm your acceptance.</p>

            <div style="text-align:center; margin:30px 0;">
                <a href="https://web.facebook.com/duy.nguyenhuu.58118" style="background:#0b57d0; color:white; padding:12px 20px; text-decoration:none; border-radius:6px;">
                    Accept Offer
                </a>
            </div>

            <p>If you have any questions, feel free to contact us.</p>

            <br>
            <p>Best regards,<br><b>Recruitment Team</b></p>
        </div>
    </div>
    """

    msg = MIMEText(body_html, "html")
    msg["Subject"] = subject
    msg["From"] = os.getenv("EMAIL_USER")
    msg["To"] = os.getenv("EMAIL_TO")

    with smtplib.SMTP(os.getenv("EMAIL_HOST"), int(os.getenv("EMAIL_PORT"))) as server:
        server.starttls()
        server.login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASSWORD"))
        server.send_message(msg)

    print("✓ Gửi email thành công")


# chạy test
if __name__ == "__main__":
    send_offer_email()