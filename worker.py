import os
import time
import json
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from twilio.rest import Client as TwilioClient

load_dotenv()

# ---------- Database ----------
DATABASE_URL = os.getenv("DATABASE_URL")
engine = sa.create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine)

# ---------- Twilio ----------
TW_SID = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TW_FROM = os.getenv("TWILIO_FROM")
tw = TwilioClient(TW_SID, TW_TOKEN) if TW_SID and TW_TOKEN else None

# ---------- Email ----------
EMAIL_HOST = os.getenv("EMAIL_HOST")      # e.g., smtp.gmail.com
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_USER)


def send_email(to_email, subject, body):
    msg = MIMEText(body, "plain")
    msg["From"] = EMAIL_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    try:
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            print("📌 Connecting to email server...")
            server.ehlo()
            server.starttls()
            server.ehlo()
            print("📌 Logging in...")
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        return True, "Sent"
    except Exception as e:
        return False, str(e)


def process_queue():
    print("🚀 Starting message worker...")
    while True:
        session = SessionLocal()
        try:
            # Fetch one queued message
            msg = session.execute(sa.text(
                "SELECT id, phone, email, message, attempts, method "
                "FROM sms_messages WHERE status='queued' ORDER BY created_at LIMIT 1"
            )).first()

            if not msg:
                # Nothing to process, wait
                session.close()
                time.sleep(2)
                continue

            msg_id, phone, email, message, attempts, method = msg
            print(f"📌 Processing message {msg_id}, method: {method}")

            # Mark as sending
            session.execute(sa.text(
                "UPDATE sms_messages SET status='sending', attempts = attempts + 1, last_attempt_at = now() WHERE id=:id"
            ), {"id": msg_id})
            session.commit()

            success = False
            response_info = {}

            try:
                if method == "sms" and phone:
                    if not tw:
                        raise RuntimeError("Twilio not configured")
                    res = tw.messages.create(body=message, from_=TW_FROM, to=phone)
                    success = True
                    response_info = {"sid": res.sid}

                elif method == "email" and email:
                    ok, resp = send_email(email, "Your License Key", message)
                    success = ok
                    response_info = {"info": resp}

                else:
                    raise RuntimeError("Invalid method or missing recipient")

                # Mark as sent
                session.execute(sa.text(
                    "UPDATE sms_messages SET status='sent', response_json=:resp WHERE id=:id"
                ), {"resp": json.dumps(response_info), "id": msg_id})
                session.commit()
                print(f"✅ Message sent ({method}): {msg_id}")

            except Exception as ex:
                print("❌ Message send failed:", ex)
                row = session.execute(sa.text(
                    "SELECT attempts FROM sms_messages WHERE id=:id"
                ), {"id": msg_id}).first()
                att = row[0] if row else attempts + 1
                status = 'failed' if att >= 5 else 'queued'
                session.execute(sa.text(
                    "UPDATE sms_messages SET status=:status, last_attempt_at=now() WHERE id=:id"
                ), {"status": status, "id": msg_id})
                session.commit()

        finally:
            session.close()
        time.sleep(1)


if __name__ == "__main__":
    process_queue()
