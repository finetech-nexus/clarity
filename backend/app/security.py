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


KNOWN_ROLES = ("admin", "checker", "investigator", "analyst", "pending")


def parse_roles(value: str | None) -> list[str]:
    found: list[str] = []
    for item in str(value or "").split(","):
        role = item.strip()
        if role in KNOWN_ROLES and role not in found:
            found.append(role)
    if any(role != "pending" for role in found):
        found = [role for role in found if role != "pending"]
    return found or ["pending"]


def serialize_roles(roles: list[str]) -> str:
    unknown = [role for role in roles if role not in KNOWN_ROLES]
    if unknown:
        raise ValueError(unknown[0])
    chosen = [role for role in KNOWN_ROLES if role in roles and role != "pending"]
    if not chosen and "pending" in roles:
        chosen = ["pending"]
    return ",".join(chosen or ["pending"])


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    surface: str
    user_id: str | None
    email: str | None
    role: str
    credential_id: str | None = None

    def allows(self, *roles: str) -> bool:
        held = set(parse_roles(self.role))
        if "admin" in held:
            return True
        return bool(held.intersection(roles))
