# worker.py
import os
import time
import json
import logging
import smtplib
from email.mime.text import MIMEText
from typing import Optional
import jwt
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- Config ----------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logging.error("DATABASE_URL not set. Exiting.")
    raise RuntimeError("DATABASE_URL not configured")

MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", 5))
RETRY_INTERVAL = float(os.getenv("RETRY_INTERVAL", 5.0))  # seconds between retries

# ---------- Database ----------
engine = sa.create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine)

# ---------- Email ----------
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_USER)
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() in ("1", "true", "yes")

if not (EMAIL_USER and EMAIL_PASS):
    logging.warning("Email credentials not fully configured. Emails will fail until configured.")


# ---------- Email function ----------
def send_email(to_email: str, subject: str, body: str, timeout=20):
    """Send an email via SMTP."""
    if not (EMAIL_USER and EMAIL_PASS):
        return False, "SMTP credentials not configured"

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = EMAIL_FROM
    msg["To"] = to_email
    msg["Subject"] = subject

    try:
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=timeout) as server:
            server.ehlo()
            if EMAIL_USE_TLS:
                server.starttls()
                server.ehlo()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        return True, "Sent"
    except Exception as e:
        logging.exception("send_email error")
        return False, str(e)


# ---------- JWT license functions ----------
def load_private_key(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def generate_license_jwt(private_key_pem: bytes, license_id: int, product_sku: str, order_id: int,
                         issuer: str, expires_days: Optional[int] = None) -> str:
    now = int(time.time())
    payload = {
        "sub": str(license_id),
        "iss": issuer,
        "iat": now,
        "jti": f"lic-{license_id}-{now}",
        "product": product_sku,
        "order_id": order_id
    }
    if expires_days:
        payload["exp"] = now + expires_days * 24 * 3600
    token = jwt.encode(payload, private_key_pem, algorithm="RS256")
    return token


# ---------- Queue processing with automatic retries ----------
def process_all_messages():
    """Process all queued email messages with retries."""
    session = SessionLocal()
    try:
        rows = session.execute(sa.text("""
            SELECT id, email, message, attempts
            FROM sms_messages
            WHERE status='queued' AND method='email'
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
        """)).all()

        if not rows:
            logging.info("No queued emails to process.")
            return []

        processed_ids = []

        for row in rows:
            msg_id, email, message, attempts = row
            logging.info("Processing message id=%s email=%s attempts=%s", msg_id, email, attempts)

            # Mark as sending and increment attempts
            session.execute(sa.text("""
                UPDATE sms_messages
                SET status='sending', attempts=attempts + 1, last_attempt_at=now()
                WHERE id=:id
            """), {"id": msg_id})
            session.commit()

            try_count = 0
            success = False
            last_error = ""

            while try_count < MAX_ATTEMPTS and not success:
                ok, resp = send_email(email, "Your License Key", message)
                if ok:
                    success = True
                    logging.info("Email id=%s sent successfully", msg_id)
                    session.execute(sa.text("""
                        UPDATE sms_messages
                        SET status='sent', response_json=:resp, sent_at=now()
                        WHERE id=:id
                    """), {"resp": json.dumps({"info": resp}), "id": msg_id})
                    session.commit()
                    processed_ids.append(msg_id)
                    break
                else:
                    last_error = resp
                    try_count += 1
                    logging.warning("Retry %s for email id=%s failed: %s", try_count, msg_id, resp)
                    time.sleep(RETRY_INTERVAL)

            if not success:
                logging.error("Email id=%s failed after %s attempts", msg_id, MAX_ATTEMPTS)
                session.execute(sa.text("""
                    UPDATE sms_messages
                    SET status='failed', response_json=:resp, last_attempt_at=now()
                    WHERE id=:id
                """), {"resp": json.dumps({"error": last_error}), "id": msg_id})
                session.commit()

    finally:
        session.close()

    return processed_ids
