import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
import os

load_dotenv()

EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_USER)

msg = MIMEText("This is a test email from my POS system.")
msg["Subject"] = "Test Email"
msg["From"] = EMAIL_FROM
msg["To"] = EMAIL_FROM

try:
    print("Connecting to mail server...")
    server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=10)
    server.ehlo()  # say hello
    server.starttls()  # start encryption
    server.ehlo()  # re-identify as encrypted connection
    print("Logging in...")
    server.login(EMAIL_USER, EMAIL_PASS)
    print("Sending email...")
    server.sendmail(EMAIL_FROM, [EMAIL_FROM], msg.as_string())
    print("✅ Email sent successfully!")
    server.quit()
except Exception as e:
    print("❌ Email failed:", e)
