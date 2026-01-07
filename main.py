import os
import json
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, APIRouter
from pydantic import BaseModel
from datetime import datetime
from dotenv import load_dotenv
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from typing import Optional
from worker import load_private_key, generate_license_jwt, SessionLocal, process_all_messages
from generate_keys import generate_license_key
from paynow import Paynow
load_dotenv()
router = APIRouter()

# --- ENVIRONMENT VARIABLES ---
DATABASE_URL = os.getenv("DATABASE_URL")
PRIVATE_KEY_ENV = os.getenv("PRIVATE_KEY")
PUBLIC_KEY_ENV = os.getenv("PUBLIC_KEY")
ISSUER = os.getenv("ISSUER", "Reed POS Technologies")
TW_SID = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TW_FROM = os.getenv("TWILIO_FROM")
BASE_URL = os.getenv("BASE_URL", "https://pos-license-server.onrender.com")
INTERNAL_SECRET = os.getenv("INTERNAL_SECRET")
PAYNOW_INTEGRATION_ID = os.getenv("PAYNOW_INTEGRATION_ID")
PAYNOW_INTEGRATION_KEY = os.getenv("PAYNOW_INTEGRATION_KEY")
PAYNOW_RETURN_URL = os.getenv("PAYNOW_RETURN_URL", "https://pos-license-server.onrender.com/payment/return") # not strictly needed
PAYNOW_RESULT_URL = os.getenv("PAYNOW_RESULT_URL", "https://pos-license-server.onrender.com/webhook/payment") # this is the webhook

paynow = Paynow(
    PAYNOW_INTEGRATION_ID,
    PAYNOW_INTEGRATION_KEY,
    PAYNOW_RETURN_URL,
    PAYNOW_RESULT_URL
)

class StartPaynowRequest(BaseModel):
    email: str = None
    phone: str = None
    product: str
    amount: float



if not all([DATABASE_URL, PRIVATE_KEY_ENV, PUBLIC_KEY_ENV]):
    raise RuntimeError("Set DATABASE_URL, PRIVATE_KEY, and PUBLIC_KEY in environment or .env")

# --- SQLAlchemy ---
engine = sa.create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine)

# --- Key loading ---
PRIVATE_KEY = PRIVATE_KEY_ENV.encode() if isinstance(PRIVATE_KEY_ENV, str) else PRIVATE_KEY_ENV
PUBLIC_KEY = PUBLIC_KEY_ENV.encode() if isinstance(PUBLIC_KEY_ENV, str) else PUBLIC_KEY_ENV

# --- FastAPI app ---
app = FastAPI(title="License backend")

# --- Pydantic Models ---
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

# --- Helper for activation history ---
def _get_last_activation_terminal(session, license_id):
    row = session.execute(
        sa.text("SELECT terminal_id FROM license_activations WHERE license_id = :lid ORDER BY activated_at DESC LIMIT 1"),
        {"lid": license_id}
    ).first()
    return row[0] if row else None
@router.post("/paynow/start")
def start_paynow_payment(req: StartPaynowRequest):
    try:
        if not req.email and not req.phone:
            raise HTTPException(
                status_code=400,
                detail="Email or phone number is required"
            )

        # Create Paynow payment
        payment = paynow.create_payment(
            reference=f"{(req.email or req.phone)}-{req.product}",
            email=req.email or "buyer@unknown.com"
        )

        # Add item (currency MUST match your Paynow account)
        payment.add(
            req.product,
            float(req.amount),
            currency="USD"
        )

        # Mobile payment (EcoCash / OneMoney)
        if req.phone:
            payment.paynow_mobile = req.phone

        # Send payment to Paynow
        response = paynow.send(payment)

        if not response.success:
            raise HTTPException(
                status_code=400,
                detail=f"Paynow initiation failed: {response.errors}"
            )

        return {
            "redirect_url": response.redirect_url,
            "poll_url": response.poll_url,
            "reference": response.poll_url.split("/")[-1]
        }

    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))

@app.get("/orders/by-reference/{reference}")
async def get_license_by_reference(reference: str):
    session = SessionLocal()
    try:
        row = session.execute(sa.text("""
            SELECT l.license_key
            FROM orders o
            JOIN licenses l ON l.order_id = o.id
            WHERE o.provider_order_id = :ref
        """), {"ref": reference}).first()

        if not row:
            return {"ok": False}

        return {
            "ok": True,
            "license": row.license_key
        }
    finally:
        session.close()

# --- PAYMENT WEBHOOK: where your Flask server POSTs successful payment ---
@app.post("/webhook/payment")
async def webhook_payment(payload: PaymentWebhook, background_tasks: BackgroundTasks):
    """
    Accepts payment notifications from the website/checkout backend and provisions licenses.
    """
    session = SessionLocal()
    try:
        # 1. Idempotency: Ignore if already processed
        q = session.execute(
            sa.text("SELECT id FROM orders WHERE provider_order_id = :po"),
            {"po": payload.provider_order_id}
        ).first()
        if q:
            return {"ok": True, "message": "Already processed"}

        # 2. Insert order (for reference/traceability)
        res = session.execute(sa.text(
            "INSERT INTO orders (provider, provider_order_id, amount_cents, currency, customer_phone, status) "
            "VALUES (:prov, :poid, :amt, :cur, :phone, 'paid') RETURNING id"
        ), {
            "prov": payload.provider, 
            "poid": payload.provider_order_id,
            "amt": payload.amount_cents,
            "cur": payload.currency,
            "phone": payload.customer_phone
        })
        order_row = res.fetchone()
        if not order_row:
            raise RuntimeError("Failed to create order")
        order_id = order_row[0]
        session.commit()

        # 3. Generate unique license key
        issued_to = payload.customer_email or payload.customer_phone
        license_key = generate_license_key()
        exists = session.execute(sa.text(
            "SELECT id FROM licenses WHERE license_key = :k"
        ), {"k": license_key}).first()
        while exists:
            license_key = generate_license_key()
            exists = session.execute(sa.text(
                "SELECT id FROM licenses WHERE license_key = :k"
            ), {"k": license_key}).first()

        res2 = session.execute(sa.text(
            "INSERT INTO licenses (license_key, product_sku, order_id, issued_to, issued_phone, issued_email) "
            "VALUES (:key, :sku, :oid, :issued_to, :phone, :email) RETURNING id"
        ), {
            "key": license_key, 
            "sku": payload.product_sku, 
            "oid": order_id,
            "issued_to": issued_to,
            "phone": payload.customer_phone, 
            "email": payload.customer_email
        })
        license_row = res2.fetchone()
        if not license_row:
            raise RuntimeError("Failed to create license")
        license_id = license_row[0]
        session.commit()

        # 4. Queue outgoing messages (SMS or Email if present)
        message = f"Thank you for your purchase.\nYour POS license key: {license_key}\nKeep it safe."
        if payload.customer_phone:
            session.execute(sa.text(
                "INSERT INTO sms_messages (phone, message, license_id, method, status) "
                "VALUES (:phone, :msg, :lid, 'sms', 'queued')"
            ), {"phone": payload.customer_phone, "msg": message, "lid": license_id})
        if payload.customer_email:
            session.execute(sa.text(
                "INSERT INTO sms_messages (email, message, license_id, method, status) "
                "VALUES (:email, :msg, :lid, 'email', 'queued')"
            ), {"email": payload.customer_email, "msg": message, "lid": license_id})
        session.commit()

        # 5. Kick off background messaging
        background_tasks.add_task(process_all_messages)

        # 6. Return license key (useful for API tests)
        return {"ok": True, "license": license_key}

    except Exception as ex:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(ex))
    finally:
        session.close()

# --- LICENSE ACTIVATION (client hits with key+terminal id) ---
@app.post("/licenses/activate")
async def activate_license(req: ActivationRequest):
    session = SessionLocal()
    try:
        lic = session.execute(sa.text(
            "SELECT id, status, activated FROM licenses WHERE license_key = :tok"
        ), {"tok": req.license_key}).first()
        if not lic:
            raise HTTPException(status_code=404, detail="License not found")
        license_id, status, activated = lic

        if status in ("revoked", "expired"):
            raise HTTPException(status_code=400, detail="License is not valid")

        last_terminal = _get_last_activation_terminal(session, license_id)
        if activated:
            if last_terminal and last_terminal != req.terminal_id:
                raise HTTPException(status_code=400, detail="License already activated on another terminal")
            # Same terminal re-activation: log + update timestamp
            session.execute(sa.text(
                "INSERT INTO license_activations (license_id, terminal_id, activated_at) VALUES (:lid, :tid, now())"
            ), {"lid": license_id, "tid": req.terminal_id})
            session.execute(sa.text(
                "UPDATE licenses SET activated = true, activated_at = now() WHERE id = :lid"
            ), {"lid": license_id})
            session.commit()
            return {"ok": True, "message": "License re-activated for same terminal"}

        # Not yet activated/other terminal
        if last_terminal and last_terminal != req.terminal_id:
            raise HTTPException(status_code=400, detail="License has previous activation on a different terminal and cannot be activated here")
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

# --- LICENSE VERIFICATION (for client/installer) ---
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
        _, status, activated, expires_at = lic
        if status in ("revoked", "expired"):
            raise HTTPException(status_code=400, detail="License invalid or expired")
        if expires_at and expires_at < datetime.utcnow():
            raise HTTPException(status_code=400, detail="License expired")
        return {"ok": True, "activated": activated, "status": status}
    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))
    finally:
        session.close()

# --- PUBLIC KEY DOWNLOAD ---
@app.get("/public_key")
async def public_key():
    return {"public_key": PUBLIC_KEY.decode() if isinstance(PUBLIC_KEY, (bytes, bytearray)) else PUBLIC_KEY}
# at bottom of main.py
app.include_router(router)
