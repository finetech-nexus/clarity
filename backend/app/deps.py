import time
from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.security import Principal, decode_jwt, parse_roles
from app.services import ApiFailure, credential_for_token, user_by_email

_WINDOWS: dict[str, list[float]] = {}


def rate_limit(key: str, limit: int = 120) -> None:
    now = time.time()
    bucket = _WINDOWS.setdefault(key, [])
    bucket[:] = [item for item in bucket if now - item < 60]
    if len(bucket) >= limit:
        raise ApiFailure(429, {"code": "rate_limited", "messages": ["Too many requests"]})
    bucket.append(now)


def db_session(request: Request) -> Iterator[Session]:
    db = request.app.state.database.session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    return None


def marble_principal(request: Request, db: Session = Depends(db_session)) -> Principal:
    token = request.headers.get("x-api-key") or _bearer(request)
    if not token:
        raise ApiFailure(401, None)
    rate_limit(f"marble:{token[:12]}")
    credential = credential_for_token(db, token, "marble")
    if credential:
        return Principal(credential.tenant_id, "marble", None, None, "service", credential.id)
    settings = request.app.state.settings
    try:
        payload = decode_jwt(settings.jwt_secret, token)
    except Exception as exc:
        raise ApiFailure(401, None) from exc
    user = user_by_email(db, payload.get("tenant_id", ""), payload.get("email", ""))
    if not user or not user.active:
        raise ApiFailure(401, None)
    return Principal(user.tenant_id, "marble", user.id, user.email, user.role)


def ballerine_principal(request: Request, db: Session = Depends(db_session)) -> Principal:
    token = _bearer(request)
    if not token:
        raise ApiFailure(401, {"statusCode": 401, "message": "Unauthorized", "error": "Unauthorized"})
    rate_limit(f"ballerine:{token[:12]}")
    credential = credential_for_token(db, token, "ballerine")
    if credential:
        return Principal(credential.tenant_id, "ballerine", None, None, "service", credential.id)
    settings = request.app.state.settings
    try:
        payload = decode_jwt(settings.jwt_secret, token)
    except Exception as exc:
        raise ApiFailure(401, {"statusCode": 401, "message": "Unauthorized", "error": "Unauthorized"}) from exc
    user = user_by_email(db, payload.get("tenant_id", ""), payload.get("email", ""))
    if not user or not user.active:
        raise ApiFailure(401, {"statusCode": 401, "message": "Unauthorized", "error": "Unauthorized"})
    return Principal(user.tenant_id, "ballerine", user.id, user.email, user.role)


def clarity_principal(request: Request, db: Session = Depends(db_session)) -> Principal:
    token = _bearer(request)
    if not token:
        raise ApiFailure(401, {"code": "unauthorized", "message": "Authentification requise."})
    rate_limit(f"clarity:{token[:12]}")
    settings = request.app.state.settings
    user = None
    try:
        payload = decode_jwt(settings.jwt_secret, token)
        user = user_by_email(db, payload["tenant_id"], payload["email"])
        if not user:
            raise ApiFailure(401, {"code": "unauthorized", "message": "Utilisateur introuvable."})
    except ApiFailure:
        raise
    except Exception:
        from app.oidc import claims_from_keycloak, enabled, provision_oidc_user

        if settings.resolved_auth_mode == "password" or not enabled(settings):
            raise ApiFailure(401, {"code": "unauthorized", "message": "Jeton invalide."})
        try:
            claims = claims_from_keycloak(settings, token)
        except Exception as exc:  # noqa: BLE001
            raise ApiFailure(401, {"code": "unauthorized", "message": "Jeton Keycloak invalide."}) from exc
        user = provision_oidc_user(db, claims)
    if parse_roles(user.role) == ["pending"] and not request.url.path.rstrip("/").endswith("/me"):
        raise ApiFailure(403, {"code": "role_pending", "message": "Un administrateur doit vous attribuer un rôle."})
    return Principal(user.tenant_id, "clarity", user.id, user.email, user.role)
