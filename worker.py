
import os
import time
import json
import logging
from typing import Optional
import jwt
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
from mailjet_rest import Client  # <-- Mailjet official SDK

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

# ---------- Mailjet Email ----------
MAILJET_API_KEY = os.getenv('MAILJET_API_KEY')
MAILJET_API_SECRET = os.getenv('MAILJET_API_SECRET')
MAILJET_FROM = os.getenv('MAILJET_FROM')  # Should be a Mailjet-verified sender or your Gmail

if not all([MAILJET_API_KEY, MAILJET_API_SECRET, MAILJET_FROM]):
    logging.warning("Mailjet config missing! Emails will fail until configured.")

# ---------- Email function (using Mailjet) ----------
def send_email(to_email: str, subject: str, body: str, timeout=20):
    """Send an email via Mailjet transactional API."""
    if not all([MAILJET_API_KEY, MAILJET_API_SECRET, MAILJET_FROM]):
        return False, "Mailjet API credentials/config missing"
    try:
        mailjet = Client(auth=(MAILJET_API_KEY, MAILJET_API_SECRET), version='v3.1')
        data = {
            'Messages': [
                {
                    "From": {
                        "Email": MAILJET_FROM,
                        "Name": "POS License"
                    },
                    "To": [
                        {
                            "Email": to_email,
                            "Name": to_email.split("@")[0]
                        }
                    ],
                    "Subject": subject,
                    "TextPart": body,
                    # "HTMLPart": "<strong>%s</strong>" % body,  # optional HTML
                }
            ]
        }
        result = mailjet.send.create(data=data)
        # Mailjet's transactional send returns 200 for success
        if result.status_code == 200:
            return True, "Sent"
        else:
            logging.error(f"Mailjet error [{result.status_code}]: {result.json()}")
            return False, str(result.json())
    except Exception as e:
        logging.exception("Mailjet send_email error")
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
