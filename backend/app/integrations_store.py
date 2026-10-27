"""Integration secrets saved from the screen override empty environment variables."""

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import set_runtime_overlay
from app.models import ProviderConnection

CATALOG = {
    "opensanctions": {
        "purpose": "sanctions",
        "name": "OpenSanctions",
        "hint": "Le criblage interroge OpenSanctions dès qu'une clé est acceptée. Les correspondances restent des candidats.",
        "fields": [{"key": "api_key", "label": "Clé API", "secret": True}],
    },
    "veriff": {
        "purpose": "identity",
        "name": "Veriff",
        "hint": "Une session d'identité n'est ouverte que lorsque la clé et le secret sont enregistrés.",
        "fields": [
            {"key": "api_key", "label": "Clé API", "secret": True},
            {"key": "shared_secret", "label": "Secret partagé", "secret": True},
            {"key": "base_url", "label": "URL de base", "secret": False},
        ],
    },
}

_OVERLAY = {
    "opensanctions": {"api_key": "opensanctions_api_key"},
    "veriff": {"api_key": "veriff_api_key", "shared_secret": "veriff_shared_secret", "base_url": "veriff_base_url"},
}

_DEFAULT = {"sanctions": "opensanctions", "identity": "veriff"}


def provider_catalog() -> list[dict]:
    return [{"id": key, **value} for key, value in CATALOG.items()]


def provider_of(row: ProviderConnection) -> str:
    chosen = str((row.detail or {}).get("provider") or "")
    if chosen in CATALOG and CATALOG[chosen]["purpose"] == row.kind:
        return chosen
    return _DEFAULT.get(row.kind, "")


def load_overlay(db: Session) -> None:
    rows = db.scalars(select(ProviderConnection).where(ProviderConnection.kind.in_(tuple(_DEFAULT)))).all()
    values = {}
    for row in rows:
        detail = row.detail or {}
        for source, target in _OVERLAY.get(provider_of(row), {}).items():
            if detail.get(source):
                values[target] = str(detail[source])
    set_runtime_overlay(values)


def public_integration(row: ProviderConnection) -> dict:
    detail = row.detail or {}
    provider = provider_of(row)
    secret = bool(detail.get("api_key") or detail.get("shared_secret"))
    return {
        "id": row.id,
        "kind": row.kind,
        "provider": provider,
        "name": CATALOG.get(provider, {}).get("name") or row.name,
        "mode": row.mode,
        "configured": bool(row.configured or secret),
        "connection_status": detail.get("connection_status") or "",
        "connection_message": detail.get("connection_message") or "",
        "has_secret": secret,
        "base_url": detail.get("base_url") or "",
    }


def save_integration(db: Session, row: ProviderConnection, body: dict) -> ProviderConnection:
    previous = provider_of(row)
    provider = str(body.get("provider") or previous)
    spec = CATALOG.get(provider)
    if spec is None or spec["purpose"] != row.kind:
        from app.services import clarity_error

        raise clarity_error(422, "unknown_provider", "Ce fournisseur n'est pas disponible pour cet usage.")
    detail = dict(row.detail or {})
    detail["provider"] = provider
    allowed = {field["key"] for field in spec["fields"]}
    if provider != previous:
        for key in ("api_key", "shared_secret", "base_url", "model"):
            if key not in allowed:
                detail.pop(key, None)
        detail.pop("connection_status", None)
        detail.pop("connection_message", None)
    for key in allowed:
        if key in body and body[key] not in (None, ""):
            detail[key] = str(body[key])
    row.detail = detail
    row.name = spec["name"]
    row.configured = bool(detail.get("api_key") or detail.get("shared_secret"))
    row.mode = "configured" if row.configured else "not_configured"
    load_overlay(db)
    return row


def probe_integration(row: ProviderConnection) -> tuple[bool, str]:
    provider = provider_of(row)
    detail = row.detail or {}
    if provider == "opensanctions":
        key = str(detail.get("api_key") or "")
        if not key:
            return False, "Aucune clé OpenSanctions."
        try:
            response = httpx.get(
                "https://api.opensanctions.org/search/default",
                params={"q": "test", "limit": 1},
                headers={"Authorization": f"ApiKey {key}"},
                timeout=8,
            )
        except httpx.HTTPError:
            return False, "OpenSanctions ne répond pas."
        if response.status_code == 200:
            return True, "OpenSanctions répond. Les correspondances restent des candidats."
        if response.status_code in {401, 403}:
            return False, "La clé OpenSanctions est refusée."
        return False, "OpenSanctions n'a pas accepté la clé."
    if provider == "veriff":
        if detail.get("api_key") and detail.get("shared_secret"):
            return True, "Clés Veriff enregistrées. L'essai ne crée pas de session."
        return False, "La clé et le secret Veriff sont requis."
    return False, "Ce fournisseur ne se vérifie pas encore."


def check_integration(db: Session, row: ProviderConnection) -> ProviderConnection:
    ok, message = probe_integration(row)
    detail = dict(row.detail or {})
    detail["connection_status"] = "connected" if ok else "rejected"
    detail["connection_message"] = message
    row.detail = detail
    row.mode = "connected" if ok else "rejected"
    return row
