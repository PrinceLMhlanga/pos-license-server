import os
import time
import json
import logging
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- Config ----------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logging.error("DATABASE_URL is not set. Exiting.")
    raise RuntimeError("DATABASE_URL not configured")

QUEUE_POLL_INTERVAL = float(os.getenv("QUEUE_POLL_INTERVAL", 1.0))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", 5))

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
    logging.warning("Email credentials not fully configured (EMAIL_USER/EMAIL_PASS). Emails will fail until configured.")


def send_email(to_email, subject, body, timeout=20):
    """
    Synchronously send an email via SMTP. Returns (ok: bool, info: str).
    """
    if not (EMAIL_USER and EMAIL_PASS):
        return False, "SMTP credentials not configured"

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = EMAIL_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    try:
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=timeout) as server:
            logging.debug("Connecting to SMTP server %s:%s", EMAIL_HOST, EMAIL_PORT)
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


def process_queue():
    logging.info("Starting message worker (email-only mode)...")
    while True:
        session = SessionLocal()
        try:
            # Atomically select one queued message and lock the row so other workers skip it
            msg_row = session.execute(sa.text("""
                SELECT id, phone, email, message, attempts, method
                FROM sms_messages
                WHERE status = 'queued'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            """)).first()

            if not msg_row:
                session.close()
                time.sleep(QUEUE_POLL_INTERVAL)
                continue

            msg_id, phone, email, message, attempts, method = msg_row
            logging.info("Processing message id=%s method=%s attempts=%s", msg_id, method, attempts)

            # Mark as sending and increment attempts
            session.execute(sa.text("""
                UPDATE sms_messages
                SET status = 'sending', attempts = attempts + 1, last_attempt_at = now()
                WHERE id = :id
            """), {"id": msg_id})
            session.commit()

            success = False
            response_info = {}

            try:
                # Only process email messages here
                if method == "email" and email:
                    ok, resp = send_email(email, "Your License Key", message)
                    success = ok
                    response_info = {"info": resp}
                    if not ok:
                        raise RuntimeError(f"Email send failed: {resp}")

                elif method == "sms":
                    # SMS handling has been disabled — mark as failed and log
                    response_info = {"error": "sms_disabled", "info": "SMS sending disabled in worker"}
                    success = False
                    logging.warning("SMS sending is disabled. Message id=%s will be marked failed.", msg_id)

                else:
                    response_info = {"error": "invalid_method_or_missing_recipient"}
                    raise RuntimeError("Invalid method or missing recipient")

                # Mark as sent
                session.execute(sa.text("""
                    UPDATE sms_messages
                    SET status = 'sent', response_json = :resp, sent_at = now()
                    WHERE id = :id
                """), {"resp": json.dumps(response_info), "id": msg_id})
                session.commit()
                logging.info("Message id=%s sent successfully (method=%s)", msg_id, method)

            except Exception as ex:
                logging.exception("Message id=%s send failed", msg_id)
                # check updated attempts
                row = session.execute(sa.text("SELECT attempts FROM sms_messages WHERE id = :id"), {"id": msg_id}).first()
                att = row[0] if row else (attempts + 1)
                status = "failed" if att >= MAX_ATTEMPTS else "queued"
                session.execute(sa.text("""
                    UPDATE sms_messages
                    SET status = :status, response_json = :resp, last_attempt_at = now()
                    WHERE id = :id
                """), {"status": status, "resp": json.dumps({"error": str(ex), **(response_info or {})}), "id": msg_id})
                session.commit()
                logging.info("Message id=%s marked as %s (attempts=%s)", msg_id, status, att)

        finally:
            session.close()
        time.sleep(QUEUE_POLL_INTERVAL)


if __name__ == "__main__":
    process_queue()

if __name__ == "__main__":
    process_queue()
