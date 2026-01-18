import os
import time
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
import requests
from requests.auth import HTTPBasicAuth
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, String, DateTime
from cryptography.hazmat.primitives import serialization
import json, base64
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from sqlalchemy import create_engine
from sqlalchemy import text

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
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET")
PAYPAL_BASE_URL = os.getenv("PAYPAL_BASE_URL", "https://api-m.paypal.com")
PAYPAL_CURRENCY=os.getenv("PAYPAL_CURRENCY")
paynow = Paynow(
    PAYNOW_INTEGRATION_ID,
    PAYNOW_INTEGRATION_KEY,
    PAYNOW_RETURN_URL,
    PAYNOW_RESULT_URL
)
if not PRIVATE_KEY_ENV:
    raise RuntimeError("PRIVATE_KEY_ENV not set")

PRIVATE_KEY = serialization.load_pem_private_key(
    PRIVATE_KEY_ENV.replace("\\n", "\n").encode(),
    password=None
)
class StartPaynowRequest(BaseModel):
    email: str = None
    phone: str = None
    product: str
    amount: float

def paypal_headers():
    r = requests.post(
        f"{PAYPAL_BASE_URL}/v1/oauth2/token",
        auth=HTTPBasicAuth(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=10
    )
    r.raise_for_status()
    return r.json()["access_token"]

def create_signed_license(payload: dict) -> str:
    """
    Returns BASE64(JSON({ payload, signature }))
    """

    # 1️⃣ Canonical JSON (MUST match VB expectations)
    payload_bytes = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True
    ).encode()

    # 2️⃣ Base64 payload
    payload_b64 = base64.b64encode(payload_bytes).decode()

    # 3️⃣ Sign BASE64 payload (IMPORTANT)
    signature = base64.b64encode(
        PRIVATE_KEY.sign(
            payload_b64.encode(),
            padding.PKCS1v15(),
            hashes.SHA256()
        )
    ).decode()

    # 4️⃣ Final license blob
    license_blob = base64.b64encode(
        json.dumps({
            "payload": payload_b64,
            "signature": signature
        }, separators=(",", ":")).encode()
    ).decode()

    return license_blob




if not all([DATABASE_URL, PRIVATE_KEY_ENV, PUBLIC_KEY_ENV]):
    raise RuntimeError("Set DATABASE_URL, PRIVATE_KEY, and PUBLIC_KEY in environment or .env")



# --- SQLAlchemy production setup ---
engine = create_engine(
    DATABASE_URL,
    echo=False,            # Keep False in production
    future=True,           # Keep True if using 2.0 style
    pool_size=5,           # Max active connections to DB
    max_overflow=5,        # Extra connections beyond pool_size
    pool_pre_ping=True,    # Check connection before using
    pool_recycle=300       # Recycle idle connections after 5 minutes
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)



# --- Key loading ---

if not PRIVATE_KEY_ENV or not PUBLIC_KEY_ENV:
    raise RuntimeError("RSA keys not configured")

PRIVATE_KEY = serialization.load_pem_private_key(
    PRIVATE_KEY_ENV.replace("\\n", "\n").encode(),
    password=None
)

PUBLIC_KEY = serialization.load_pem_public_key(
    PUBLIC_KEY_ENV.replace("\\n", "\n").encode()
)

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
Base = declarative_base()


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    provider = Column(String, nullable=False)
    provider_order_id = Column(String, nullable=False, unique=True)
    poll_url = Column(String, nullable=False)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
class ActivationRequest(BaseModel):
    license: str
    terminal_id: str
   
class PaymentCheckRequest(BaseModel):
    provider: str
    reference: str

# after Base = declarative_base() and model classes and after engine = sa.create_engine(...)
Base.metadata.create_all(bind=engine)
def issue_license_for_order(session, provider, provider_order_id, product, email=None, phone=None):
    # idempotency
    existing = session.execute(sa.text("""
        SELECT l.license_key
        FROM orders o
        JOIN licenses l ON l.order_id = o.id
        WHERE o.provider = :prov AND o.provider_order_id = :poid
    """), {"prov": provider, "poid": provider_order_id}).first()

    if existing:
        return existing[0]

    # create order
    res = session.execute(sa.text("""
        INSERT INTO orders (provider, provider_order_id, status)
        VALUES (:prov, :poid, 'paid')
        RETURNING id
    """), {"prov": provider, "poid": provider_order_id})
    order_id = res.fetchone()[0]

    # generate unique license
    license_key = generate_license_key()
    while session.execute(
        sa.text("SELECT 1 FROM licenses WHERE license_key = :k"),
        {"k": license_key}
    ).first():
        license_key = generate_license_key()

    session.execute(sa.text("""
        INSERT INTO licenses (license_key, product_sku, order_id, issued_phone, issued_email)
        VALUES (:k, :sku, :oid, :phone, :email)
    """), {
        "k": license_key,
        "sku": product,
        "oid": order_id,
        "phone": phone,
        "email": email
    })

    session.commit()
    return license_key

def verify_and_extract_license(signed_license: str) -> dict:
    try:
        decoded = base64.b64decode(signed_license).decode()
        obj = json.loads(decoded)

        payload_b64 = obj["payload"]
        signature = base64.b64decode(obj["signature"])

        # verify signature against BASE64 payload
        PUBLIC_KEY.verify(
            signature,
            payload_b64.encode(),
            padding.PKCS1v15(),
            hashes.SHA256()
        )

        # decode payload
        payload_json = base64.b64decode(payload_b64).decode()
        return json.loads(payload_json)

    except Exception:
        raise HTTPException(status_code=400, detail="Invalid license format or signature")

def paypal_get_order(order_id):
    token = paypal_headers()
    r = requests.get(
        f"{PAYPAL_BASE_URL}/v2/checkout/orders/{order_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10
    )
    r.raise_for_status()
    return r.json()

def paypal_capture_order(order_id: str):
    access_token = paypal_headers()

    r = requests.post(
        f"{PAYPAL_BASE_URL}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        timeout=10
    )

    r.raise_for_status()
    return r.json()

import requests
from urllib.parse import urljoin

def paynow_check_status(session, reference: str) -> str:
    """
    Poll Paynow LIVE to check payment status.
    Returns: 'paid', 'pending', or 'failed'
    """

    payment = session.query(Payment).filter_by(
        provider="paynow",
        provider_order_id=reference
    ).first()

    if not payment:
        raise Exception(f"Payment record not found for reference {reference}")

    # ✅ ALWAYS use the poll_url returned by Paynow
    url = payment.poll_url

    data = {
        "id": PAYNOW_INTEGRATION_ID,
        "key": PAYNOW_INTEGRATION_KEY,
    }

    try:
        r = requests.post(url, data=data, timeout=10)
        r.raise_for_status()
        raw = r.text.strip().lower()
    except Exception as e:
        raise Exception(f"Failed to poll Paynow transaction: {e}")

    # 🧠 Paynow authoritative logic
    if "paid=true" in raw or "status=paid" in raw:
        mapped_status = "paid"

    elif any(x in raw for x in [
        "failed",
        "cancelled",
        "canceled",
        "expired",
        "error",
        "timeout",
        "timed out",
        "insufficient funds"
    ]):
        mapped_status = "failed"

    else:
        mapped_status = "pending"

    # ✅ Update DB
    payment.status = mapped_status
    session.commit()

    return mapped_status


# --- Helper for activation history ---
def _get_last_activation_terminal(session, license_id):
    row = session.execute(
        sa.text("SELECT terminal_id FROM license_activations WHERE license_id = :lid ORDER BY activated_at DESC LIMIT 1"),
        {"lid": license_id}
    ).first()
    return row[0] if row else None
@router.post("/paynow/start")
def start_paynow_payment(req: StartPaynowRequest):
    session = SessionLocal()
    try:
        reference = f"POS-{int(time.time())}"

        payment_req = paynow.create_payment(
            reference,
            req.email or "buyer@swiftpos.co.zw"
        )

        payment_req.add(req.product, float(req.amount))

        if req.phone:
            payment_req.paynow_mobile = req.phone

        response = paynow.send(payment_req)

        if not response.success:
            raise HTTPException(status_code=400, detail=response.errors)

        # 🔑 SAVE PAYMENT (THIS WAS MISSING)
        payment = Payment(
            provider="paynow",
            provider_order_id=reference,
            poll_url=response.poll_url,
            status="pending"
        )

        session.add(payment)
        session.commit()

        return {
            "ok": True,
            "redirect_url": response.redirect_url,
            "reference": reference
        }

    except Exception as ex:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(ex))
    finally:
        session.close()




from fastapi import HTTPException
import traceback
email = getattr(req, "customer_email", None) or getattr(req, "email", None)
phone = getattr(req, "customer_phone", None) or getattr(req, "phone", None)

@router.post("/payment/check")
def check_payment(req: PaymentCheckRequest):
    session = SessionLocal()
    try:
        # Validate request
        if not getattr(req, "provider", None) or not getattr(req, "reference", None):
            raise HTTPException(status_code=400, detail="provider and reference are required")

        provider = req.provider.lower().strip()

        # =========================
        # PAYNOW
        # =========================
        if provider == "paynow":
            try:
                raw_status = paynow_check_status(session, req.reference)
            except Exception as e:
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"paynow_check_status error: {e}")

            if raw_status is None:
                raise HTTPException(status_code=500, detail="paynow_check_status returned None")

            # Normalize to string safely
            try:
                status_text = str(raw_status).strip().lower()
            except Exception as e:
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"Invalid paynow status type: {type(raw_status)}")

            # Map PayNow statuses to canonical statuses
            status_map = {
                "paid": "paid",
                "awaiting delivery": "paid",
                "awaiting payment": "pending",
                "created": "pending",
                "cancelled": "failed",
                "failed": "failed",
                "disputed": "failed",
            }
            status = status_map.get(status_text, "pending")

            # Not paid yet
            if status != "paid":
                return {"ok": False, "status": status}

            # Paid → issue or fetch existing license
            try:
                license_key = issue_license_for_order(
                    session=session,
                    provider="paynow",
                    provider_order_id=req.reference,
                    product="SWIFTPOS_SINGLE",
                    email=email,  # pick the correct field
                    phone=phone
                )
                license_payload = {
                    "license_key": license_key,
                    "product": "SWIFTPOS_SINGLE",
                    "provider": "paynow",
                    "order_id": req.reference,
                    "issued_at": int(time.time())
                }
                signed_license = create_signed_license(license_payload)  # returns BASE64(payload).BASE64(signature)
                session.commit()
            except Exception as e:
                traceback.print_exc()
                session.rollback()
                raise HTTPException(status_code=500, detail=f"Failed to issue license: {e}")

            return {"ok": True, "status": "paid", "license": signed_license}

        # =========================
        # PAYPAL
        # =========================
        if provider == "paypal":
            try:
                order = paypal_get_order(req.reference)
            except Exception as e:
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"paypal_get_order error: {e}")

            if order.get("status") == "CREATED":
                return {"ok": False, "status": "pending"}

            if order.get("status") == "APPROVED":
                try:
                    order = paypal_capture_order(req.reference)
                except Exception as e:
                    traceback.print_exc()
                    raise HTTPException(status_code=500, detail=f"paypal_capture_order error: {e}")

            if order.get("status") != "COMPLETED":
                return {"ok": False, "status": order.get("status", "").lower()}

            try:
                capture = order["purchase_units"][0]["payments"]["captures"][0]
            except Exception:
                traceback.print_exc()
                raise HTTPException(status_code=500, detail="Unexpected PayPal order structure")

            if capture.get("status") != "COMPLETED":
                return {"ok": False, "status": capture.get("status", "").lower()}

            try:
                license_key = issue_license_for_order(
                    session=session,
                    provider="paypal",
                    provider_order_id=req.reference,
                    product="SWIFTPOS_SINGLE",
                    email=email,  # pick the correct field
                    phone=phone
                )

                license_payload = {
                    "license_key": license_key,
                    "product": "SWIFTPOS_SINGLE",
                    "provider": "paypal",
                    "order_id": req.reference,
                    "issued_at": int(time.time())
                }
                signed_license = create_signed_license(license_payload)  # returns BASE64(payload).BASE64(signature)
                session.commit()
            except Exception as e:
                traceback.print_exc()
                session.rollback()
                raise HTTPException(status_code=500, detail=f"Failed to issue license: {e}")

            return {"ok": True, "status": "paid", "license": signed_license}

        # =========================
        # UNKNOWN PROVIDER
        # =========================
        raise HTTPException(status_code=400, detail="Unknown payment provider")

    except HTTPException:
        # Re-raise HTTPExceptions unchanged
        raise

    except Exception as ex:
        # Log and return a generic server error
        traceback.print_exc()
        session.rollback()
        raise HTTPException(status_code=500, detail="Payment check failed")

    finally:
        session.close()

@router.post("/paypal/start")
def start_paypal_payment(req: StartPaynowRequest):
    access_token = paypal_headers()

    order = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {
                "currency_code": PAYPAL_CURRENCY,
                "value": f"{req.amount:.2f}"
            },
            "description": req.product
        }],
        "application_context": {
            "return_url": f"{BASE_URL}/payment/return",
            "cancel_url": f"{BASE_URL}/payment/cancel"
        }
    }

    r = requests.post(
        f"{PAYPAL_BASE_URL}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=order,
        timeout=10
    )
    r.raise_for_status()

    data = r.json()
    approval_url = next(
        link["href"] for link in data["links"]
        if link["rel"] == "approve"
    )

    return {
        "redirect_url": approval_url,
        "order_id": data["id"]
    }

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
from fastapi import HTTPException
import base64, json
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from datetime import datetime

@app.post("/licenses/activate")
async def activate_license(req: ActivationRequest):
    session = SessionLocal()
    try:
        # =========================
        # 1️⃣ Decode signed license
        # =========================
        try:
            decoded_json = base64.b64decode(req.license).decode()
            license_obj = json.loads(decoded_json)

            payload_b64 = license_obj["payload"]          # 🔑 BASE64 payload
            signature = base64.b64decode(license_obj["signature"])
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid license format")

        # =========================
        # 2️⃣ Verify signature (IMPORTANT FIX)
        # =========================
        try:
            PUBLIC_KEY.verify(
                signature,
                payload_b64.encode(),   # ✅ VERIFY EXACT DATA THAT WAS SIGNED
                padding.PKCS1v15(),
                hashes.SHA256()
            )
        except Exception:
            raise HTTPException(status_code=400, detail="License signature invalid")

        # =========================
        # 3️⃣ Decode payload AFTER verification
        # =========================
        try:
            payload_json = base64.b64decode(payload_b64).decode()
            payload = json.loads(payload_json)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid license payload")

        license_key = payload.get("license_key")
        if not license_key:
            raise HTTPException(status_code=400, detail="License payload missing license_key")

        # =========================
        # 4️⃣ Fetch license from DB
        # =========================
        lic = session.execute(sa.text("""
            SELECT id, status, activated, expires_at
            FROM licenses
            WHERE license_key = :k
        """), {"k": license_key}).first()

        if not lic:
            raise HTTPException(status_code=404, detail="License not found")

        license_id, status, activated, expires_at = lic

        if status in ("revoked", "expired"):
            raise HTTPException(status_code=400, detail="License is not valid")

        if expires_at and expires_at < datetime.utcnow():
            raise HTTPException(status_code=400, detail="License expired")

        # =========================
        # 5️⃣ Activation logic
        # =========================
        last_terminal = _get_last_activation_terminal(session, license_id)

        if activated and last_terminal and last_terminal != req.terminal_id:
            raise HTTPException(
                status_code=400,
                detail="License already activated on another terminal"
            )

        session.execute(sa.text("""
            INSERT INTO license_activations (license_id, terminal_id, activated_at)
            VALUES (:lid, :tid, now())
        """), {"lid": license_id, "tid": req.terminal_id})

        session.execute(sa.text("""
            UPDATE licenses
            SET activated = true, activated_at = now()
            WHERE id = :lid
        """), {"lid": license_id})

        session.commit()

        # =========================
        # 6️⃣ Re-issue signed license (terminal-bound)
        # =========================
        new_payload = {
            "license_key": license_key,
            "license_id": license_id,
            "terminal_id": req.terminal_id,
            "issued_at": datetime.utcnow().isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None
        }

        signed_license = create_signed_license(new_payload)

        return {
            "ok": True,
            "license": signed_license
        }

    except HTTPException:
        raise
    except Exception as ex:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(ex))
    finally:
        session.close()
from fastapi.responses import HTMLResponse
from fastapi import Request

@app.get("/payment/return", response_class=HTMLResponse)
async def payment_return(request: Request):
    html_content = """
    <html>
        <head>
            <title>Return to SwiftPOS</title>
            <style>
                body {
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    background-color: #f0f2f5;
                    margin: 0;
                }
                .card {
                    background-color: #fff;
                    padding: 40px;
                    border-radius: 16px;
                    box-shadow: 0 10px 30px rgba(0,0,0,0.1);
                    text-align: center;
                    max-width: 500px;
                }
                .title {
                    font-size: 26px;
                    font-weight: 700;
                    color: #4CAF50;
                    margin-bottom: 20px;
                }
                .message {
                    font-size: 16px;
                    color: #333;
                    line-height: 1.5;
                }
            </style>
        </head>
        <body>
            <div class="card">
                <div class="title">Return to SwiftPOS</div>
                <div class="message">
                    Please return to your SwiftPOS desktop app and click <strong>'Check Payment'</strong> 
                    to view your payment status.<br><br>
                    If your payment was successful, the license will be autofilled automatically. 
                    You can then click <strong>'Activate'</strong> to proceed.
                </div>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@router.get("/payment/cancel", response_class=HTMLResponse)
def payment_cancel():
    html_content = """
    <html>
        <head>
            <title>Payment Cancelled</title>
            <style>
                body {
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    background-color: #f0f2f5;
                    margin: 0;
                }
                .card {
                    background-color: #fff;
                    padding: 40px;
                    border-radius: 16px;
                    box-shadow: 0 10px 30px rgba(0,0,0,0.1);
                    text-align: center;
                    max-width: 500px;
                }
                .title {
                    font-size: 26px;
                    font-weight: 700;
                    color: #e74c3c;
                    margin-bottom: 20px;
                }
                .message {
                    font-size: 16px;
                    color: #333;
                    line-height: 1.5;
                }
            </style>
        </head>
        <body>
            <div class="card">
                <div class="title">Payment Cancelled ❌</div>
                <div class="message">
                    You cancelled the payment. No money was charged.<br><br>
                    You can safely return to the SwiftPOS app and try again if you wish.
                </div>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)


# --- LICENSE VERIFICATION (ONLINE / INSTALLER / SUPPORT) ---
@app.get("/licenses/verify/{license_key}")
async def verify_license(
    license_key: str,
    terminal_id: str | None = None
):
    session = SessionLocal()
    try:
        lic = session.execute(sa.text("""
            SELECT id, status, activated, expires_at
            FROM licenses
            WHERE license_key = :key
        """), {"key": license_key}).first()

        if not lic:
            raise HTTPException(status_code=404, detail="License not found")

        license_id, status, activated, expires_at = lic

        if status in ("revoked", "expired"):
            raise HTTPException(
                status_code=400,
                detail="License revoked or expired"
            )

        if expires_at and expires_at < datetime.utcnow():
            raise HTTPException(
                status_code=400,
                detail="License expired"
            )

        # 🔐 OPTIONAL TERMINAL CHECK (ONLINE VALIDATION)
        if terminal_id:
            last_terminal = _get_last_activation_terminal(session, license_id)
            if last_terminal and last_terminal != terminal_id:
                raise HTTPException(
                    status_code=400,
                    detail="License is bound to a different terminal"
                )

        return {
            "ok": True,
            "license_id": license_id,
            "activated": activated,
            "status": status,
            "expires_at": expires_at.isoformat() if expires_at else None
        }

    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))
    finally:
        session.close()
@app.get("/health")
def health_check():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))  # simple query to test DB
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        return {"status": "error", "details": str(e)}
# --- PUBLIC KEY DOWNLOAD ---
@app.get("/public_key")
async def public_key():
    return {"public_key": PUBLIC_KEY.decode() if isinstance(PUBLIC_KEY, (bytes, bytearray)) else PUBLIC_KEY}
# at bottom of main.py
app.include_router(router)
