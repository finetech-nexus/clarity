"""Integration secrets saved from the screen override empty environment variables."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import set_runtime_overlay
from app.models import ProviderConnection

_FIELDS = {
    "sanctions": {"api_key": "opensanctions_api_key"},
    "identity": {"api_key": "veriff_api_key", "shared_secret": "veriff_shared_secret", "base_url": "veriff_base_url"},
    "language": {"api_key": "ai_api_key", "base_url": "ai_base_url", "model": "ai_model"},
}


def load_overlay(db: Session) -> None:
    rows = db.scalars(select(ProviderConnection).where(ProviderConnection.kind.in_(tuple(_FIELDS)))).all()
    values = {}
    for row in rows:
        detail = row.detail or {}
        for source, target in _FIELDS[row.kind].items():
            if detail.get(source):
                values[target] = str(detail[source])
    set_runtime_overlay(values)


def public_integration(row: ProviderConnection) -> dict:
    detail = row.detail or {}
    secret = bool(detail.get("api_key") or detail.get("shared_secret"))
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "mode": row.mode,
        "configured": bool(row.configured or secret),
        "production": False,
        "has_secret": secret,
        "base_url": detail.get("base_url") or "",
        "model": detail.get("model") or "",
    }


def save_integration(db: Session, row: ProviderConnection, body: dict) -> ProviderConnection:
    detail = dict(row.detail or {})
    for key in ("api_key", "shared_secret", "base_url", "model"):
        if key in body and body[key] not in (None, ""):
            detail[key] = str(body[key])
    row.detail = detail
    row.configured = bool(detail.get("api_key") or detail.get("shared_secret") or detail.get("base_url"))
    row.mode = "configured" if row.configured else row.mode
    load_overlay(db)
    return row
