"""External providers. A successful call is the only result marked production=True."""

import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx

_DATASET_CACHE: dict | None = None

PUBLIC_DATASETS = [
    {"name": "default", "title": "OpenSanctions default", "type": "collection"},
    {"name": "sanctions", "title": "Sanctions", "type": "collection"},
    {"name": "peps", "title": "Politically exposed persons", "type": "collection"},
    {"name": "debarment", "title": "Debarment", "type": "collection"},
    {"name": "crime", "title": "Crime", "type": "collection"},
]


def _settings():
    from app.config import get_settings

    return get_settings()


def opensanctions_configured() -> bool:
    return bool(_settings().opensanctions_api_key)


def veriff_configured() -> bool:
    settings = _settings()
    return bool(settings.veriff_api_key and settings.veriff_shared_secret)


def ai_configured() -> bool:
    return bool(_settings().ai_api_key)


def match_opensanctions(name: str, schema: str, birth_date: str | None, country: str | None) -> list[dict]:
    settings = _settings()
    if not settings.opensanctions_api_key or not name:
        return []
    properties: dict = {"name": [name]}
    if birth_date:
        properties["birthDate"] = [birth_date]
    if country:
        properties["country"] = [country]
    try:
        response = httpx.post(
            "https://api.opensanctions.org/match/default",
            headers={"Authorization": f"ApiKey {settings.opensanctions_api_key}"},
            json={"queries": {"q": {"schema": schema or "Person", "properties": properties}}},
            timeout=8,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    results = ((body.get("responses") or {}).get("q") or {}).get("results") or []
    rows = []
    for item in results:
        score = int(round(float(item.get("score") or 0) * 100))
        rows.append(
            {
                "entity_id": str(item.get("id") or ""),
                "score": score,
                "explanation": [f"OpenSanctions score {score} on {item.get('caption') or name}. Candidate only."],
                "production": True,
                "payload": {
                    "id": item.get("id"),
                    "caption": item.get("caption") or name,
                    "schema": item.get("schema") or schema,
                    "datasets": item.get("datasets") or [],
                    "score": item.get("score"),
                    "topics": (item.get("properties") or {}).get("topics") or item.get("topics") or [],
                    "properties": item.get("properties") or {},
                    "synthetic": False,
                    "provider": "opensanctions",
                    "notes": ["Candidate match returned by OpenSanctions. Not a confirmed hit."],
                },
            }
        )
    return rows


def dataset_catalog() -> dict:
    global _DATASET_CACHE
    if _DATASET_CACHE is not None:
        return _DATASET_CACHE
    live = False
    datasets = list(PUBLIC_DATASETS)
    try:
        response = httpx.get("https://data.opensanctions.org/datasets/latest/index.json", timeout=4)
        if response.status_code == 200:
            payload = response.json()
            fetched = payload.get("datasets") if isinstance(payload, dict) else None
            if isinstance(fetched, list) and fetched:
                datasets = [
                    {"name": item.get("name"), "title": item.get("title"), "type": item.get("type")}
                    for item in fetched
                    if isinstance(item, dict) and item.get("name")
                ][:80]
                live = True
    except (httpx.HTTPError, ValueError):
        live = False
    _DATASET_CACHE = {"datasets": datasets, "live": live, "provider": "opensanctions"}
    return _DATASET_CACHE


def create_veriff_session(first_name: str, last_name: str, vendor_data: str) -> dict:
    settings = _settings()
    if not veriff_configured():
        raise RuntimeError("veriff is not configured")
    payload = {"verification": {"person": {"firstName": first_name, "lastName": last_name}, "vendorData": vendor_data}}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(settings.veriff_shared_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    response = httpx.post(
        settings.veriff_base_url.rstrip("/") + "/v1/sessions",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-AUTH-CLIENT": settings.veriff_api_key,
            "X-HMAC-SIGNATURE": signature,
        },
        timeout=12,
    )
    response.raise_for_status()
    body = response.json()
    verification = body.get("verification") or {}
    return {"checkId": verification.get("id"), "sessionId": verification.get("sessionToken") or verification.get("id"), "url": verification.get("url"), "provider": "veriff", "production": True}


def complete_ai(instructions: str, facts: str) -> str | None:
    settings = _settings()
    if not settings.ai_api_key:
        return None
    try:
        response = httpx.post(
            settings.ai_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.ai_api_key}"},
            json={
                "model": settings.ai_model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": facts},
                ],
            },
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        return body["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
