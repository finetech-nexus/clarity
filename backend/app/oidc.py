"""OpenID Connect authorization-code login with PKCE."""

import base64
import hashlib
import secrets
import ssl
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.ids import TENANT_ID
from app.models import Tenant, User
from app.security import encode_jwt, hash_password, parse_roles, serialize_roles
from app.services import clarity_error

PENDING_ROLE = "pending"
ASSIGNABLE_ROLES = {"pending", "analyst", "checker", "investigator", "admin"}


def enabled(settings: Settings) -> bool:
    return bool(settings.oidc_issuer.strip() and settings.oidc_client_id.strip())


def redirect_uri(settings: Settings) -> str:
    configured = settings.oidc_redirect_uri.strip()
    if configured:
        return configured
    return f"{settings.public_app_url.rstrip('/')}/api/clarity/v1/auth/oidc/callback"


def _jwks_client(uri: str) -> PyJWKClient:
    # The sandbox Keycloak certificate is private to the cluster.
    return PyJWKClient(uri, ssl_context=ssl._create_unverified_context())


def discover(issuer: str) -> dict:
    response = httpx.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=8, verify=False)
    if response.status_code >= 400:
        raise clarity_error(502, "oidc_unavailable", "Le fournisseur OpenID ne répond pas.")
    document = response.json()
    if not document.get("authorization_endpoint") or not document.get("token_endpoint") or not document.get("jwks_uri"):
        raise clarity_error(502, "oidc_unavailable", "La découverte OpenID est incomplète.")
    return document


def authorization_redirect(settings: Settings) -> str:
    document = discover(settings.oidc_issuer)
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(18)
    state = encode_jwt(settings.jwt_secret, {"kind": "oidc", "verifier": verifier, "nonce": nonce}, 10)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).rstrip(b"=").decode("ascii")
    query = urlencode(
        {
            "client_id": settings.oidc_client_id,
            "response_type": "code",
            "scope": "openid email profile",
            "redirect_uri": redirect_uri(settings),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{document['authorization_endpoint']}?{query}"


def identity_from_callback(settings: Settings, code: str, state: str) -> dict:
    try:
        pending = jwt.decode(state, settings.jwt_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError as exc:
        raise clarity_error(401, "oidc_rejected", "La session OpenID a expiré.") from exc
    if pending.get("kind") != "oidc" or not pending.get("verifier") or not pending.get("nonce"):
        raise clarity_error(401, "oidc_rejected", "La session OpenID est invalide.")
    document = discover(settings.oidc_issuer)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(settings),
        "client_id": settings.oidc_client_id,
        "code_verifier": pending["verifier"],
    }
    if settings.oidc_client_secret.strip():
        form["client_secret"] = settings.oidc_client_secret
    response = httpx.post(document["token_endpoint"], data=form, timeout=8, verify=False)
    if response.status_code >= 400:
        raise clarity_error(401, "oidc_rejected", "Le fournisseur OpenID a refusé le code.")
    id_token = str(response.json().get("id_token") or "")
    if not id_token:
        raise clarity_error(401, "oidc_rejected", "Le fournisseur OpenID n'a pas renvoyé d'identité.")
    try:
        signing_key = _jwks_client(document["jwks_uri"]).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.oidc_client_id,
            issuer=document.get("issuer") or settings.oidc_issuer.rstrip("/"),
        )
    except jwt.InvalidTokenError as exc:
        raise clarity_error(401, "oidc_rejected", "L'identité OpenID n'est pas valide.") from exc
    if claims.get("nonce") != pending["nonce"]:
        raise clarity_error(401, "oidc_rejected", "L'identité OpenID n'est pas valide.")
    return claims


def claims_from_keycloak(settings: Settings, token: str) -> dict:
    """Validate an access token issued by Keycloak for the same client as the other applications."""
    issuer = settings.oidc_issuer.strip().rstrip("/")
    if not issuer:
        raise clarity_error(401, "unauthorized", "Keycloak n'est pas configuré.")
    try:
        header = jwt.get_unverified_header(token)
        algorithm = str(header.get("alg") or "RS256")
        if algorithm not in {"RS256", "PS256", "ES256"}:
            raise jwt.InvalidTokenError(algorithm)
        signing_key = _jwks_client(f"{issuer}/protocol/openid-connect/certs").get_signing_key_from_jwt(token)
        claims = jwt.decode(token, signing_key.key, algorithms=[algorithm], issuer=issuer, options={"verify_aud": False})
    except jwt.InvalidTokenError as exc:
        raise clarity_error(401, "unauthorized", "Jeton Keycloak invalide.") from exc
    client = settings.oidc_client_id.strip()
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    authorized = str(claims.get("azp") or "")
    if client and authorized not in {"", client} and client not in audiences:
        raise clarity_error(401, "unauthorized", "Jeton Keycloak invalide.")
    if not claims.get("email") and not claims.get("preferred_username"):
        info = httpx.get(
            f"{issuer}/protocol/openid-connect/userinfo",
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
            verify=False,
        )
        if info.status_code < 400 and isinstance(info.json(), dict):
            claims = {**claims, **info.json()}
    return claims


def provision_oidc_user(db: Session, claims: dict) -> User:
    from app.config import current_settings

    if db.get(Tenant, TENANT_ID) is None:
        raise clarity_error(503, "not_ready", "Le tenant n'est pas prêt.")
    email = str(claims.get("email") or "").strip().lower()
    username = str(claims.get("preferred_username") or "").strip().lower()
    keys = [item for item in (email, username) if item]
    if not keys:
        raise clarity_error(400, "oidc_rejected", "Keycloak n'a pas fourni d'identifiant.")
    user = None
    for key in keys:
        user = db.scalar(select(User).where(User.tenant_id == TENANT_ID, User.email == key))
        if user:
            break
    given = str(claims.get("given_name") or "").strip()
    family = str(claims.get("family_name") or "").strip()
    name = str(claims.get("name") or f"{given} {family}".strip() or keys[0])
    bootstrap = bool(set(keys) & current_settings().oidc_admin_names)
    if user is None:
        user = User(
            tenant_id=TENANT_ID,
            email=email or username,
            full_name=name[:200],
            password_hash=hash_password(secrets.token_urlsafe(24)),
            role="admin" if bootstrap else PENDING_ROLE,
            active=True,
            mfa_enabled=False,
        )
        db.add(user)
        db.flush()
        return user
    if name:
        user.full_name = name[:200]
    user.active = True
    if bootstrap and "admin" not in parse_roles(user.role):
        user.role = serialize_roles([*parse_roles(user.role), "admin"])
    return user
