import os
import json
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from datetime import datetime
from dotenv import load_dotenv
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from twilio.rest import Client as TwilioClient
from typing import Optional
from fastapi import BackgroundTasks
from worker import load_private_key, generate_license_jwt, SessionLocal, process_all_messages
import requests


# import your short key generator
from generate_keys import generate_license_key

load_dotenv()


# --- Required env vars ---
DATABASE_URL = os.getenv("DATABASE_URL")
PRIVATE_KEY_ENV = os.getenv("PRIVATE_KEY")
PUBLIC_KEY_ENV = os.getenv("PUBLIC_KEY")
ISSUER = os.getenv("ISSUER", "Reed POS Technologies")
TW_SID = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TW_FROM = os.getenv("TWILIO_FROM")
BASE_URL = os.getenv("BASE_URL", "https://pos-license-server.onrender.com")
INTERNAL_SECRET=9c3f4a8e5b7d2f1a0e6c9d8b4f2a7e1c

# Ensure critical variables exist
if not all([DATABASE_URL, PRIVATE_KEY_ENV, PUBLIC_KEY_ENV]):
    raise RuntimeError("Set DATABASE_URL, PRIVATE_KEY, and PUBLIC_KEY in .env or Render environment")

# --- SQLAlchemy setup ---
engine = sa.create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine)

# --- Key loading helpers ---
def load_key_from_file(path: str):
    with open(path, "rb") as f:
        return f.read()

# Prefer env var first (raw PEM), fallback to file path if needed
if PRIVATE_KEY_ENV:
    PRIVATE_KEY = PRIVATE_KEY_ENV.encode() if isinstance(PRIVATE_KEY_ENV, str) else PRIVATE_KEY_ENV
else:
    PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")
    if not PRIVATE_KEY_PATH:
        raise RuntimeError("Provide PRIVATE_KEY env var or PRIVATE_KEY_PATH")
    PRIVATE_KEY = load_key_from_file(PRIVATE_KEY_PATH)

if PUBLIC_KEY_ENV:
    PUBLIC_KEY = PUBLIC_KEY_ENV.encode() if isinstance(PUBLIC_KEY_ENV, str) else PUBLIC_KEY_ENV
else:
    PUBLIC_KEY_PATH = os.getenv("PUBLIC_KEY_PATH")
    if not PUBLIC_KEY_PATH:
        raise RuntimeError("Provide PUBLIC_KEY env var or PUBLIC_KEY_PATH")
    PUBLIC_KEY = load_key_from_file(PUBLIC_KEY_PATH)

# --- Twilio client ---
twilio_client = TwilioClient(TW_SID, TW_TOKEN) if TW_SID and TW_TOKEN else None


# Ensure tables exist (run schema.sql separately in prod; quick create here for demo)
from pathlib import Path

# Ensure tables exist (one-time init)
if os.getenv("INIT_DB", "false").lower() == "true":
    schema_path = Path(__file__).parent / "schema.sql"

    if not schema_path.exists():
        raise RuntimeError(f"schema.sql not found at {schema_path}")

    with engine.begin() as conn:
        conn.execute(sa.text(schema_path.read_text()))

    print("✅ Database schema created successfully")


app = FastAPI(title="License backend")

# Pydantic models for webhook and activation
class PaymentWebhook(BaseModel):
    provider: str
    provider_order_id: str
    amount_cents: int
    currency: str
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    product_sku: str

class ActivationRequest(BaseModel):
    license_key: str
    terminal_id: str
    extra: dict = {}

def _get_last_activation_terminal(session, license_id):
    """
    Returns the most recent terminal_id for given license_id, or None if none exists.
    """
    row = session.execute(
        sa.text("SELECT terminal_id FROM license_activations WHERE license_id = :lid ORDER BY activated_at DESC LIMIT 1"),
        {"lid": license_id}
    ).first()
    return row[0] if row else None

@app.post("/webhook/payment")
async def webhook_payment(payload: PaymentWebhook, request: Request):
    session = SessionLocal()
    try:
        # check if order exists (idempotency)
        q = session.execute(
            sa.text("SELECT id, status FROM orders WHERE provider_order_id = :po"),
            {"po": payload.provider_order_id}
        ).first()
        if q:
            return {"ok": True, "message": "Already processed"}

        # insert order
        res = session.execute(sa.text(
            "INSERT INTO orders (provider, provider_order_id, amount_cents, currency, customer_phone, status) "
            "VALUES (:prov, :poid, :amt, :cur, :phone, 'paid') RETURNING id"
        ), {"prov": payload.provider, "poid": payload.provider_order_id, "amt": payload.amount_cents,
            "cur": payload.currency, "phone": payload.customer_phone})
        order_row = res.fetchone()
        if not order_row:
            raise RuntimeError("Failed to create order")
        order_id = order_row[0]
        session.commit()
        # 🔐 Notify Render main.py to send email
try:
    requests.post(
        f"{os.getenv('MAIN_APP_URL')}/internal/payment-confirmed",
        headers={
            "X-Internal-Secret": os.getenv("INTERNAL_SECRET"),
            "Content-Type": "application/json"
        },
        json={
            "email": payload.customer_email,
            "name": payload.customer_email,  # or real name if available
            "license_key": license_key,
            "order_id": payload.provider_order_id,
            "plan": payload.product_sku
        },
        timeout=10
    )
except Exception as e:
    # IMPORTANT: do NOT fail payment if email fails
    print("Email trigger failed:", e)


        # generate a short unique license_key and insert license row including the key (to satisfy NOT NULL)
        # pick who it is issued to (email if available else phone)
        issued_to = payload.customer_email or payload.customer_phone

        # generate and ensure uniqueness
        license_key = generate_license_key()
        exists = session.execute(sa.text("SELECT id FROM licenses WHERE license_key = :k"), {"k": license_key}).first()
        while exists:
            license_key = generate_license_key()
            exists = session.execute(sa.text("SELECT id FROM licenses WHERE license_key = :k"), {"k": license_key}).first()

        res2 = session.execute(sa.text(
            "INSERT INTO licenses (license_key, product_sku, order_id, issued_to, issued_phone, issued_email) "
            "VALUES (:key, :sku, :oid, :issued_to, :phone, :email) RETURNING id"
        ), {"key": license_key, "sku": payload.product_sku, "oid": order_id, "issued_to": issued_to,
            "phone": payload.customer_phone, "email": payload.customer_email})
        license_row = res2.fetchone()
        if not license_row:
            raise RuntimeError("Failed to create license")
        license_id = license_row[0]
        session.commit()

        # message text to send (short key)
        message = f"Thank you for your purchase.\nYour POS license key: {license_key}\nKeep it safe."

        # queue message(s) with explicit method column
        if payload.customer_phone:
            session.execute(sa.text(
                "INSERT INTO sms_messages (phone, message, license_id, method) VALUES (:phone, :msg, :lid, 'sms')"
            ), {"phone": payload.customer_phone, "msg": message, "lid": license_id})

        if payload.customer_email:
            session.execute(sa.text(
                "INSERT INTO sms_messages (email, message, license_id, method) VALUES (:email, :msg, :lid, 'email')"
            ), {"email": payload.customer_email, "msg": message, "lid": license_id})

        session.commit()

        return {"ok": True, "license": license_key}

    except Exception as ex:
        session.rollback()
        # return a helpful error message (avoid leaking secrets)
        raise HTTPException(status_code=500, detail=str(ex))
    finally:
        session.close()
        
@app.post("/payment-complete/")
async def payment_complete(email: str, product_sku: str, order_id: int, background_tasks: BackgroundTasks):
    """
    Called automatically when payment succeeds.
    Generates license, queues email, triggers background sending with retries.
    """
    # 1️⃣ Generate license token
    license_token = generate_license_jwt(PRIVATE_KEY, license_id=order_id,
                                         product_sku=product_sku, order_id=order_id,
                                         issuer=ISSUER, expires_days=365)

    # 2️⃣ Queue the email
    session = SessionLocal()
    session.execute(sa.text("""
        INSERT INTO sms_messages (email, message, method, status, attempts, created_at)
        VALUES (:email, :message, 'email', 'queued', 0, now())
    """), {"email": email, "message": f"Here is your license: {license_token}"})
    session.commit()
    session.close()

    # 3️⃣ Trigger background sending with automatic retries
    background_tasks.add_task(process_all_messages)

    return {"message": "Payment successful! License email will be sent automatically."}

@app.post("/send-all-emails/")
async def send_all_emails(background_tasks: BackgroundTasks):
    """
    Process all queued emails in the database in the background.
    """
    background_tasks.add_task(process_all_messages)
    return {"message": "All queued emails will be processed in the background."}

@app.post("/licenses/activate")
async def activate_license(req: ActivationRequest):
    session = SessionLocal()
    try:
        lic = session.execute(sa.text("SELECT id, status, activated FROM licenses WHERE license_key = :tok"),
                              {"tok": req.license_key}).first()
        if not lic:
            raise HTTPException(status_code=404, detail="License not found")
        license_id = lic[0]
        status = lic[1]
        activated = lic[2]

        if status in ('revoked', 'expired'):
            raise HTTPException(status_code=400, detail="License is not valid")

        # get last activation terminal (if any)
        last_terminal = _get_last_activation_terminal(session, license_id)

        if activated:
            # If already activated, only allow activation from the same terminal.
            if last_terminal and last_terminal != req.terminal_id:
                # License has been activated on another terminal -> block.
                raise HTTPException(status_code=400, detail="License already activated on another terminal")
            else:
                # Same terminal re-activation: log an activation event and refresh activated_at
                session.execute(sa.text(
                    "INSERT INTO license_activations (license_id, terminal_id, activated_at) VALUES (:lid, :tid, now())"
                ), {"lid": license_id, "tid": req.terminal_id})
                session.execute(sa.text(
                    "UPDATE licenses SET activated = true, activated_at = now() WHERE id = :lid"
                ), {"lid": license_id})
                session.commit()
                return {"ok": True, "message": "License re-activated for same terminal"}

        # Not activated yet: if there's a recorded terminal (rare) that differs, block to be safe
        if last_terminal and last_terminal != req.terminal_id:
            raise HTTPException(status_code=400, detail="License has previous activation on a different terminal and cannot be activated here")

        # record activation
        session.execute(sa.text(
            "INSERT INTO license_activations (license_id, terminal_id, activated_at) VALUES (:lid, :tid, now())"
        ), {"lid": license_id, "tid": req.terminal_id})
        session.execute(sa.text(
            "UPDATE licenses SET activated = true, activated_at = now() WHERE id = :lid"
        ), {"lid": license_id})
        session.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as ex:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(ex))
    finally:
        session.close()

@app.get("/licenses/verify/{license_key}")
async def verify_license(license_key: str):
    session = SessionLocal()
    try:
        lic = session.execute(sa.text("""
            SELECT id, status, activated, expires_at 
            FROM licenses WHERE license_key = :key
        """), {"key": license_key}).first()

        if not lic:
            raise HTTPException(status_code=404, detail="License not found")

        status, activated, expires_at = lic[1], lic[2], lic[3]

        # Check status
        if status in ("revoked", "expired"):
            raise HTTPException(status_code=400, detail="License invalid or expired")

        # Check expiry
        if expires_at and expires_at < datetime.utcnow():
            raise HTTPException(status_code=400, detail="License expired")

        return {"ok": True, "activated": activated, "status": status}

    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))
    finally:
        session.close()

@app.get("/public_key")
async def public_key():
    # client can download embedded public key (or bundle in installer)
    return {"public_key": PUBLIC_KEY.decode() if isinstance(PUBLIC_KEY, (bytes, bytearray)) else PUBLIC_KEY}

# main.py (Render)


app = Flask(__name__)

INTERNAL_SECRET = os.getenv("INTERNAL_SECRET")

@app.route("/internal/payment-confirmed", methods=["POST"])
def payment_confirmed():
    auth = request.headers.get("X-Internal-Secret")
    if auth != INTERNAL_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json or {}

    email = data.get("email")
    name = data.get("name")
    license_key = data.get("license_key")
    order_id = data.get("order_id")
    plan = data.get("plan")

    if not all([email, license_key, order_id]):
        return jsonify({"error": "Missing data"}), 400

    send_license_email(
        email=email,
        name=name,
        license_key=license_key,
        order_id=order_id,
        plan=plan
    )

    return jsonify({"success": True}), 200

