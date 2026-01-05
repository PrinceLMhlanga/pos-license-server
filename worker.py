# worker.py
import os
import json
import logging
import smtplib
from email.mime.text import MIMEText
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logging.error("DATABASE_URL not set. Exiting.")
    raise RuntimeError("DATABASE_URL not configured")

MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", 5))

engine = sa.create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine)

EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_USER)
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() in ("1", "true", "yes")

if not (EMAIL_USER and EMAIL_PASS):
    logging.warning("Email credentials not fully configured.")


def send_email(to_email, subject, body, timeout=20):
    """Synchronously send an email via SMTP."""
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


def process_all_messages():
    """Process all queued email messages."""
    session = SessionLocal()
    processed_ids = []
    try:
        # Select all queued emails
        rows = session.execute(sa.text("""
            SELECT id, email, message, attempts
            FROM sms_messages
            WHERE status = 'queued' AND method='email'
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
        """)).all()

        if not rows:
            logging.info("No queued emails to process.")
            return processed_ids

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

            try:
                ok, resp = send_email(email, "Your License Key", message)
                if not ok:
                    raise RuntimeError(resp)

                # Mark as sent
                session.execute(sa.text("""
                    UPDATE sms_messages
                    SET status='sent', response_json=:resp, sent_at=now()
                    WHERE id=:id
                """), {"resp": json.dumps({"info": resp}), "id": msg_id})
                session.commit()
                processed_ids.append(msg_id)
                logging.info("Email id=%s sent successfully", msg_id)

            except Exception as ex:
                logging.exception("Email id=%s failed", msg_id)
                status = "failed" if attempts + 1 >= MAX_ATTEMPTS else "queued"
                session.execute(sa.text("""
                    UPDATE sms_messages
                    SET status=:status, response_json=:resp, last_attempt_at=now()
                    WHERE id=:id
                """), {"status": status, "resp": json.dumps({"error": str(ex)}), "id": msg_id})
                session.commit()

    finally:
        session.close()
    return processed_ids
