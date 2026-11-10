"""Adverse media API. Collection never fabricates articles."""

from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adverse_media import (
    AdverseMediaError,
    add_attachment,
    collect_source,
    desk_summary,
    ensure_sources,
    get_policy,
    process_queue,
    reanalyze,
    record_decision,
    retry_failed,
    run_due,
    save_policy,
    screen_customer,
    search_name,
    source_history,
    update_alert,
    update_source,
)
from app.config import ROOT
from app.deps import clarity_principal, db_session
from app.models import (
    AdverseMediaAlert,
    AdverseMediaArticle,
    AdverseMediaArticleVersion,
    AdverseMediaCollectionLog,
    AdverseMediaProcessingLog,
    AdverseMediaSource,
)
from app.security import Principal
from app.services import actor_label, clarity_error, store_bytes
from app.timeutil import iso

router = APIRouter(prefix="/api/clarity/v1/adverse-media", tags=["adverse-media"])


def _failure(exc: AdverseMediaError):
    raise clarity_error(400, "invalid", exc.message)


def _source(db: Session, principal: Principal, source_id: str) -> AdverseMediaSource:
    row = db.get(AdverseMediaSource, source_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Source introuvable.")
    return row


def _article(db: Session, principal: Principal, article_id: str) -> AdverseMediaArticle:
    row = db.get(AdverseMediaArticle, article_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Article introuvable.")
    return row


def _alert(db: Session, principal: Principal, alert_id: str) -> AdverseMediaAlert:
    row = db.get(AdverseMediaAlert, alert_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Alerte introuvable.")
    return row


@router.get("/sources")
def list_sources(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = ensure_sources(db, principal.tenant_id)
    return [source_history(db, row) for row in rows]


@router.patch("/sources/{source_id}")
def patch_source(source_id: str, body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin"):
        raise clarity_error(403, "forbidden", "Seul un administrateur paramètre les sources.")
    try:
        row = update_source(db, _source(db, principal, source_id), body, actor_label(principal))
    except AdverseMediaError as exc:
        _failure(exc)
    return source_history(db, row)


@router.post("/sources/{source_id}/collect")
def collect(source_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin", "analyst"):
        raise clarity_error(403, "forbidden", "Collecte réservée à l'administrateur ou à l'analyste.")
    result = collect_source(db, _source(db, principal, source_id))
    processed = process_queue(db, principal.tenant_id)
    return {"collection": result, "processing": processed}


@router.get("/articles")
def list_articles(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(
        select(AdverseMediaArticle).where(AdverseMediaArticle.tenant_id == principal.tenant_id).order_by(AdverseMediaArticle.created_at.desc()).limit(100)
    ).all()
    return [
        {
            "id": row.id,
            "title": row.title,
            "url": row.url,
            "status": row.status,
            "language": row.language,
            "content_hash": row.content_hash,
            "published_at": iso(row.published_at) if row.published_at else None,
        }
        for row in rows
    ]


@router.get("/articles/{article_id}")
def article_detail(article_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    article = _article(db, principal, article_id)
    versions = db.scalars(
        select(AdverseMediaArticleVersion).where(AdverseMediaArticleVersion.article_id == article.id).order_by(AdverseMediaArticleVersion.version.asc())
    ).all()
    return {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "status": article.status,
        "content_hash": article.content_hash,
        "versions": [
            {
                "id": version.id,
                "version": version.version,
                "content_hash": version.content_hash,
                "nlp_status": version.nlp_status,
                "nlp_engine_version": version.nlp_engine_version,
                "analyzed_at": iso(version.analyzed_at) if version.analyzed_at else None,
                "error": version.error,
                "analysis": version.analysis,
            }
            for version in versions
        ],
    }


@router.post("/articles/{article_id}/reanalyze")
def reanalyze_article(article_id: str, body: dict | None = None, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin", "analyst"):
        raise clarity_error(403, "forbidden", "Réanalyse réservée à l'analyste.")
    try:
        outcome = reanalyze(db, _article(db, principal, article_id), force=bool((body or {}).get("force")))
    except AdverseMediaError as exc:
        _failure(exc)
    return {"status": outcome}


@router.post("/versions/{version_id}/retry")
def retry_version(version_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    version = db.get(AdverseMediaArticleVersion, version_id)
    article = db.get(AdverseMediaArticle, version.article_id) if version else None
    if not version or not article or article.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Version introuvable.")
    return {"status": retry_failed(db, version)}


@router.post("/process")
def process(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    return process_queue(db, principal.tenant_id)


@router.get("/alerts")
def list_alerts(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(
        select(AdverseMediaAlert).where(AdverseMediaAlert.tenant_id == principal.tenant_id).order_by(AdverseMediaAlert.created_at.desc()).limit(200)
    ).all()
    return [
        {"id": row.id, "title": row.title, "status": row.status, "customer_id": row.customer_id, "payload": row.payload}
        for row in rows
    ]


@router.patch("/alerts/{alert_id}")
def patch_alert(alert_id: str, body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("analyst", "investigator", "checker"):
        raise clarity_error(403, "forbidden", "Mise à jour réservée à la conformité.")
    try:
        row = update_alert(db, _alert(db, principal, alert_id), body, actor_label(principal))
    except AdverseMediaError as exc:
        _failure(exc)
    return {"id": row.id, "status": row.status}


@router.post("/alerts/{alert_id}/decision")
def decide_alert(alert_id: str, body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("analyst", "investigator", "checker"):
        raise clarity_error(403, "forbidden", "Décision réservée à la conformité.")
    try:
        decision = record_decision(db, _alert(db, principal, alert_id), str(body.get("outcome") or ""), str(body.get("rationale") or ""), actor_label(principal))
    except AdverseMediaError as exc:
        _failure(exc)
    return {"id": decision.id, "outcome": decision.outcome}


@router.post("/alerts/{alert_id}/attachments")
async def upload_attachment(
    alert_id: str,
    file: UploadFile = File(...),
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    if not principal.allows("analyst", "investigator", "checker"):
        raise clarity_error(403, "forbidden", "Pièce réservée à la conformité.")
    content = await file.read()
    name = (file.filename or "piece.bin").rsplit("/", 1)[-1]
    if len(content) > 2_000_000 or len(content) == 0:
        raise clarity_error(400, "invalid", "La pièce doit faire entre 1 octet et 2 Mo.")
    if not name.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp")):
        raise clarity_error(400, "invalid", "Formats acceptés : pdf, png, jpg, webp.")
    if content.startswith(b"MZ") or content.startswith(b"\x7fELF") or b"<script" in content.lower():
        raise clarity_error(400, "invalid", "Contenu refusé.")
    path, _digest = store_bytes(Path(ROOT / "data" / "uploads"), principal.tenant_id, name, content, db=db)
    item = add_attachment(db, _alert(db, principal, alert_id), name, path, actor_label(principal))
    return {"name": item["name"]}


@router.get("/policy")
def read_policy(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    return get_policy(db, principal.tenant_id)


@router.put("/policy")
def write_policy(body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin"):
        raise clarity_error(403, "forbidden", "Seul un administrateur paramètre la surveillance.")
    try:
        return save_policy(db, principal.tenant_id, body, actor_label(principal))
    except (TypeError, ValueError) as exc:
        raise clarity_error(400, "invalid", "Paramètres illisibles.") from exc


@router.get("/logs")
def logs(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    collections = db.scalars(
        select(AdverseMediaCollectionLog).where(AdverseMediaCollectionLog.tenant_id == principal.tenant_id).order_by(AdverseMediaCollectionLog.started_at.desc()).limit(50)
    ).all()
    processing = db.scalars(
        select(AdverseMediaProcessingLog)
        .where(AdverseMediaProcessingLog.tenant_id == principal.tenant_id, AdverseMediaProcessingLog.customer_id.is_(None))
        .order_by(AdverseMediaProcessingLog.created_at.desc())
        .limit(50)
    ).all()
    return {
        "collections": [
            {"source_id": row.source_id, "status": row.status, "fetched": row.fetched_count, "stored": row.stored_count, "error": row.error, "at": iso(row.started_at)}
            for row in collections
        ],
        "processing": [
            {"stage": row.stage, "status": row.status, "attempt": row.attempt, "error": row.error, "detail": row.detail, "at": iso(row.created_at)}
            for row in processing
        ],
    }


@router.post("/customers/{customer_id}/screen")
def screen(customer_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("analyst", "investigator", "checker"):
        raise clarity_error(403, "forbidden", "Criblage réservé à la conformité.")
    try:
        result = screen_customer(db, principal.tenant_id, customer_id, trigger="manual")
    except AdverseMediaError as exc:
        _failure(exc)
    from app.models import Customer

    customer = db.get(Customer, customer_id)
    return {"screening": result, "adverse_media": desk_summary(db, customer) if customer else None}


@router.post("/search")
def search(body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    name = str(body.get("name") or "").strip()
    if len(name) < 3:
        raise clarity_error(400, "invalid", "Le nom recherché est trop court.")
    return {"results": search_name(db, principal.tenant_id, name, body.get("dob"), body.get("country")), "guilt_conclusion": False}


@router.post("/cycle")
def cycle(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin"):
        raise clarity_error(403, "forbidden", "Cycle réservé à l'administrateur.")
    return run_due(db, principal.tenant_id)


@router.get("/customers/{customer_id}")
def customer_media(customer_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    from app.models import Customer

    customer = db.get(Customer, customer_id)
    if not customer or customer.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Client introuvable.")
    return desk_summary(db, customer)


@router.get("/files/{alert_id}/{name}")
def attachment_content(alert_id: str, name: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    from app.models import AdverseMediaInvestigation

    alert = _alert(db, principal, alert_id)
    investigation = db.scalar(select(AdverseMediaInvestigation).where(AdverseMediaInvestigation.alert_id == alert.id))
    item = next((row for row in ((investigation.attachments if investigation else []) or []) if row.get("name") == name), None)
    if not item:
        raise clarity_error(404, "not_found", "Pièce introuvable.")
    path = Path(str(item.get("path") or ""))
    if not path.is_file():
        raise clarity_error(404, "not_found", "Pièce introuvable.")
    return Response(content=path.read_bytes(), media_type="application/octet-stream")
