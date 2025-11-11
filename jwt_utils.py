import time
import jwt
from datetime import datetime, timedelta
from typing import Optional

def load_private_key(path: str) -> str:
    with open(path, "rb") as f:
        return f.read()

def load_public_key(path: str) -> str:
    with open(path, "rb") as f:
        return f.read()

def generate_license_jwt(private_key_pem: bytes, license_id: int, product_sku: str, order_id: int, issuer: str, expires_days: Optional[int] = None) -> str:
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
    # PyJWT returns str in modern versions
    return token