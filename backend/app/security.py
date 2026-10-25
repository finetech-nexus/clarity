import hashlib
import hmac
from dataclasses import dataclass

import jwt

from app.timeutil import utcnow


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str, salt: str = "clarity-demo") -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt = stored.split("$", 1)[0]
    return hmac.compare_digest(hash_password(password, salt), stored)


def encode_jwt(secret: str, payload: dict, ttl_minutes: int) -> str:
    body = dict(payload)
    body["exp"] = int(utcnow().timestamp()) + ttl_minutes * 60
    return jwt.encode(body, secret, algorithm="HS256")


def decode_jwt(secret: str, token: str) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"])


def sign_payload(secret: str, payload: dict) -> str:
    import json

    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def verify_payload_signature(secret: str, payload: dict, signature: str) -> bool:
    if not signature:
        return False
    expected = sign_payload(secret, payload)
    return hmac.compare_digest(expected.lower(), signature.lower())


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    surface: str
    user_id: str | None
    email: str | None
    role: str
    credential_id: str | None = None

    def allows(self, *roles: str) -> bool:
        if self.role == "admin":
            return True
        return self.role in roles
