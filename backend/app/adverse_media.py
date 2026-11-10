"""Adverse media engine.

Independent from OpenSanctions. Customers and parties stay the KYC referential.
The lexicon engine classifies allegations. It never concludes that a person is guilty.
Articles are stored only when a connector returns them. Nothing is invented.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AdverseMediaAlert,
    AdverseMediaArticle,
    AdverseMediaArticleVersion,
    AdverseMediaAuditLog,
    AdverseMediaCollectionLog,
    AdverseMediaDecision,
    AdverseMediaEntity,
    AdverseMediaEntityMention,
    AdverseMediaEvent,
    AdverseMediaInvestigation,
    AdverseMediaMatch,
    AdverseMediaProcessingLog,
    AdverseMediaSource,
    BusinessProfile,
    ConfigDocument,
    Customer,
    IndividualProfile,
    Party,
    Tenant,
)
from app.timeutil import utcnow

NLP_ENGINE = "lexicon-1.0"
ALERT_STATUSES = (
    "new",
    "in_review",
    "waiting_info",
    "escalated",
    "false_positive",
    "confirmed_relevant",
    "closed",
)
DECISION_OUTCOMES = ("false_positive", "confirmed_relevant", "closed")
ALERTING_STANCES = ("allegation", "investigation", "prosecution", "conviction")
STANCE_RANK = {
    "denial": 0,
    "acquittal": 0,
    "mention": 1,
    "allegation": 2,
    "investigation": 3,
    "prosecution": 4,
    "conviction": 5,
}
STANCE_MEDIA = {
    "mention": 15,
    "allegation": 45,
    "investigation": 60,
    "prosecution": 75,
    "conviction": 90,
    "acquittal": 10,
    "denial": 10,
}
CATEGORIES = (
    "corruption",
    "fraud",
    "money_laundering",
    "terrorist_financing",
    "organized_crime",
    "drug_trafficking",
    "tax_evasion",
    "embezzlement",
    "scam",
    "cybercrime",
    "smuggling",
    "regulatory",
    "reputation",
)
_CATEGORY_TERMS = {
    "corruption": ("corruption", "corrup", "pot-de-vin", "رشوة"),
    "fraud": ("fraude", "fraud", "احتيال"),
    "money_laundering": ("blanchiment", "money laundering", "غسيل الأموال"),
    "terrorist_financing": ("financement du terrorisme", "terrorist financing", "تمويل الإرهاب"),
    "organized_crime": ("criminalité organisée", "organized crime", "جريمة منظمة"),
    "drug_trafficking": ("stupéfiant", "trafic de drogue", "drug trafficking", "مخدرات"),
    "tax_evasion": ("évasion fiscale", "evasion fiscale", "tax evasion", "تهرب ضريبي"),
    "embezzlement": ("détournement", "detournement", "embezzlement", "اختلاس"),
    "scam": ("escroquerie", "arnaque", "scam", "نصب"),
    "cybercrime": ("cybercriminalité", "cybercriminalite", "cybercrime", "جرائم معلوماتية"),
    "smuggling": ("contrebande", "smuggling", "تهريب"),
    "regulatory": ("violation réglementaire", "violation reglementaire", "regulatory breach", "مخالفة تنظيمية"),
    "reputation": ("atteinte à la réputation", "atteinte a la reputation", "reputational", "تشويه السمعة"),
}
_STANCE_TERMS = (
    ("acquittal", ("acquitté", "acquitte", "acquitted", "relaxé", "relaxe", "براءة")),
    ("denial", ("dément", "dement", "dénégation", "denies", "denied", "denial", "ينفي", "نفي")),
    ("conviction", ("condamné", "condamne", "convicted", "sentenced", "أدين", "إدانة")),
    ("prosecution", ("poursuite", "inculpé", "inculpe", "charged", "prosecuted", "ملاحقة", "متابع")),
    ("investigation", ("enquête", "enquete", "investigation", "تحقيق")),
    ("allegation", ("allégation", "allegation", "allégué", "allegue", "alleged", "soupçonné", "soupconne", "accused", "يزعم", "زعم")),
)
_ARABIC = {
    "ا": "a", "أ": "a", "إ": "i", "آ": "a", "ب": "b", "ت": "t", "ث": "th", "ج": "j", "ح": "h",
    "خ": "kh", "د": "d", "ذ": "dh", "ر": "r", "ز": "z", "س": "s", "ش": "sh", "ص": "s", "ض": "d",
    "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f", "ق": "q", "ك": "k", "ل": "l", "م": "m",
    "ن": "n", "ه": "h", "ة": "a", "و": "ou", "ؤ": "ou", "ي": "i", "ى": "a", "ئ": "i", "ء": "",
}
_PARTICLES = ("ben", "bin", "ibn", "ould", "ouled", "el", "al", "bn")
_STOP = {"le", "la", "les", "de", "des", "du", "et", "the", "and", "of", "pour", "dans", "une", "un"}
_SOURCE_CATALOG = (
    {"name": "GDELT", "kind": "gdelt", "url": "https://api.gdeltproject.org/api/v2/doc/doc", "language": "multi", "config": {"query": "Tunisia OR Tunisie", "timeout_seconds": 12, "quota_per_hour": 20, "max_records": 25}},
    {"name": "La Presse de Tunisie", "kind": "tunisian", "url": "https://lapresse.tn/feed/", "language": "fr", "config": {"timeout_seconds": 12, "quota_per_hour": 30, "format": "rss"}},
    {"name": "Business News Tunisie", "kind": "tunisian", "url": "https://businessnews.com.tn/feed/", "language": "fr", "config": {"timeout_seconds": 12, "quota_per_hour": 30, "format": "rss"}},
    {"name": "BBC World", "kind": "international", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "language": "en", "config": {"timeout_seconds": 12, "quota_per_hour": 30, "format": "rss"}},
    {"name": "France 24", "kind": "international", "url": "https://www.france24.com/fr/rss", "language": "fr", "config": {"timeout_seconds": 12, "quota_per_hour": 30, "format": "rss"}},
    {"name": "Jeune Afrique", "kind": "international", "url": "https://www.jeuneafrique.com/feed/", "language": "fr", "config": {"timeout_seconds": 12, "quota_per_hour": 30, "format": "rss"}},
    {"name": "Presse tunisienne", "kind": "tunisian", "url": "", "language": "fr", "config": {"timeout_seconds": 12, "quota_per_hour": 30, "format": "rss"}},
    {"name": "Presse internationale", "kind": "international", "url": "", "language": "en", "config": {"timeout_seconds": 12, "quota_per_hour": 30, "format": "rss"}},
    {"name": "Flux RSS", "kind": "rss", "url": "", "language": "multi", "config": {"timeout_seconds": 12, "quota_per_hour": 60, "format": "rss"}},
    {"name": "Publications officielles", "kind": "official", "url": "", "language": "fr", "config": {"timeout_seconds": 12, "quota_per_hour": 20, "format": "rss"}},
    {"name": "Sources institutionnelles", "kind": "institutional", "url": "", "language": "fr", "config": {"timeout_seconds": 12, "quota_per_hour": 20, "format": "rss"}},
    {"name": "API publique", "kind": "public_api", "url": "", "language": "multi", "config": {"timeout_seconds": 12, "quota_per_hour": 20, "format": "json"}},
)
_DEFAULT_POLICY = {
    "identity_threshold": 82,
    "media_threshold": 40,
    "frequencies_hours": {"high": 24, "medium": 168, "low": 720},
    "article_window_days": 90,
}


class AdverseMediaError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    converted = []
    for char in text:
        converted.append(_ARABIC.get(char, char))
    text = "".join(converted).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def skeleton(value: str | None) -> str:
    text = fold(value)
    for particle in _PARTICLES:
        text = re.sub(rf"\b{particle}\b", " ", text)
    return re.sub(r"[aeiouy\s]", "", text)


def content_hash(title: str, body: str) -> str:
    material = fold(f"{title}\n{body}")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def plain_text(value: str | None, limit: int = 20000) -> str:
    text = str(value or "")
    text = re.sub(r"(?is)<script\b.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style\b.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text).replace("\x00", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _policy_row(db: Session, tenant_id: str) -> ConfigDocument | None:
    return db.scalar(
        select(ConfigDocument).where(
            ConfigDocument.tenant_id == tenant_id,
            ConfigDocument.kind == "adverse_media_policy",
            ConfigDocument.status == "active",
        )
    )


def get_policy(db: Session, tenant_id: str) -> dict:
    row = _policy_row(db, tenant_id)
    payload = dict(_DEFAULT_POLICY)
    if row and isinstance(row.payload, dict):
        payload.update(row.payload)
    hours = dict(_DEFAULT_POLICY["frequencies_hours"])
    hours.update(payload.get("frequencies_hours") or {})
    payload["frequencies_hours"] = hours
    payload["identity_threshold"] = int(payload.get("identity_threshold") or 82)
    payload["media_threshold"] = int(payload.get("media_threshold") or 40)
    payload["article_window_days"] = int(payload.get("article_window_days") or 90)
    return payload


def save_policy(db: Session, tenant_id: str, payload: dict, actor: str) -> dict:
    current = get_policy(db, tenant_id)
    hours = dict(current["frequencies_hours"])
    incoming = payload.get("frequencies_hours") or {}
    for key in ("high", "medium", "low"):
        if key in incoming:
            hours[key] = max(1, min(int(incoming[key]), 24 * 366))
    current["frequencies_hours"] = hours
    if "identity_threshold" in payload:
        current["identity_threshold"] = max(50, min(int(payload["identity_threshold"]), 100))
    if "media_threshold" in payload:
        current["media_threshold"] = max(1, min(int(payload["media_threshold"]), 100))
    if "article_window_days" in payload:
        current["article_window_days"] = max(1, min(int(payload["article_window_days"]), 3650))
    row = _policy_row(db, tenant_id)
    if row is None:
        row = ConfigDocument(tenant_id=tenant_id, kind="adverse_media_policy", name="Adverse media", status="active", payload=current)
        db.add(row)
    else:
        row.payload = current
        row.updated_at = utcnow()
    db.flush()
    _audit(db, tenant_id, actor, "policy.update", "policy", row.id, current)
    db.flush()
    return current


def _source_ready(kind: str, url: str, config: dict) -> bool:
    if not url:
        return False
    if kind == "gdelt" and not str(config.get("query") or "").strip():
        return False
    return True


def ensure_sources(db: Session, tenant_id: str) -> list[AdverseMediaSource]:
    existing = {row.name: row for row in db.scalars(select(AdverseMediaSource).where(AdverseMediaSource.tenant_id == tenant_id)).all()}
    for spec in _SOURCE_CATALOG:
        config = dict(spec["config"])
        config["seeded"] = True
        ready = _source_ready(spec["kind"], spec["url"], config)
        row = existing.get(spec["name"])
        if row is None:
            db.add(
                AdverseMediaSource(
                    tenant_id=tenant_id,
                    name=spec["name"],
                    kind=spec["kind"],
                    url=spec["url"],
                    language=spec["language"],
                    frequency_minutes=60,
                    active=ready,
                    availability="unknown",
                    config=config,
                )
            )
            continue
        if (row.config or {}).get("seeded"):
            continue
        current = dict(row.config or {})
        if not row.url and spec["url"]:
            row.url = spec["url"]
        if spec["kind"] == "gdelt" and not str(current.get("query") or "").strip() and config.get("query"):
            current["query"] = config["query"]
        for key, value in config.items():
            current.setdefault(key, value)
        current["seeded"] = True
        row.config = current
        if _source_ready(row.kind, row.url, current):
            row.active = True
        row.updated_at = utcnow()
    db.flush()
    return list(db.scalars(select(AdverseMediaSource).where(AdverseMediaSource.tenant_id == tenant_id).order_by(AdverseMediaSource.name)).all())


def update_source(db: Session, source: AdverseMediaSource, payload: dict, actor: str) -> AdverseMediaSource:
    if "url" in payload:
        url = str(payload.get("url") or "").strip()
        if url and not url.startswith(("https://", "http://")):
            raise AdverseMediaError("L'URL de la source doit commencer par http:// ou https://.")
        source.url = url[:500]
    if "language" in payload:
        source.language = str(payload.get("language") or "multi")[:16]
    if "frequency_minutes" in payload:
        source.frequency_minutes = max(15, min(int(payload["frequency_minutes"]), 24 * 60 * 31))
    if "config" in payload and isinstance(payload["config"], dict):
        merged = dict(source.config or {})
        for key in ("query", "timeout_seconds", "quota_per_hour", "max_records", "format"):
            if key in payload["config"]:
                merged[key] = payload["config"][key]
        if "timeout_seconds" in merged:
            merged["timeout_seconds"] = max(3, min(int(merged["timeout_seconds"]), 30))
        if "quota_per_hour" in merged:
            merged["quota_per_hour"] = max(1, min(int(merged["quota_per_hour"]), 500))
        if "max_records" in merged:
            merged["max_records"] = max(1, min(int(merged["max_records"]), 100))
        source.config = merged
    if "active" in payload:
        active = bool(payload["active"])
        if active and not source.url:
            raise AdverseMediaError("Une source sans URL ne peut pas être activée.")
        if active and source.kind == "gdelt" and not str((source.config or {}).get("query") or "").strip():
            raise AdverseMediaError("GDELT exige une requête avant activation.")
        source.active = active
    source.updated_at = utcnow()
    _audit(db, source.tenant_id, actor, "source.update", "source", source.id, {"active": source.active, "frequency_minutes": source.frequency_minutes})
    db.flush()
    return source


def _audit(db: Session, tenant_id: str, actor: str, action: str, entity_type: str, entity_id: str, detail: dict | None = None) -> None:
    from app.services import audit

    db.add(
        AdverseMediaAuditLog(
            tenant_id=tenant_id,
            actor=actor or "system",
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail or {},
        )
    )
    audit(db, tenant_id, actor or "system", f"adverse_media.{action}", entity_type, entity_id, detail)


def _timeout(source: AdverseMediaSource) -> int:
    return max(3, min(int((source.config or {}).get("timeout_seconds") or 12), 30))


def _quota(source: AdverseMediaSource) -> int:
    return max(1, min(int((source.config or {}).get("quota_per_hour") or 30), 500))


def http_get(url: str, timeout: int) -> bytes:
    if not url.startswith(("https://", "http://")):
        raise AdverseMediaError("URL refusée.")
    request = urllib.request.Request(url, headers={"User-Agent": "ClarityAdverseMedia/1.0", "Accept": "application/json, application/rss+xml, application/xml, text/xml, */*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(750_001)[:750_000]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdverseMediaError(f"Source indisponible : {exc.__class__.__name__}") from exc


def _canonical_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.startswith(("https://", "http://")):
        raise AdverseMediaError("Article sans URL http(s).")
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme == "javascript":
        raise AdverseMediaError("URL refusée.")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))[:500]


def _published(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=utcnow().tzinfo)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=utcnow().tzinfo)
    except (TypeError, ValueError, IndexError):
        return None


def parse_rss(payload: bytes) -> list[dict]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise AdverseMediaError("Flux RSS illisible.") from exc
    items = []
    for node in root.iter():
        if not str(node.tag).lower().endswith("item"):
            continue
        values = {}
        for child in list(node):
            key = str(child.tag).lower().rsplit("}", 1)[-1]
            values[key] = child.text or values.get(key) or ""
            if key in {"encoded", "content"} and child.text:
                values["body"] = child.text
        link = values.get("link") or values.get("guid") or ""
        title = values.get("title") or ""
        body = values.get("body") or values.get("description") or ""
        if not link and not title:
            continue
        items.append(
            {
                "url": link,
                "title": title,
                "body": body,
                "published": values.get("pubdate") or values.get("date") or "",
                "external_id": values.get("guid") or link,
                "language": values.get("language") or "",
            }
        )
    return items


def parse_gdelt(payload: bytes) -> list[dict]:
    try:
        data = json.loads(payload.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise AdverseMediaError("Réponse GDELT illisible.") from exc
    rows = data.get("articles") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise AdverseMediaError("Réponse GDELT sans liste d'articles.")
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "url": row.get("url") or "",
                "title": row.get("title") or "",
                "body": row.get("title") or "",
                "published": row.get("seendate") or "",
                "external_id": row.get("url") or "",
                "language": row.get("language") or "",
            }
        )
    return items


def parse_public_json(payload: bytes) -> list[dict]:
    try:
        data = json.loads(payload.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise AdverseMediaError("Réponse JSON illisible.") from exc
    rows = data.get("articles") if isinstance(data, dict) else data if isinstance(data, list) else None
    if not isinstance(rows, list):
        raise AdverseMediaError("La source ne fournit pas une liste d'articles.")
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "url": row.get("url") or "",
                "title": row.get("title") or "",
                "body": row.get("body") or row.get("description") or "",
                "published": row.get("published") or row.get("published_at") or "",
                "external_id": row.get("id") or row.get("url") or "",
                "language": row.get("language") or "",
            }
        )
    return items


def _connector_request(source: AdverseMediaSource) -> tuple[str, str]:
    kind = source.kind
    config = source.config or {}
    if kind == "gdelt":
        query = str(config.get("query") or "").strip()
        if not query:
            raise AdverseMediaError("Requête GDELT absente.")
        params = urllib.parse.urlencode(
            {
                "query": query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": max(1, min(int(config.get("max_records") or 25), 100)),
                "sort": "datedesc",
            }
        )
        base = source.url.split("?", 1)[0]
        return "gdelt", f"{base}?{params}"
    fmt = str(config.get("format") or ("json" if kind == "public_api" else "rss"))
    if not source.url:
        raise AdverseMediaError("URL de source absente.")
    return fmt, source.url


def enrich_gdelt_items(items: list[dict], fetch, timeout: int, limit: int = 8) -> list[dict]:
    """GDELT artlist returns titles. Read the article page when the site responds."""
    enriched = []
    for item in items[:limit]:
        row = dict(item)
        url = str(row.get("url") or "")
        title = str(row.get("title") or "")
        if url.startswith(("https://", "http://")):
            try:
                page = fetch(url, timeout)
                text = plain_text(page.decode("utf-8", errors="replace"))
                if len(text) > len(title) + 40:
                    row["body"] = text
            except AdverseMediaError:
                row["body"] = row.get("body") or title
        enriched.append(row)
    return enriched


def _parse_payload(fmt: str, payload: bytes) -> list[dict]:
    if fmt == "gdelt":
        return parse_gdelt(payload)
    if fmt == "json":
        return parse_public_json(payload)
    return parse_rss(payload)


def _within_quota(db: Session, source: AdverseMediaSource, now: datetime) -> bool:
    start = now - timedelta(hours=1)
    used = db.scalar(
        select(func.count()).select_from(AdverseMediaCollectionLog).where(
            AdverseMediaCollectionLog.source_id == source.id,
            AdverseMediaCollectionLog.status == "success",
            AdverseMediaCollectionLog.started_at >= start,
        )
    )
    return int(used or 0) < _quota(source)


def _log_collection(db: Session, source: AdverseMediaSource, status: str, fetched: int, stored: int, error: str | None, started: datetime) -> None:
    db.add(
        AdverseMediaCollectionLog(
            tenant_id=source.tenant_id,
            source_id=source.id,
            status=status,
            fetched_count=fetched,
            stored_count=stored,
            error=error,
            started_at=started,
            finished_at=utcnow(),
        )
    )
    source.last_collected_at = utcnow()
    source.availability = "up" if status == "success" else "down" if status == "error" else source.availability
    source.last_error = error
    source.updated_at = utcnow()


def collect_source(db: Session, source: AdverseMediaSource, fetch=http_get) -> dict:
    started = utcnow()
    if not source.active:
        _log_collection(db, source, "skipped", 0, 0, "Source inactive.", started)
        db.flush()
        return {"status": "skipped", "fetched": 0, "stored": 0}
    if not _within_quota(db, source, started):
        _log_collection(db, source, "quota", 0, 0, "Quota horaire atteint.", started)
        db.flush()
        return {"status": "quota", "fetched": 0, "stored": 0}
    try:
        fmt, url = _connector_request(source)
        payload = fetch(url, _timeout(source))
        items = _parse_payload(fmt, payload)
        if fmt == "gdelt":
            items = enrich_gdelt_items(items, fetch, _timeout(source))
    except AdverseMediaError as exc:
        _log_collection(db, source, "error", 0, 0, exc.message, started)
        db.flush()
        return {"status": "error", "fetched": 0, "stored": 0, "error": exc.message}
    stored = 0
    kept = 0
    for item in items:
        try:
            outcome = ingest_article(
                db,
                source,
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
                body=str(item.get("body") or ""),
                published=str(item.get("published") or ""),
                external_id=str(item.get("external_id") or ""),
                language=str(item.get("language") or source.language or ""),
            )
        except AdverseMediaError:
            continue
        kept += 1
        if outcome == "stored":
            stored += 1
    _log_collection(db, source, "success", kept, stored, None, started)
    db.flush()
    return {"status": "success", "fetched": kept, "stored": stored}


def ingest_article(
    db: Session,
    source: AdverseMediaSource,
    *,
    url: str,
    title: str,
    body: str,
    published: str = "",
    external_id: str = "",
    language: str = "",
) -> str:
    clean_title = plain_text(title, 500)
    clean_body = plain_text(body)
    if not clean_title and not clean_body:
        raise AdverseMediaError("Article vide.")
    canonical = _canonical_url(url)
    digest = content_hash(clean_title, clean_body)
    article = db.scalar(select(AdverseMediaArticle).where(AdverseMediaArticle.tenant_id == source.tenant_id, AdverseMediaArticle.url == canonical))
    if article and article.content_hash == digest:
        return "duplicate"
    published_at = _published(published)
    if article is None:
        article = AdverseMediaArticle(
            tenant_id=source.tenant_id,
            source_id=source.id,
            external_id=(external_id or canonical)[:200],
            url=canonical,
            title=clean_title,
            language=(language or source.language or "")[:16] or None,
            published_at=published_at,
            content_hash=digest,
            status="queued",
        )
        db.add(article)
        db.flush()
        version_number = 1
    else:
        version_number = int(
            db.scalar(select(func.max(AdverseMediaArticleVersion.version)).where(AdverseMediaArticleVersion.article_id == article.id))
            or 0
        ) + 1
        article.title = clean_title
        article.content_hash = digest
        article.status = "queued"
        article.updated_at = utcnow()
        if published_at:
            article.published_at = published_at
    existing_version = db.scalar(
        select(AdverseMediaArticleVersion).where(
            AdverseMediaArticleVersion.article_id == article.id,
            AdverseMediaArticleVersion.content_hash == digest,
        )
    )
    if existing_version and existing_version.nlp_status == "analyzed" and existing_version.nlp_engine_version == NLP_ENGINE:
        article.status = "analyzed"
        return "duplicate"
    if existing_version is None:
        db.add(
            AdverseMediaArticleVersion(
                article_id=article.id,
                version=version_number,
                content_hash=digest,
                title=clean_title,
                body=clean_body,
                nlp_status="pending",
            )
        )
    else:
        existing_version.nlp_status = "pending"
        existing_version.error = None
    db.flush()
    return "stored"


def _negated(text: str, term: str) -> bool:
    source = str(text or "")
    at = source.lower().find(str(term or "").lower())
    if at < 0:
        folded = fold(source)
        at = folded.find(fold(term))
        window = folded[max(0, at): at + 1] if at >= 0 else folded
        raw = source
    else:
        raw = source[max(0, at - 64) : at + 1]
        window = fold(raw)
    if any(marker in raw for marker in ("لم", "ليس")):
        return True
    return any(marker in window for marker in ("n est pas", "n a pas", "ne pas", "not ", "no evidence"))


def _stance(text: str) -> tuple[str, bool]:
    folded = fold(text)
    found: list[tuple[int, str, bool]] = []
    for stance, terms in _STANCE_TERMS:
        for term in terms:
            needle = fold(term) if re.search(r"[A-Za-zÀ-ÿ]", term) else term
            haystack = folded if needle.isascii() else text
            at = haystack.find(needle)
            if at >= 0:
                found.append((at, stance, _negated(text, term)))
    active = [item for item in found if not item[2]]
    if any(item[1] in {"acquittal", "denial"} for item in active):
        chosen = next(item for item in active if item[1] in {"acquittal", "denial"})
        return chosen[1], False
    if active:
        best = max(active, key=lambda item: STANCE_RANK[item[1]])
        return best[1], False
    if found:
        return found[0][1], True
    return "mention", False


def _categories(text: str) -> list[tuple[str, str, bool]]:
    folded = fold(text)
    found = []
    for category, terms in _CATEGORY_TERMS.items():
        for term in terms:
            needle = fold(term) if re.search(r"[A-Za-zÀ-ÿ]", term) else term
            haystack = folded if needle.isascii() else text
            at = haystack.find(needle)
            if at < 0:
                continue
            excerpt = plain_text(text[max(0, at - 80) : at + 160], 240)
            found.append((category, excerpt, _negated(text, term)))
            break
    return found


def _entities(text: str) -> list[dict]:
    people = []
    for match in re.finditer(r"\b([A-Z][\w'’-]{1,40}(?:\s+[A-Z][\w'’-]{1,40}){1,3})\b", text):
        name = match.group(1).strip()
        tokens = [token for token in name.split() if fold(token) not in _STOP]
        if len(tokens) < 2:
            continue
        people.append({"kind": "person", "name": name, "start": match.start()})
    for match in re.finditer(r"([\u0600-\u06FF]{2,}(?:\s+[\u0600-\u06FF]{2,}){1,3})", text):
        name = match.group(1).strip()
        if len(name.split()) >= 2:
            people.append({"kind": "person", "name": name, "start": match.start()})
    organizations = []
    for match in re.finditer(r"(?:société|societe|company|banque|شركة)\s+([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){0,3})", text, re.IGNORECASE):
        organizations.append({"kind": "organization", "name": match.group(1).strip(), "start": match.start()})
    unique = []
    seen = set()
    for row in people + organizations:
        key = (row["kind"], fold(row["name"]))
        if key in seen or not key[1]:
            continue
        seen.add(key)
        unique.append(row)
    relation = re.search(
        r"([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){1,3}).{0,40}?(dirigeant|gérant|gerant|actionnaire|bénéficiaire|beneficiaire|président|president).{0,20}?(?:de|of)\s+([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){0,3})",
        text,
    )
    if relation:
        person = fold(relation.group(1))
        for row in unique:
            if row["kind"] == "person" and fold(row["name"]) == person:
                row["role"] = relation.group(2).lower()
                row["organization"] = relation.group(3)
    return unique


def analyze_text(title: str, body: str) -> dict:
    text = plain_text(f"{title}. {body}")
    stance, stance_negated = _stance(text)
    categories = _categories(text)
    if not categories:
        stance = "mention"
    entities = _entities(text)
    events = []
    for category, excerpt, negated in categories:
        event_stance = "mention" if negated or stance_negated else stance
        events.append(
            {
                "category": category,
                "stance": event_stance,
                "negated": bool(negated or stance_negated),
                "confidence": 40 if negated else min(90, 50 + 10 * len(categories)),
                "excerpt": excerpt,
                "guilt_conclusion": False,
            }
        )
    return {
        "engine": NLP_ENGINE,
        "guilt_conclusion": False,
        "stance": stance if categories and not stance_negated else "mention",
        "negated": bool(stance_negated or any(item["negated"] for item in events)),
        "entities": [{"kind": item["kind"], "name": item["name"], "role": item.get("role"), "organization": item.get("organization")} for item in entities],
        "events": events,
    }


def _replace_analysis(db: Session, tenant_id: str, version: AdverseMediaArticleVersion, analysis: dict) -> None:
    entity_ids = list(
        db.scalars(select(AdverseMediaEntity.id).where(AdverseMediaEntity.article_version_id == version.id)).all()
    )
    if entity_ids:
        match_ids = list(db.scalars(select(AdverseMediaMatch.id).where(AdverseMediaMatch.entity_id.in_(entity_ids))).all())
        if match_ids:
            db.execute(update(AdverseMediaAlert).where(AdverseMediaAlert.match_id.in_(match_ids)).values(match_id=None))
            db.execute(delete(AdverseMediaMatch).where(AdverseMediaMatch.id.in_(match_ids)))
        db.execute(delete(AdverseMediaEntityMention).where(AdverseMediaEntityMention.entity_id.in_(entity_ids)))
    db.execute(update(AdverseMediaAlert).where(AdverseMediaAlert.event_id.in_(select(AdverseMediaEvent.id).where(AdverseMediaEvent.article_version_id == version.id))).values(event_id=None))
    db.execute(delete(AdverseMediaEvent).where(AdverseMediaEvent.article_version_id == version.id))
    db.execute(delete(AdverseMediaEntity).where(AdverseMediaEntity.article_version_id == version.id))
    db.flush()
    stance = analysis.get("stance") or "mention"
    negated = bool(analysis.get("negated"))
    entities = []
    for row in analysis.get("entities") or []:
        entity = AdverseMediaEntity(
            tenant_id=tenant_id,
            article_version_id=version.id,
            kind=row.get("kind") or "person",
            name=str(row.get("name") or "")[:240],
            normalized_name=fold(row.get("name"))[:240],
            attributes={"role": row.get("role"), "organization": row.get("organization"), "aliases": []},
        )
        db.add(entity)
        db.flush()
        db.add(
            AdverseMediaEntityMention(
                entity_id=entity.id,
                article_version_id=version.id,
                excerpt=plain_text(version.body, 240),
                stance=stance,
                negated=negated,
            )
        )
        entities.append(entity)
    for row in analysis.get("events") or []:
        db.add(
            AdverseMediaEvent(
                tenant_id=tenant_id,
                article_version_id=version.id,
                entity_id=entities[0].id if entities else None,
                category=row["category"],
                stance=row["stance"],
                negated=bool(row["negated"]),
                confidence=int(row["confidence"]),
                excerpt=row.get("excerpt") or "",
            )
        )
    version.analysis = analysis
    version.nlp_status = "analyzed"
    version.nlp_engine_version = NLP_ENGINE
    version.analyzed_at = utcnow()
    version.error = None
    db.flush()


def analyze_version(db: Session, version: AdverseMediaArticleVersion, *, force: bool = False) -> str:
    article = db.get(AdverseMediaArticle, version.article_id)
    if article is None:
        return "missing"
    if (
        not force
        and version.nlp_status == "analyzed"
        and version.nlp_engine_version == NLP_ENGINE
        and version.content_hash == content_hash(version.title, version.body)
    ):
        return "unchanged"
    attempts = int(
        db.scalar(
            select(func.count()).select_from(AdverseMediaProcessingLog).where(
                AdverseMediaProcessingLog.article_version_id == version.id,
                AdverseMediaProcessingLog.stage == "analyze",
                AdverseMediaProcessingLog.status == "error",
            )
        )
        or 0
    )
    try:
        analysis = analyze_text(version.title, version.body)
        _replace_analysis(db, article.tenant_id, version, analysis)
        article.status = "analyzed"
        article.updated_at = utcnow()
        db.add(
            AdverseMediaProcessingLog(
                tenant_id=article.tenant_id,
                article_version_id=version.id,
                stage="analyze",
                status="completed",
                attempt=attempts + 1,
                detail={"engine": NLP_ENGINE, "events": len(analysis.get("events") or []), "entities": len(analysis.get("entities") or [])},
            )
        )
        db.flush()
        return "analyzed"
    except Exception as exc:
        version.nlp_status = "failed"
        version.error = str(exc)[:500]
        article.status = "failed"
        db.add(
            AdverseMediaProcessingLog(
                tenant_id=article.tenant_id,
                article_version_id=version.id,
                stage="analyze",
                status="error",
                attempt=attempts + 1,
                error=version.error,
            )
        )
        db.flush()
        return "failed"


def process_queue(db: Session, tenant_id: str, limit: int = 20) -> dict:
    rows = db.scalars(
        select(AdverseMediaArticleVersion)
        .join(AdverseMediaArticle, AdverseMediaArticle.id == AdverseMediaArticleVersion.article_id)
        .where(AdverseMediaArticle.tenant_id == tenant_id, AdverseMediaArticleVersion.nlp_status.in_(("pending", "failed")))
        .order_by(AdverseMediaArticleVersion.created_at.asc())
        .limit(limit)
    ).all()
    analyzed = 0
    failed = 0
    for version in rows:
        if version.nlp_status == "failed":
            attempts = int(
                db.scalar(
                    select(func.count()).select_from(AdverseMediaProcessingLog).where(
                        AdverseMediaProcessingLog.article_version_id == version.id,
                        AdverseMediaProcessingLog.status == "error",
                    )
                )
                or 0
            )
            if attempts >= 3:
                continue
            last = db.scalar(
                select(AdverseMediaProcessingLog)
                .where(AdverseMediaProcessingLog.article_version_id == version.id, AdverseMediaProcessingLog.status == "error")
                .order_by(AdverseMediaProcessingLog.created_at.desc())
            )
            if last and last.created_at and utcnow() < last.created_at + timedelta(minutes=2**attempts):
                continue
        outcome = analyze_version(db, version)
        if outcome == "analyzed":
            match_version(db, tenant_id, version)
            analyzed += 1
        elif outcome == "failed":
            failed += 1
    return {"analyzed": analyzed, "failed": failed}


@dataclass
class Subject:
    customer_id: str | None
    party_id: str | None
    kind: str
    names: list[str] = field(default_factory=list)
    dob: str | None = None
    nationality: str | None = None
    country: str | None = None
    address: str | None = None
    role: str | None = None
    organization: str | None = None

    @property
    def key(self) -> str:
        if self.party_id:
            return f"party:{self.party_id}"
        return f"customer:{self.customer_id}"


def _codes(value: str | None) -> str:
    return fold(value)[:8]


def name_similarity(left: str, right: str) -> float:
    a = fold(left)
    b = fold(right)
    if len(a) < 4 or len(b) < 4:
        return 0.0
    full = SequenceMatcher(None, a, b).ratio()
    skel_a = skeleton(a)
    skel_b = skeleton(b)
    skel = 1.0 if skel_a and skel_a == skel_b and len(skel_a) >= 5 else SequenceMatcher(None, skel_a, skel_b).ratio() if skel_a and skel_b else 0.0
    return max(full, skel * 0.92)


def identity_assessment(entity_name: str, entity: dict, subject: Subject) -> tuple[int, list[str]] | None:
    best = 0.0
    for candidate in subject.names:
        best = max(best, name_similarity(entity_name, candidate))
    if best < 0.86:
        return None
    if best >= 0.98:
        points = 88
    elif best >= 0.92:
        points = 86
    else:
        points = 72
    notes = [f"Similarité de nom {best:.2f}. La donnée absente n'est pas comptée comme une correspondance."]
    entity_dob = str(entity.get("dob") or "").strip()
    subject_dob = str(subject.dob or "").strip()
    if entity_dob and subject_dob:
        if entity_dob != subject_dob:
            return None
        points += 8
        notes.append("Date de naissance identique.")
    else:
        notes.append("Date de naissance absente d'un côté : aucun point ajouté.")
    entity_nat = _codes(entity.get("nationality"))
    subject_nat = _codes(subject.nationality)
    if entity_nat and subject_nat:
        if entity_nat != subject_nat:
            points -= 15
            notes.append("Nationalité différente.")
        else:
            points += 4
            notes.append("Nationalité identique.")
    else:
        notes.append("Nationalité absente d'un côté : aucun point ajouté.")
    entity_country = _codes(entity.get("country"))
    subject_country = _codes(subject.country)
    if entity_country and subject_country:
        if entity_country != subject_country:
            points -= 10
            notes.append("Pays différent.")
        else:
            points += 4
            notes.append("Pays identique.")
    else:
        notes.append("Pays absent d'un côté : aucun point ajouté.")
    if entity.get("organization") and subject.organization:
        if name_similarity(str(entity["organization"]), subject.organization) >= 0.92:
            points += 4
            notes.append("Organisation associée concordante.")
        else:
            points -= 8
            notes.append("Organisation associée différente.")
    return max(0, min(100, points)), notes


def media_score_for(events: list[AdverseMediaEvent]) -> tuple[int, AdverseMediaEvent | None]:
    best = 0
    chosen = None
    for event in events:
        if event.negated:
            score = 0
        else:
            score = STANCE_MEDIA.get(event.stance, 0)
        if score >= best:
            best = score
            chosen = event
    return best, chosen


def portfolio(db: Session, tenant_id: str) -> list[Subject]:
    customers = db.scalars(
        select(Customer)
        .where(Customer.tenant_id == tenant_id)
        .options(selectinload(Customer.individual), selectinload(Customer.business))
    ).all()
    business_names = {}
    subjects: list[Subject] = []
    for customer in customers:
        aliases = [str(item) for item in (customer.extension or {}).get("aliases") or [] if str(item).strip()]
        if customer.kind == "individual" and customer.individual:
            profile: IndividualProfile = customer.individual
            subjects.append(
                Subject(
                    customer_id=customer.id,
                    party_id=None,
                    kind="person",
                    names=[customer.display_name, profile.legal_name, f"{profile.given_name} {profile.family_name}", *aliases],
                    dob=profile.date_of_birth,
                    nationality=profile.nationality,
                    country=profile.residence_country,
                    address=profile.address,
                )
            )
        if customer.kind == "business" and customer.business:
            profile_b: BusinessProfile = customer.business
            business_names[customer.id] = profile_b.legal_name
            subjects.append(
                Subject(
                    customer_id=customer.id,
                    party_id=None,
                    kind="organization",
                    names=[customer.display_name, profile_b.legal_name, profile_b.trading_name or "", *aliases],
                    country=profile_b.jurisdiction,
                    address=profile_b.registered_address,
                    organization=profile_b.legal_name,
                )
            )
    parties = db.scalars(select(Party).where(Party.tenant_id == tenant_id)).all()
    for party in parties:
        subjects.append(
            Subject(
                customer_id=party.business_customer_id,
                party_id=party.id,
                kind="person",
                names=[party.full_name],
                dob=party.date_of_birth,
                nationality=party.nationality,
                role=party.role,
                organization=business_names.get(party.business_customer_id),
            )
        )
    return [subject for subject in subjects if any(fold(name) for name in subject.names)]


def _dedup_key(customer_id: str | None, party_id: str | None, name: str, category: str, article_id: str) -> str:
    material = "|".join([customer_id or "", party_id or "", fold(name), category, article_id])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:64]


def _upsert_alert(
    db: Session,
    tenant_id: str,
    subject: Subject,
    entity: AdverseMediaEntity,
    match: AdverseMediaMatch,
    event: AdverseMediaEvent,
    article: AdverseMediaArticle,
) -> AdverseMediaAlert | None:
    if event.negated or event.stance not in ALERTING_STANCES:
        return None
    policy = get_policy(db, tenant_id)
    if match.identity_score < int(policy["identity_threshold"]) or match.media_score < int(policy["media_threshold"]):
        return None
    key = _dedup_key(subject.customer_id, subject.party_id, entity.name, event.category, article.id)
    alert = db.scalar(select(AdverseMediaAlert).where(AdverseMediaAlert.tenant_id == tenant_id, AdverseMediaAlert.dedup_key == key))
    payload = {
        "stance": event.stance,
        "category": event.category,
        "identity_score": match.identity_score,
        "media_score": match.media_score,
        "article_id": article.id,
        "article_title": article.title,
        "article_url": article.url,
        "excerpt": event.excerpt,
        "entity_name": entity.name,
        "guilt_conclusion": False,
        "explanation": match.explanation,
    }
    if alert is None:
        alert = AdverseMediaAlert(
            tenant_id=tenant_id,
            customer_id=subject.customer_id,
            match_id=match.id,
            event_id=event.id,
            title=f"{entity.name} — {event.category}",
            status="new",
            priority="high" if event.stance == "conviction" else "medium",
            dedup_key=key,
            payload=payload,
        )
        db.add(alert)
        db.flush()
        db.add(AdverseMediaInvestigation(tenant_id=tenant_id, alert_id=alert.id, status="new", comments=[], attachments=[]))
        return alert
    if alert.status in {"false_positive", "closed"}:
        history = list((alert.payload or {}).get("evolutions") or [])
        history.append({"at": utcnow().isoformat(), "stance": event.stance, "media_score": match.media_score})
        alert.payload = {**(alert.payload or {}), "evolutions": history[-10:]}
        return alert
    previous = STANCE_RANK.get(str((alert.payload or {}).get("stance") or "mention"), 0)
    if STANCE_RANK[event.stance] > previous:
        alert.payload = payload
        alert.match_id = match.id
        alert.event_id = event.id
        alert.priority = "high" if event.stance == "conviction" else alert.priority
        alert.updated_at = utcnow()
        _audit(db, tenant_id, "system", "alert.escalate", "alert", alert.id, {"stance": event.stance})
    return alert


def match_version(db: Session, tenant_id: str, version: AdverseMediaArticleVersion) -> dict:
    article = db.get(AdverseMediaArticle, version.article_id)
    if article is None:
        return {"matches": 0, "alerts": 0}
    entities = list(db.scalars(select(AdverseMediaEntity).where(AdverseMediaEntity.article_version_id == version.id)).all())
    events = list(db.scalars(select(AdverseMediaEvent).where(AdverseMediaEvent.article_version_id == version.id)).all())
    media, _event = media_score_for(events)
    subjects = portfolio(db, tenant_id)
    matched_customers: set[str] = set()
    match_count = 0
    alert_count = 0
    for entity in entities:
        attributes = dict(entity.attributes or {})
        for subject in subjects:
            if entity.kind == "organization" and subject.kind != "organization":
                continue
            if entity.kind == "person" and subject.kind == "organization":
                continue
            assessed = identity_assessment(entity.name, attributes, subject)
            if assessed is None:
                continue
            identity, notes = assessed
            if identity < 70:
                continue
            match = db.scalar(select(AdverseMediaMatch).where(AdverseMediaMatch.entity_id == entity.id, AdverseMediaMatch.subject_key == subject.key))
            if match is None:
                match = AdverseMediaMatch(
                    tenant_id=tenant_id,
                    entity_id=entity.id,
                    customer_id=subject.customer_id,
                    party_id=subject.party_id,
                    subject_key=subject.key,
                    identity_score=identity,
                    media_score=media,
                    explanation=notes,
                )
                db.add(match)
                db.flush()
            else:
                match.identity_score = identity
                match.media_score = media
                match.explanation = notes
            match_count += 1
            if subject.customer_id:
                matched_customers.add(subject.customer_id)
            for event in events:
                alert = _upsert_alert(db, tenant_id, subject, entity, match, event, article)
                if alert is not None and alert.status == "new":
                    alert_count += 1
    for subject in subjects:
        if not subject.customer_id or subject.party_id:
            continue
        db.add(
            AdverseMediaProcessingLog(
                tenant_id=tenant_id,
                article_version_id=version.id,
                customer_id=subject.customer_id,
                stage="match",
                status="completed",
                detail={
                    "hits": 1 if subject.customer_id in matched_customers else 0,
                    "engine": NLP_ENGINE,
                    "party_count": sum(1 for item in subjects if item.customer_id == subject.customer_id and item.party_id),
                },
            )
        )
    db.add(
        AdverseMediaProcessingLog(
            tenant_id=tenant_id,
            article_version_id=version.id,
            stage="match",
            status="completed",
            detail={"matches": match_count, "alerts": alert_count, "engine": NLP_ENGINE},
        )
    )
    db.flush()
    return {"matches": match_count, "alerts": alert_count}


def screen_customer(db: Session, tenant_id: str, customer_id: str, trigger: str = "manual") -> dict:
    customer = db.get(Customer, customer_id)
    if customer is None or customer.tenant_id != tenant_id:
        raise AdverseMediaError("Client introuvable.")
    policy = get_policy(db, tenant_id)
    since = utcnow() - timedelta(days=int(policy["article_window_days"]))
    versions = db.scalars(
        select(AdverseMediaArticleVersion)
        .join(AdverseMediaArticle, AdverseMediaArticle.id == AdverseMediaArticleVersion.article_id)
        .where(
            AdverseMediaArticle.tenant_id == tenant_id,
            AdverseMediaArticleVersion.nlp_status == "analyzed",
            AdverseMediaArticleVersion.analyzed_at >= since,
        )
    ).all()
    subjects = [subject for subject in portfolio(db, tenant_id) if subject.customer_id == customer_id]
    match_count = 0
    alert_count = 0
    for version in versions:
        article = db.get(AdverseMediaArticle, version.article_id)
        entities = list(db.scalars(select(AdverseMediaEntity).where(AdverseMediaEntity.article_version_id == version.id)).all())
        events = list(db.scalars(select(AdverseMediaEvent).where(AdverseMediaEvent.article_version_id == version.id)).all())
        media, _event = media_score_for(events)
        for entity in entities:
            for subject in subjects:
                if entity.kind == "organization" and subject.kind != "organization":
                    continue
                if entity.kind == "person" and subject.kind == "organization":
                    continue
                assessed = identity_assessment(entity.name, dict(entity.attributes or {}), subject)
                if assessed is None:
                    continue
                identity, notes = assessed
                if identity < 70:
                    continue
                match = db.scalar(
                    select(AdverseMediaMatch).where(AdverseMediaMatch.entity_id == entity.id, AdverseMediaMatch.subject_key == subject.key)
                )
                if match is None:
                    match = AdverseMediaMatch(
                        tenant_id=tenant_id,
                        entity_id=entity.id,
                        customer_id=subject.customer_id,
                        party_id=subject.party_id,
                        subject_key=subject.key,
                        identity_score=identity,
                        media_score=media,
                        explanation=notes,
                    )
                    db.add(match)
                    db.flush()
                match_count += 1
                if article is None:
                    continue
                for event in events:
                    before = db.scalar(select(func.count()).select_from(AdverseMediaAlert).where(AdverseMediaAlert.tenant_id == tenant_id))
                    _upsert_alert(db, tenant_id, subject, entity, match, event, article)
                    after = db.scalar(select(func.count()).select_from(AdverseMediaAlert).where(AdverseMediaAlert.tenant_id == tenant_id))
                    alert_count += int(after or 0) - int(before or 0)
    party_count = sum(1 for subject in subjects if subject.party_id)
    db.add(
        AdverseMediaProcessingLog(
            tenant_id=tenant_id,
            customer_id=customer_id,
            stage="screen",
            status="completed",
            detail={"trigger": trigger, "matches": match_count, "alerts": alert_count, "party_count": party_count, "engine": NLP_ENGINE},
        )
    )
    db.flush()
    return {"status": "completed", "matches": match_count, "alerts": alert_count, "engine": NLP_ENGINE}


def rescreen_due(db: Session, tenant_id: str) -> int:
    policy = get_policy(db, tenant_id)
    customers = db.scalars(select(Customer).where(Customer.tenant_id == tenant_id)).all()
    done = 0
    now = utcnow()
    for customer in customers:
        tier = customer.risk_tier if customer.risk_tier in {"high", "medium", "low"} else "low"
        hours = int(policy["frequencies_hours"][tier])
        last = db.scalar(
            select(AdverseMediaProcessingLog)
            .where(
                AdverseMediaProcessingLog.tenant_id == tenant_id,
                AdverseMediaProcessingLog.customer_id == customer.id,
                AdverseMediaProcessingLog.stage == "screen",
                AdverseMediaProcessingLog.status == "completed",
            )
            .order_by(AdverseMediaProcessingLog.created_at.desc())
        )
        party_count = int(db.scalar(select(func.count()).select_from(Party).where(Party.business_customer_id == customer.id)) or 0)
        due = last is None or last.created_at is None or now >= last.created_at + timedelta(hours=hours)
        changed = bool(last and customer.updated_at and last.created_at and customer.updated_at > last.created_at)
        parties_changed = bool(last and int((last.detail or {}).get("party_count") or 0) != party_count)
        if due or changed or parties_changed:
            screen_customer(db, tenant_id, customer.id, trigger="rescreen")
            done += 1
    return done


def run_due(db: Session, tenant_id: str | None = None) -> dict:
    tenant_ids = [tenant_id] if tenant_id else list(db.scalars(select(Tenant.id)).all())
    summary = {"collected": 0, "analyzed": 0, "rescreened": 0}
    now = utcnow()
    for current in tenant_ids:
        for source in ensure_sources(db, current):
            if not source.active:
                continue
            if source.last_collected_at and now < source.last_collected_at + timedelta(minutes=source.frequency_minutes):
                continue
            result = collect_source(db, source)
            summary["collected"] += int(result.get("stored") or 0)
        processed = process_queue(db, current)
        summary["analyzed"] += int(processed["analyzed"])
        summary["rescreened"] += rescreen_due(db, current)
    return summary


def record_decision(db: Session, alert: AdverseMediaAlert, outcome: str, rationale: str, actor: str) -> AdverseMediaDecision:
    if outcome not in DECISION_OUTCOMES:
        raise AdverseMediaError("Décision inconnue.")
    text = plain_text(rationale, 4000)
    if len(text) < 10:
        raise AdverseMediaError("La justification doit expliquer la décision.")
    decision = AdverseMediaDecision(tenant_id=alert.tenant_id, alert_id=alert.id, outcome=outcome, rationale=text, actor=actor or "analyst")
    db.add(decision)
    alert.status = outcome
    alert.updated_at = utcnow()
    investigation = db.scalar(select(AdverseMediaInvestigation).where(AdverseMediaInvestigation.alert_id == alert.id))
    if investigation:
        investigation.status = outcome
        investigation.updated_at = utcnow()
    _audit(db, alert.tenant_id, actor, "decision.record", "alert", alert.id, {"outcome": outcome})
    db.flush()
    if alert.customer_id:
        from app.services import assess_customer

        tenant = db.get(Tenant, alert.tenant_id)
        customer = db.get(Customer, alert.customer_id)
        if tenant and customer:
            assess_customer(db, tenant, customer, source="adverse-media-decision")
    return decision


def update_alert(db: Session, alert: AdverseMediaAlert, payload: dict, actor: str) -> AdverseMediaAlert:
    if "status" in payload:
        status = str(payload["status"])
        if status not in ALERT_STATUSES or status in DECISION_OUTCOMES:
            raise AdverseMediaError("Ce statut se décide avec une justification.")
        alert.status = status
    if "assignee_id" in payload:
        alert.assignee_id = str(payload.get("assignee_id") or "") or None
    investigation = db.scalar(select(AdverseMediaInvestigation).where(AdverseMediaInvestigation.alert_id == alert.id))
    if investigation is None:
        investigation = AdverseMediaInvestigation(tenant_id=alert.tenant_id, alert_id=alert.id, comments=[], attachments=[])
        db.add(investigation)
        db.flush()
    if payload.get("comment"):
        comments = list(investigation.comments or [])
        comments.append({"at": utcnow().isoformat(), "actor": actor, "text": plain_text(payload["comment"], 2000)})
        investigation.comments = comments
    investigation.status = alert.status
    investigation.assignee_id = alert.assignee_id
    investigation.updated_at = utcnow()
    alert.updated_at = utcnow()
    _audit(db, alert.tenant_id, actor, "alert.update", "alert", alert.id, {"status": alert.status})
    db.flush()
    return alert


def add_attachment(db: Session, alert: AdverseMediaAlert, name: str, storage_path: str, actor: str) -> dict:
    safe = re.sub(r"[^A-Za-z0-9._ -]", "", PathName(name))[:120] or "piece"
    investigation = db.scalar(select(AdverseMediaInvestigation).where(AdverseMediaInvestigation.alert_id == alert.id))
    if investigation is None:
        investigation = AdverseMediaInvestigation(tenant_id=alert.tenant_id, alert_id=alert.id, comments=[], attachments=[])
        db.add(investigation)
        db.flush()
    item = {"name": safe, "path": storage_path, "actor": actor, "at": utcnow().isoformat()}
    attachments = list(investigation.attachments or [])
    attachments.append(item)
    investigation.attachments = attachments[-10:]
    investigation.updated_at = utcnow()
    _audit(db, alert.tenant_id, actor, "attachment.add", "alert", alert.id, {"name": safe})
    db.flush()
    return item


def PathName(name: str) -> str:
    return str(name or "").replace("\\", "/").rsplit("/", 1)[-1]


def media_risk_factor(db: Session, customer: Customer) -> dict | None:
    """Points only after a human confirmed the alert. Allegations stay below reported convictions."""
    decisions = db.scalars(
        select(AdverseMediaDecision)
        .join(AdverseMediaAlert, AdverseMediaAlert.id == AdverseMediaDecision.alert_id)
        .where(AdverseMediaAlert.customer_id == customer.id, AdverseMediaDecision.outcome == "confirmed_relevant")
        .order_by(AdverseMediaDecision.created_at.desc())
    ).all()
    best = 0
    stance = ""
    for decision in decisions:
        alert = db.get(AdverseMediaAlert, decision.alert_id)
        if alert is None:
            continue
        current = str((alert.payload or {}).get("stance") or "")
        if current == "conviction":
            points = 16
        elif current in {"allegation", "investigation", "prosecution"}:
            points = 8
        else:
            points = 0
        if points > best:
            best = points
            stance = current
    if best <= 0:
        return None
    return {
        "code": "media",
        "points": min(24, best),
        "explanation": (
            f"Médias défavorables confirmés par un analyste ({stance}). "
            "Le moteur ne conclut pas à la culpabilité. Plafond du facteur : 24."
        ),
    }


def reanalyze(db: Session, article: AdverseMediaArticle, *, force: bool = False) -> str:
    version = db.scalar(
        select(AdverseMediaArticleVersion)
        .where(AdverseMediaArticleVersion.article_id == article.id)
        .order_by(AdverseMediaArticleVersion.version.desc())
    )
    if version is None:
        raise AdverseMediaError("Version introuvable.")
    if not force and version.nlp_status == "analyzed" and version.nlp_engine_version == NLP_ENGINE:
        return "unchanged"
    confirmed = False
    alerts = db.scalars(select(AdverseMediaAlert).where(AdverseMediaAlert.tenant_id == article.tenant_id)).all()
    for alert in alerts:
        if str((alert.payload or {}).get("article_id") or "") != article.id:
            continue
        if db.scalar(select(AdverseMediaDecision.id).where(AdverseMediaDecision.alert_id == alert.id, AdverseMediaDecision.outcome == "confirmed_relevant")):
            confirmed = True
            break
    if confirmed:
        raise AdverseMediaError("Une décision humaine confirmée bloque la réanalyse de cet article.")
    outcome = analyze_version(db, version, force=True)
    if outcome == "analyzed":
        match_version(db, article.tenant_id, version)
    return outcome


def retry_failed(db: Session, version: AdverseMediaArticleVersion) -> str:
    if version.nlp_status != "failed":
        return analyze_version(db, version)
    version.nlp_status = "pending"
    version.error = None
    db.flush()
    article = db.get(AdverseMediaArticle, version.article_id)
    outcome = analyze_version(db, version, force=True)
    if outcome == "analyzed" and article is not None:
        match_version(db, article.tenant_id, version)
    return outcome


def desk_summary(db: Session, customer: Customer) -> dict:
    last = db.scalar(
        select(AdverseMediaProcessingLog)
        .where(
            AdverseMediaProcessingLog.customer_id == customer.id,
            AdverseMediaProcessingLog.stage.in_(("screen", "match")),
            AdverseMediaProcessingLog.status == "completed",
        )
        .order_by(AdverseMediaProcessingLog.created_at.desc())
    )
    alerts = db.scalars(
        select(AdverseMediaAlert).where(AdverseMediaAlert.customer_id == customer.id).order_by(AdverseMediaAlert.created_at.desc())
    ).all()
    rendered = []
    for alert in alerts:
        decisions = db.scalars(
            select(AdverseMediaDecision).where(AdverseMediaDecision.alert_id == alert.id).order_by(AdverseMediaDecision.created_at.asc())
        ).all()
        investigation = db.scalar(select(AdverseMediaInvestigation).where(AdverseMediaInvestigation.alert_id == alert.id))
        payload = alert.payload or {}
        rendered.append(
            {
                "id": alert.id,
                "title": alert.title,
                "status": alert.status,
                "priority": alert.priority,
                "identity_score": payload.get("identity_score"),
                "media_score": payload.get("media_score"),
                "stance": payload.get("stance"),
                "category": payload.get("category"),
                "excerpt": payload.get("excerpt"),
                "article_title": payload.get("article_title"),
                "article_url": payload.get("article_url"),
                "entity_name": payload.get("entity_name"),
                "explanation": payload.get("explanation") or [],
                "guilt_conclusion": False,
                "comments": list((investigation.comments if investigation else []) or []),
                "attachments": [{"name": item.get("name"), "at": item.get("at")} for item in ((investigation.attachments if investigation else []) or [])],
                "decisions": [
                    {"id": item.id, "outcome": item.outcome, "rationale": item.rationale, "actor": item.actor, "created_at": item.created_at.isoformat()}
                    for item in decisions
                ],
            }
        )
    matches = db.scalars(select(AdverseMediaMatch).where(AdverseMediaMatch.customer_id == customer.id).order_by(AdverseMediaMatch.created_at.desc())).all()
    articles = []
    seen = set()
    for match in matches:
        entity = db.get(AdverseMediaEntity, match.entity_id)
        if entity is None or entity.article_version_id in seen:
            continue
        seen.add(entity.article_version_id)
        version = db.get(AdverseMediaArticleVersion, entity.article_version_id)
        article = db.get(AdverseMediaArticle, version.article_id) if version else None
        if article is None or version is None:
            continue
        articles.append(
            {
                "id": article.id,
                "title": article.title,
                "url": article.url,
                "status": article.status,
                "analyzed_at": version.analyzed_at.isoformat() if version.analyzed_at else None,
                "engine": version.nlp_engine_version,
                "identity_score": match.identity_score,
                "media_score": match.media_score,
                "stance": (version.analysis or {}).get("stance"),
                "guilt_conclusion": False,
            }
        )
    open_count = sum(1 for alert in alerts if alert.status in {"new", "in_review", "waiting_info", "escalated"})
    return {
        "last_screening": None
        if last is None
        else {
            "at": last.created_at.isoformat() if last.created_at else None,
            "status": last.status,
            "stage": last.stage,
            "trigger": (last.detail or {}).get("trigger") or last.stage,
            "matches": (last.detail or {}).get("matches") or (last.detail or {}).get("hits") or 0,
            "alerts": (last.detail or {}).get("alerts") or 0,
            "engine": (last.detail or {}).get("engine"),
        },
        "alerts": rendered,
        "articles": articles,
        "open_count": open_count,
        "guilt_conclusion": False,
    }


def search_name(db: Session, tenant_id: str, name: str, dob: str | None = None, country: str | None = None) -> list[dict]:
    results = []
    entities = db.scalars(select(AdverseMediaEntity).where(AdverseMediaEntity.tenant_id == tenant_id)).all()
    for entity in entities:
        assessed = identity_assessment(entity.name, {"dob": None, "nationality": None, "country": None}, Subject(
            customer_id=None,
            party_id=None,
            kind=entity.kind,
            names=[name],
            dob=dob,
            nationality=country,
            country=country,
        ))
        if assessed is None:
            continue
        identity, notes = assessed
        version = db.get(AdverseMediaArticleVersion, entity.article_version_id)
        article = db.get(AdverseMediaArticle, version.article_id) if version else None
        results.append(
            {
                "entity": entity.name,
                "identity_score": identity,
                "article_title": article.title if article else "",
                "explanation": notes,
                "guilt_conclusion": False,
            }
        )
    results.sort(key=lambda item: item["identity_score"], reverse=True)
    return results[:50]


def source_view(source: AdverseMediaSource) -> dict:
    logs = []
    return {
        "id": source.id,
        "name": source.name,
        "kind": source.kind,
        "url": source.url,
        "language": source.language,
        "frequency_minutes": source.frequency_minutes,
        "active": source.active,
        "last_collected_at": source.last_collected_at.isoformat() if source.last_collected_at else None,
        "availability": source.availability,
        "last_error": source.last_error,
        "config": source.config or {},
        "recent_errors": logs,
    }


def source_history(db: Session, source: AdverseMediaSource) -> dict:
    view = source_view(source)
    rows = db.scalars(
        select(AdverseMediaCollectionLog)
        .where(AdverseMediaCollectionLog.source_id == source.id)
        .order_by(AdverseMediaCollectionLog.started_at.desc())
        .limit(20)
    ).all()
    view["recent_errors"] = [
        {"status": row.status, "error": row.error, "fetched": row.fetched_count, "stored": row.stored_count, "at": row.started_at.isoformat() if row.started_at else None}
        for row in rows
    ]
    return view
