"""Every Marble console operation that is not already implemented by a dedicated route."""

import csv
import io
import json
import re
import secrets
from collections import Counter
from datetime import timedelta
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.catalog import add_doc, data_model_dto, get_doc, list_docs, touch
from app.config import ROOT
from app.deps import db_session, marble_principal
from app.models import (
    ApiCredential,
    AuditEvent,
    CaseComment,
    CaseRecord,
    CaseTag,
    Customer,
    DataModelField,
    Decision,
    Evidence,
    Inbox,
    IngestedObject,
    RegulatoryReport,
    RiskAssessment,
    RuleSnooze,
    Scenario,
    ScreeningCheck,
    ScreeningMatch,
    Tag,
    Tenant,
    User,
)
from app.providers import ai_configured, complete_ai, dataset_catalog
from app.security import Principal, encode_jwt, hash_token
from app.services import execute_scenario, ingest_object, marble_error, persist_screening, require_uuid, store_bytes
from app.timeutil import iso, utcnow

_OPS: list[tuple[str, str, str]] | None = None


def console_operations() -> list[tuple[str, str, str]]:
    global _OPS
    if _OPS is not None:
        return _OPS
    root = ROOT / "docs" / "api-compatibility" / "marble" / "original" / "console"
    index = yaml.safe_load((root / "marblecore-api.yaml").read_text())
    cache: dict[str, dict] = {}
    found = []
    for path, item in index["paths"].items():
        filename, pointer = item["$ref"].split("#")
        if filename not in cache:
            cache[filename] = yaml.safe_load((root / "marblecore-api" / Path(filename).name).read_text())
        key = pointer[1:].replace("~1", "/").replace("~0", "~")
        node = cache[filename][key]
        for method in ("get", "post", "put", "patch", "delete"):
            if method in node:
                found.append((method.upper(), path, node[method].get("operationId") or f"{method}_{path}"))
    _OPS = found
    return found


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def mount(app: FastAPI) -> None:
    existing = {(method, _norm(route.path)) for route in app.routes if getattr(route, "methods", None) and getattr(route, "path", None) for method in route.methods if method not in {"HEAD", "OPTIONS"}}
    for method, path, operation_id in console_operations():
        if (method, _norm(path)) in existing:
            continue
        app.add_api_route(path, _endpoint(path, operation_id), methods=[method], name=f"console_{operation_id}")
        existing.add((method, _norm(path)))


def _endpoint(path: str, operation_id: str):
    names = re.findall(r"\{([^}]+)\}", path)
    signature = ", ".join(f"{name}: str" for name in names)
    comma = ", " if names else ""
    mapping = ", ".join(f"{name!r}: {name}" for name in names)
    namespace = {
        "handle": handle,
        "Depends": Depends,
        "Request": Request,
        "Session": Session,
        "Principal": Principal,
        "marble_principal": marble_principal,
        "db_session": db_session,
        "operation_id": operation_id,
        "json": json,
    }
    exec(
        f"""
async def endpoint({signature}{comma}request: Request, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    payload = {{}}
    if request.method in {{"POST", "PUT", "PATCH"}}:
        raw = await request.body()
        if raw and "application/json" in request.headers.get("content-type", ""):
            payload = json.loads(raw)
        elif raw and not request.headers.get("content-type", "").startswith("multipart"):
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {{}}
    return handle(operation_id, request, principal, db, {{{mapping}}}, payload)
""",
        namespace,
    )
    namespace["endpoint"].__name__ = operation_id
    return namespace["endpoint"]


def handle(operation_id: str, request: Request, principal: Principal, db: Session, params: dict, body: dict):
    handler = HANDLERS.get(operation_id)
    if handler:
        return handler(request, principal, db, params, body)
    return _stored(operation_id, request, principal, db, params, body)


def _page(items: list) -> dict:
    return {"items": items, "has_next_page": False}


def _case(db: Session, principal: Principal, case_id: str) -> CaseRecord:
    require_uuid(case_id, "caseId")
    row = db.get(CaseRecord, case_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "case not found")
    return row


def _case_dto(db: Session, row: CaseRecord) -> dict:
    tag_rows = []
    for link in db.scalars(select(CaseTag).where(CaseTag.case_id == row.id)).all():
        tag = db.get(Tag, link.tag_id)
        if tag:
            tag_rows.append({"id": tag.id, "name": tag.name, "target": tag.target})
    count = db.scalar(select(func.count()).select_from(Decision).where(Decision.case_id == row.id)) or 0
    return {
        "id": row.id,
        "created_at": iso(row.created_at),
        "decisions_count": int(count),
        "name": row.name,
        "status": row.status,
        "outcome": row.outcome,
        "review_level": row.review_level,
        "inbox_id": row.inbox_id,
        "contributors": [],
        "tags": tag_rows,
        "type": "decision",
        "assignee_id": row.assignee_id,
        "snoozed_until": iso(row.snoozed_until) if row.snoozed_until else None,
    }


def _inbox(db: Session, tenant_id: str) -> Inbox:
    row = db.scalar(select(Inbox).where(Inbox.tenant_id == tenant_id))
    if not row:
        row = Inbox(tenant_id=tenant_id, name="Conformité")
        db.add(row)
        db.flush()
    return row


def _user_ref(db: Session, user_id: str | None) -> dict | None:
    if not user_id:
        return None
    user = db.get(User, user_id)
    if not user:
        return None
    return {"id": user.id, "name": user.full_name, "email": user.email}


def _review_text(db: Session, case: CaseRecord) -> tuple[str, str, bool]:
    decisions = db.scalars(select(Decision).where(Decision.case_id == case.id)).all()
    matches = db.scalars(select(ScreeningMatch).where(ScreeningMatch.tenant_id == case.tenant_id, ScreeningMatch.status == "pending")).all()
    facts = [
        f"Dossier {case.name}, statut {case.status}, issue {case.outcome}.",
        f"{len(decisions)} décision(s) liée(s).",
        f"{len(matches)} correspondance(s) de criblage encore en attente. Une correspondance n'est pas une confirmation.",
        "Le texte qui suit prépare l'investigation. Il ne qualifie pas un blanchiment et ne confirme pas une identité.",
    ]
    narrative = complete_ai(
        "Tu es un analyste conformité. Tu résumes des faits. Tu ne conclus jamais à un blanchiment ni à une correspondance confirmée.",
        "\n".join(facts),
    )
    if narrative:
        return narrative, "configured-model", True
    return "\n".join(facts), "clarity-local-analyst", False


def post_token(request, principal, db, params, body):
    settings = request.app.state.settings
    token = encode_jwt(settings.jwt_secret, {"tenant_id": principal.tenant_id, "email": principal.email or "api@clarity.bank", "role": principal.role}, settings.jwt_ttl_minutes)
    return {"access_token": token, "token_type": "Bearer", "expires_at": iso(utcnow() + timedelta(minutes=settings.jwt_ttl_minutes))}


def get_credentials(request, principal, db, params, body):
    return {
        "credentials": {
            "organization_id": principal.tenant_id,
            "roles": [principal.role],
            "actor_identity": {"user_id": principal.user_id or principal.credential_id or principal.tenant_id, "email": principal.email or ""},
            "permissions": ["console"],
        }
    }


def list_orgs(request, principal, db, params, body):
    tenant = db.get(Tenant, principal.tenant_id)
    return [{"id": tenant.id, "name": tenant.name}] if tenant else []


def list_cases(request, principal, db, params, body):
    rows = db.scalars(select(CaseRecord).where(CaseRecord.tenant_id == principal.tenant_id).order_by(CaseRecord.created_at.desc())).all()
    return _page([_case_dto(db, row) for row in rows])


def create_case(request, principal, db, params, body):
    inbox = _inbox(db, principal.tenant_id)
    row = CaseRecord(tenant_id=principal.tenant_id, inbox_id=body.get("inbox_id") or inbox.id, name=body.get("name") or "Dossier", status="pending", outcome="unset")
    db.add(row)
    db.flush()
    return _case_dto(db, row)


def get_case(request, principal, db, params, body):
    return _case_dto(db, _case(db, principal, params["caseId"]))


def update_case(request, principal, db, params, body):
    row = _case(db, principal, params["caseId"])
    if body.get("name"):
        row.name = str(body["name"])
    if body.get("status") in {"pending", "investigating", "closed"}:
        row.status = body["status"]
    if body.get("outcome"):
        row.outcome = str(body["outcome"])
    if "inbox_id" in body:
        row.inbox_id = str(body["inbox_id"])
    row.updated_at = utcnow()
    return _case_dto(db, row)


def assign_user(request, principal, db, params, body):
    row = _case(db, principal, params["caseId"])
    user_id = body.get("user_id") or body.get("assignee_id")
    user = db.get(User, str(user_id or ""))
    if not user or user.tenant_id != principal.tenant_id:
        raise marble_error(400, "invalid_payload", "user_id is required")
    row.assignee_id = user.id
    row.status = "investigating"
    return _case_dto(db, row)


def unassign_user(request, principal, db, params, body):
    row = _case(db, principal, params["caseId"])
    row.assignee_id = None
    return _case_dto(db, row)


def case_decisions(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    rows = db.scalars(select(Decision).where(Decision.tenant_id == principal.tenant_id, Decision.case_id == case.id)).all()
    return _page([{"id": row.id, "outcome": row.outcome, "score": row.score, "created_at": iso(row.created_at)} for row in rows])


def add_decisions(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    ids = body.get("decision_ids") or []
    for decision_id in ids:
        decision = db.get(Decision, str(decision_id))
        if decision and decision.tenant_id == principal.tenant_id:
            decision.case_id = case.id
    return case_decisions(request, principal, db, params, body)


def add_entities(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    add_doc(db, principal.tenant_id, "case_entity", case.id, body, parent_id=case.id)
    return {"case_id": case.id, "entities": [row.payload for row in list_docs(db, principal.tenant_id, "case_entity", case.id)]}


def remove_entities(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    for row in list_docs(db, principal.tenant_id, "case_entity", case.id):
        db.delete(row)
    return {"case_id": case.id, "entities": []}


def add_comment(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    text = body.get("comment") or body.get("body")
    if not isinstance(text, str) or not text:
        raise marble_error(400, "invalid_payload", "comment is required")
    row = CaseComment(tenant_id=principal.tenant_id, case_id=case.id, user_id=principal.user_id, comment=text[:16384])
    db.add(row)
    db.flush()
    return {"id": row.id, "comment": row.comment, "created_at": iso(row.created_at), "user": _user_ref(db, row.user_id)}


def snooze_case(request, principal, db, params, body):
    row = _case(db, principal, params["caseId"])
    row.snoozed_until = utcnow() + timedelta(days=int(body.get("days") or 7))
    return _case_dto(db, row)


def unsnooze_case(request, principal, db, params, body):
    row = _case(db, principal, params["caseId"])
    row.snoozed_until = None
    return _case_dto(db, row)


def update_case_tags(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    for link in db.scalars(select(CaseTag).where(CaseTag.case_id == case.id)).all():
        db.delete(link)
    for tag_id in body.get("tag_ids") or []:
        tag = db.get(Tag, str(tag_id))
        if tag and tag.tenant_id == principal.tenant_id:
            db.add(CaseTag(case_id=case.id, tag_id=tag.id))
    db.flush()
    return _case_dto(db, case)


def download_case_file(request, principal, db, params, body):
    row = db.get(Evidence, params["caseFileId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "file not found")
    return Response(content=Path(row.storage_path).read_bytes(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{row.file_name}"'})


def review_decision(request, principal, db, params, body):
    decision = db.get(Decision, str(body.get("decision_id") or ""))
    if not decision or decision.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "decision not found")
    if body.get("review_status") not in {"approve", "decline", "pending"}:
        raise marble_error(400, "invalid_payload", "review_status must be approve, decline or pending")
    decision.review_status = body["review_status"]
    return {"id": decision.id, "review_status": decision.review_status, "outcome": decision.outcome}


def pivot_objects(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    return {"case_id": case.id, "objects": [{"object_type": "customers", "object_id": case.customer_id}] if case.customer_id else []}


def related_by_pivot(request, principal, db, params, body):
    rows = db.scalars(select(CaseRecord).where(CaseRecord.tenant_id == principal.tenant_id)).all()
    return _page([_case_dto(db, row) for row in rows if params["pivotValue"] in (row.name or "")])


def related_by_object(request, principal, db, params, body):
    rows = db.scalars(select(CaseRecord).where(CaseRecord.tenant_id == principal.tenant_id, CaseRecord.customer_id == params["objectId"])).all()
    return _page([_case_dto(db, row) for row in rows])


def next_case(request, principal, db, params, body):
    current = _case(db, principal, params["caseId"])
    row = db.scalar(select(CaseRecord).where(CaseRecord.tenant_id == principal.tenant_id, CaseRecord.status != "closed", CaseRecord.id != current.id))
    return _case_dto(db, row) if row else {}


def escalate_case(request, principal, db, params, body):
    row = _case(db, principal, params["caseId"])
    row.escalated = True
    row.review_level = "checker"
    return _case_dto(db, row)


def _save_review(db, principal, case: CaseRecord):
    text, provider, production = _review_text(db, case)
    return add_doc(
        db,
        principal.tenant_id,
        "case_review",
        case.id,
        {"case_id": case.id, "status": "completed", "reaction": None, "text": text, "provider": provider, "production": production},
        parent_id=case.id,
        status="completed",
    )


def latest_review(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    rows = list_docs(db, principal.tenant_id, "case_review", case.id)
    if not rows:
        raise marble_error(404, "not_found", "review not found")
    row = rows[-1]
    return {"id": row.id, "case_id": case.id, "status": row.payload["status"], "reaction": row.payload.get("reaction"), "text": row.payload["text"], "provider": row.payload["provider"], "production": row.payload["production"], "created_at": iso(row.created_at), "updated_at": iso(row.updated_at)}


def enqueue_review(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    row = _save_review(db, principal, case)
    return latest_review(request, principal, db, {"caseId": case.id, "reviewId": row.id}, body) | {"id": row.id}


def get_review(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "case_review", params["reviewId"])
    if not row:
        raise marble_error(404, "not_found", "review not found")
    return {"id": row.id, **row.payload, "created_at": iso(row.created_at), "updated_at": iso(row.updated_at)}


def review_feedback(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "case_review", params["reviewId"])
    if not row:
        raise marble_error(404, "not_found", "review not found")
    if body.get("reaction") not in {"ok", "ko", None}:
        raise marble_error(400, "invalid_payload", "reaction must be ok or ko")
    row.payload = {**row.payload, "reaction": body.get("reaction")}
    touch(row)
    return get_review(request, principal, db, params, body)


def list_reviews(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    return [get_review(request, principal, db, {"reviewId": row.id}, body) for row in list_docs(db, principal.tenant_id, "case_review", case.id)]


def investigation_data(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    return {"case": _case_dto(db, case), "decisions": case_decisions(request, principal, db, params, body)["items"], "reviews": list_reviews(request, principal, db, params, body)}


def enrich_kyc(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    customer = db.get(Customer, case.customer_id) if case.customer_id else None
    name = customer.display_name if customer else case.name
    tenant = db.get(Tenant, principal.tenant_id)
    screening = persist_screening(db, tenant, {"Person": {"name": name}}, rule_id=None, decision_id=None, customer_id=customer.id if customer else None, counterparty=name)
    return {"case_id": case.id, "screening_id": screening.id, "status": screening.status, "production": screening.production, "provider": "opensanctions" if screening.production else "clarity-synthetic-watchlist"}


def mass_update(request, principal, db, params, body):
    updated = []
    for case_id in body.get("case_ids") or []:
        row = db.get(CaseRecord, str(case_id))
        if not row or row.tenant_id != principal.tenant_id:
            continue
        if body.get("status") in {"pending", "investigating", "closed"}:
            row.status = body["status"]
        if body.get("assignee_id"):
            row.assignee_id = str(body["assignee_id"])
        updated.append(row.id)
    return {"updated": updated}


def sar_list(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    rows = db.scalars(select(RegulatoryReport).where(RegulatoryReport.case_id == case.id)).all()
    return [{"id": row.id, "status": row.status, "jurisdiction": row.jurisdiction, "submission_channel": row.submission_channel, "created_at": iso(row.created_at)} for row in rows]


def sar_create(request, principal, db, params, body):
    case = _case(db, principal, params["caseId"])
    row = RegulatoryReport(tenant_id=principal.tenant_id, case_id=case.id, report_type="tracfin-draft", jurisdiction=body.get("jurisdiction") or "FR", status="draft", content=body.get("content") or {"narrative": ""}, submission_channel="not_connected", created_by=principal.user_id)
    db.add(row)
    db.flush()
    return {"id": row.id, "status": row.status, "submission_channel": "not_connected"}


def sar_update(request, principal, db, params, body):
    row = db.get(RegulatoryReport, params["reportId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "report not found")
    row.content = {**(row.content or {}), **(body.get("content") or body)}
    if body.get("status"):
        row.status = str(body["status"])
    row.submission_channel = "not_connected"
    return {"id": row.id, "status": row.status, "content": row.content, "submission_channel": row.submission_channel}


def sar_delete(request, principal, db, params, body):
    row = db.get(RegulatoryReport, params["reportId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "report not found")
    db.delete(row)
    return {"id": params["reportId"]}


def sar_download(request, principal, db, params, body):
    row = db.get(RegulatoryReport, params["reportId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "report not found")
    return {"id": row.id, "status": row.status, "submission_channel": "not_connected", "content": row.content, "notice": "Brouillon préparatoire. Aucun dépôt TRACFIN n'est effectué."}


def annotation_create(request, principal, db, params, body):
    text = body.get("text") or body.get("annotation") or ""
    row = add_doc(db, principal.tenant_id, "annotation", params["objectId"], {"table": params["tableName"], "object_id": params["objectId"], "text": text}, parent_id=params["objectId"])
    return {"id": row.id, "text": text, "object_id": params["objectId"], "table_name": params["tableName"], "created_at": iso(row.created_at)}


def annotations_for_object(request, principal, db, params, body):
    rows = [row for row in list_docs(db, principal.tenant_id, "annotation") if row.payload.get("object_id") == params["objectId"] and row.payload.get("table") == params["tableName"]]
    return [{"id": row.id, "text": row.payload.get("text"), "created_at": iso(row.created_at)} for row in rows]


def get_annotation(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "annotation", params["annotationId"])
    if not row:
        raise marble_error(404, "not_found", "annotation not found")
    return {"id": row.id, **row.payload, "created_at": iso(row.created_at)}


def delete_annotation(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "annotation", params["annotationId"])
    if not row:
        raise marble_error(404, "not_found", "annotation not found")
    db.delete(row)
    return {"id": params["annotationId"]}


def download_annotation_file(request, principal, db, params, body):
    row = db.get(Evidence, params["fileId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "file not found")
    return Response(content=Path(row.storage_path).read_bytes(), media_type="application/octet-stream")


def ingested_object(request, principal, db, params, body):
    row = db.scalar(select(IngestedObject).where(IngestedObject.tenant_id == principal.tenant_id, IngestedObject.object_type == params["tableName"], IngestedObject.object_id == params["objectId"]).order_by(IngestedObject.updated_at.desc()))
    if not row:
        raise marble_error(404, "not_found", "object not found")
    return row.payload


def list_client_objects(request, principal, db, params, body):
    rows = db.scalars(select(IngestedObject).where(IngestedObject.tenant_id == principal.tenant_id, IngestedObject.object_type == params["tableName"]).limit(100)).all()
    return _page([row.payload for row in rows])


def cases_for_object(request, principal, db, params, body):
    rows = db.scalars(select(CaseRecord).where(CaseRecord.tenant_id == principal.tenant_id, CaseRecord.customer_id == params["object_id"])).all()
    return _page([_case_dto(db, row) for row in rows])


def create_ingestion(request, principal, db, params, body):
    tenant = db.get(Tenant, principal.tenant_id)
    status = ingest_object(db, tenant, params["objectType"], body, True, False)
    return Response(status_code=status)


def upload_logs(request, principal, db, params, body):
    rows = list_docs(db, principal.tenant_id, "ingestion_log", params["objectType"])
    return [{"id": row.id, "status": row.status, "created_at": iso(row.created_at)} for row in rows]


def client360_tables(request, principal, db, params, body):
    return [{"name": name, "id": table["id"]} for name, table in data_model_dto(db, principal.tenant_id)["tables"].items()]


def client360_search(request, principal, db, params, body):
    query = str(body.get("query") or "").lower()
    rows = db.scalars(select(IngestedObject).where(IngestedObject.tenant_id == principal.tenant_id).limit(200)).all()
    hits = [row.payload for row in rows if query and query in json.dumps(row.payload).lower()]
    customers = db.scalars(select(Customer).where(Customer.tenant_id == principal.tenant_id)).all()
    hits.extend({"object_type": "customers", "object_id": row.id, "name": row.display_name} for row in customers if query and query in row.display_name.lower())
    return {"results": hits[:50]}


def aggregate_create(request, principal, db, params, body):
    row = add_doc(db, principal.tenant_id, "aggregate", body.get("name") or params["recordType"], {"record_type": params["recordType"], "name": body.get("name"), "expression": body.get("expression")}, status="active")
    return {"id": row.id, **row.payload}


def aggregate_update(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "aggregate", params["id"])
    if not row:
        raise marble_error(404, "not_found", "aggregate not found")
    row.payload = {**row.payload, **body}
    touch(row)
    return {"id": row.id, **row.payload}


def aggregate_delete(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "aggregate", params["id"])
    if row:
        db.delete(row)
    return {"id": params["id"]}


def aggregate_compute(request, principal, db, params, body):
    objects = db.scalars(select(IngestedObject).where(IngestedObject.tenant_id == principal.tenant_id, IngestedObject.object_type == params["recordType"])).all()
    related = [row for row in objects if str(row.payload.get("customer_id") or row.object_id) == params["customerId"]]
    amounts = [float(row.payload.get("amount") or 0) for row in related]
    return {"customer_id": params["customerId"], "count": len(related), "sum_amount": sum(amounts), "aggregates": [{"id": row.id, "name": row.name} for row in list_docs(db, principal.tenant_id, "aggregate") if row.payload.get("record_type") == params["recordType"]]}


def get_decision(request, principal, db, params, body):
    row = db.get(Decision, params["decisionId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "decision not found")
    scenario = db.get(Scenario, row.scenario_id)
    return {"id": row.id, "created_at": iso(row.created_at), "outcome": row.outcome, "pivot_values": [str(row.trigger_object.get("object_id") or "")], "review_status": row.review_status, "scenario": {"id": row.scenario_id, "name": scenario.name if scenario else "", "scenario_iteration_id": row.iteration_id, "version": int(row.scenario_version) if str(row.scenario_version).isdigit() else 0}, "score": row.score, "trigger_object": row.trigger_object, "trigger_object_type": row.trigger_object_type, "rules": row.rules}


def active_snoozes(request, principal, db, params, body):
    rows = db.scalars(select(RuleSnooze).where(RuleSnooze.tenant_id == principal.tenant_id, RuleSnooze.until > utcnow())).all()
    if "decisionId" in params:
        rows = [row for row in rows if row.decision_id == params["decisionId"]]
    return [{"id": row.id, "rule_id": row.rule_id, "pivot_value": row.pivot_value, "until": iso(row.until)} for row in rows]


def create_snooze(request, principal, db, params, body):
    decision = db.get(Decision, params["decisionId"])
    if not decision or decision.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "decision not found")
    if not body.get("rule_id"):
        raise marble_error(400, "invalid_payload", "rule_id is required")
    row = RuleSnooze(tenant_id=principal.tenant_id, decision_id=decision.id, rule_id=str(body["rule_id"]), pivot_value=str(body.get("pivot_value") or decision.trigger_object.get("object_id") or ""), until=utcnow() + timedelta(days=int(body.get("duration_days") or 7)))
    db.add(row)
    db.flush()
    return {"id": row.id, "until": iso(row.until)}


def get_snooze(request, principal, db, params, body):
    row = db.get(RuleSnooze, params["rule_snooze_id"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "snooze not found")
    return {"id": row.id, "rule_id": row.rule_id, "pivot_value": row.pivot_value, "until": iso(row.until)}


def list_screenings(request, principal, db, params, body):
    rows = db.scalars(select(ScreeningCheck).where(ScreeningCheck.tenant_id == principal.tenant_id).order_by(ScreeningCheck.created_at.desc()).limit(100)).all()
    return _page([{"id": row.id, "status": row.status, "match_count": row.match_count, "production": row.production, "created_at": iso(row.created_at)} for row in rows])


def datasets(request, principal, db, params, body):
    catalog = dataset_catalog()
    return {"datasets": catalog["datasets"], "live": catalog["live"], "provider": "opensanctions", "configured": bool(request.app.state.settings.opensanctions_api_key)}


def screening_filters(request, principal, db, params, body):
    return {"sections": [{"name": "topics", "values": ["sanction", "role.pep", "adverse_media"]}, {"name": "datasets", "values": [item["name"] for item in dataset_catalog()["datasets"][:20]]}]}


def search_screening(request, principal, db, params, body):
    tenant = db.get(Tenant, principal.tenant_id)
    query = body.get("query") or {"Person": {"name": body.get("name") or ""}}
    screening = persist_screening(db, tenant, query, rule_id=None, decision_id=None, customer_id=None, counterparty=body.get("name"))
    matches = db.scalars(select(ScreeningMatch).where(ScreeningMatch.screening_id == screening.id)).all()
    return {"id": screening.id, "status": screening.status, "production": screening.production, "matches": [{"id": row.id, "status": row.status, "score": row.score, "entity_id": row.entity_id, "payload": row.payload} for row in matches]}


def update_match(request, principal, db, params, body):
    row = db.get(ScreeningMatch, params.get("matchId") or params.get("id"))
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "match not found")
    if body.get("status") not in {"pending", "no_hit", "confirmed_hit", "skipped"}:
        raise marble_error(400, "invalid_payload", "status is invalid")
    row.status = body["status"]
    return {"id": row.id, "status": row.status}


def enrich_match(request, principal, db, params, body):
    row = db.get(ScreeningMatch, params["matchId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "match not found")
    text, provider, production = ("Fiche limitée au résultat de criblage. Aucune confirmation automatique.", "clarity-local-analyst", False)
    external = complete_ai("Résume cette correspondance sans la confirmer.", json.dumps(row.payload)[:4000])
    if external:
        text, provider, production = external, "configured-model", True
    return {"id": row.id, "summary": text, "provider": provider, "production": production}


def ai_suggestions(request, principal, db, params, body):
    screening = db.get(ScreeningCheck, params["screeningId"])
    if not screening or screening.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "screening not found")
    rows = list_docs(db, principal.tenant_id, "screening_ai", screening.id)
    return [{"id": row.id, **row.payload} for row in rows]


def enqueue_screening_ai(request, principal, db, params, body):
    screening = db.get(ScreeningCheck, params["screeningId"])
    if not screening or screening.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "screening not found")
    matches = db.scalars(select(ScreeningMatch).where(ScreeningMatch.screening_id == screening.id)).all()
    summary = complete_ai("Propose une revue humaine. Ne confirme aucune correspondance.", json.dumps([row.payload for row in matches])[:4000])
    row = add_doc(db, principal.tenant_id, "screening_ai", screening.id, {"status": "completed", "text": summary or "Aucune confirmation automatique. Les correspondances restent des candidats.", "provider": "configured-model" if summary else "clarity-local-analyst", "production": bool(summary)}, parent_id=screening.id, status="completed")
    return {"id": row.id, **row.payload}


def list_screening_files(request, principal, db, params, body):
    return [{"id": row.id, "file_name": row.payload.get("file_name")} for row in list_docs(db, principal.tenant_id, "screening_file", params["screeningId"])]


def upload_screening_file(request, principal, db, params, body):
    row = add_doc(db, principal.tenant_id, "screening_file", params["screeningId"], {"file_name": body.get("file_name") or "note.txt", "note": body.get("note") or ""}, parent_id=params["screeningId"])
    return {"id": row.id, **row.payload}


def download_screening_file(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "screening_file", params["fileId"])
    if not row:
        raise marble_error(404, "not_found", "file not found")
    return PlainTextResponse(row.payload.get("note") or "")


def freshness(request, principal, db, params, body):
    catalog = dataset_catalog()
    return {"provider": "opensanctions", "live": catalog["live"], "datasets": [{"name": item["name"], "fresh": catalog["live"]} for item in catalog["datasets"][:30]]}


def entity_detail(request, principal, db, params, body):
    from app.models import WatchlistEntry

    entry = db.get(WatchlistEntry, params["entityId"])
    if entry:
        return {"id": entry.id, "caption": entry.names[0] if entry.names else entry.id, "synthetic": True, "provider": "clarity-synthetic-watchlist", "properties": entry.properties}
    return {"id": params["entityId"], "caption": params["entityId"], "synthetic": False, "provider": "opensanctions", "properties": {"note": "Entité non présente dans le jeu local. Le détail complet exige une réponse OpenSanctions conservée."}}


def freeform(request, principal, db, params, body):
    return search_screening(request, principal, db, params, {"name": body.get("query") or body.get("name") or "", "query": {"Person": {"name": body.get("query") or body.get("name") or ""}}})


def list_kind(kind: str):
    def _list(request, principal, db, params, body):
        return [{"id": row.id, "name": row.name, **row.payload} for row in list_docs(db, principal.tenant_id, kind)]

    return _list


def save_kind(kind: str, name_field: str = "name"):
    def _save(request, principal, db, params, body):
        row = add_doc(db, principal.tenant_id, kind, str(body.get(name_field) or kind), body)
        return {"id": row.id, **row.payload}

    return _save


def delete_kind(kind: str, param: str):
    def _delete(request, principal, db, params, body):
        row = get_doc(db, principal.tenant_id, kind, params[param])
        if row:
            db.delete(row)
        return {"id": params[param]}

    return _delete


def get_custom_list(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "custom_list", params["customListId"])
    if not row:
        raise marble_error(404, "not_found", "list not found")
    values = [{"id": item.id, "value": item.payload.get("value")} for item in list_docs(db, principal.tenant_id, "custom_list_value", row.id)]
    return {"id": row.id, "name": row.name, "description": row.payload.get("description") or "", "values": values}


def update_custom_list(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "custom_list", params["customListId"])
    if not row:
        raise marble_error(404, "not_found", "list not found")
    if body.get("name"):
        row.name = str(body["name"])
    row.payload = {**row.payload, "description": body.get("description", row.payload.get("description"))}
    touch(row)
    return {"id": row.id, "name": row.name, "description": row.payload.get("description")}


def download_values(request, principal, db, params, body):
    rows = list_docs(db, principal.tenant_id, "custom_list_value", params["customListId"])
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "value"])
    for row in rows:
        writer.writerow([row.id, row.payload.get("value")])
    return PlainTextResponse(buffer.getvalue(), media_type="text/csv")


def delete_value(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "custom_list_value", params["customListValueId"])
    if row:
        db.delete(row)
    return {"id": params["customListValueId"]}


def ai_settings(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "ai_settings", principal.tenant_id)
    payload = row.payload if row else {"kyc_enrichment": {"enabled": False, "domain_filter": [], "model": None, "search_context_size": None, "custom_instructions": None}, "configured": ai_configured()}
    payload["configured"] = ai_configured()
    return payload


def upsert_ai(request, principal, db, params, body):
    enrichment = body.get("kyc_enrichment") or body
    if "domain_filter" not in enrichment or "enabled" not in enrichment:
        raise marble_error(400, "invalid_payload", "kyc_enrichment.enabled and domain_filter are required")
    row = get_doc(db, principal.tenant_id, "ai_settings", principal.tenant_id)
    payload = {"kyc_enrichment": enrichment, "configured": ai_configured()}
    if row:
        row.payload = payload
        touch(row)
    else:
        add_doc(db, principal.tenant_id, "ai_settings", "ai", payload, doc_id=principal.tenant_id)
    return payload


def ast_ai_description(request, principal, db, params, body):
    text = complete_ai("Décris cette formule de règle en français, sans l'exécuter.", json.dumps(body)[:4000])
    return {"description": text or "Formule enregistrée. Aucun modèle externe n'est configuré, la description automatique reste locale : comparaison ou criblage sur le déclencheur.", "production": bool(text), "provider": "configured-model" if text else "clarity-local-analyst"}


def generate_ast(request, principal, db, params, body):
    prompt = str(body.get("prompt") or body.get("description") or "")
    node = {"name": ">=", "children": [{"name": "Payload", "named_children": {"path": {"constant": "amount"}}}, {"constant": 10000}]}
    external = complete_ai("Réponds uniquement par un JSON AST Marble avec name, constant, children, named_children.", prompt)
    if external:
        try:
            node = json.loads(external)
        except json.JSONDecodeError:
            node = {"name": "AND", "children": [node], "comment": external[:500]}
    return {"node": node, "production": bool(external), "provider": "configured-model" if external else "clarity-local-analyst"}


def analytics(metric: str):
    def _run(request, principal, db, params, body):
        decisions = db.scalars(select(Decision).where(Decision.tenant_id == principal.tenant_id)).all()
        cases = db.scalars(select(CaseRecord).where(CaseRecord.tenant_id == principal.tenant_id)).all()
        by_outcome = Counter(row.outcome for row in decisions)
        by_status = Counter(row.status for row in cases)
        return {"metric": metric, "decisions": dict(by_outcome), "cases": dict(by_status), "total_decisions": len(decisions), "total_cases": len(cases)}

    return _run


def app_config(request, principal, db, params, body):
    settings = request.app.state.settings
    return {"version": "0.2.0", "providers": {"opensanctions": bool(settings.opensanctions_api_key), "veriff": bool(settings.veriff_api_key and settings.veriff_shared_secret), "ai": bool(settings.ai_api_key)}}


def org_export(request, principal, db, params, body):
    from app.catalog import studio_view

    return {"organization_id": principal.tenant_id, "studio": studio_view(db, principal.tenant_id)}


def org_import(request, principal, db, params, body):
    imported = []
    for item in body.get("custom_lists") or []:
        add_doc(db, principal.tenant_id, "custom_list", item.get("name") or "liste", {"description": item.get("description") or ""})
        imported.append(item.get("name"))
    return {"imported": imported, "mode": "merge"}


def archetypes(request, principal, db, params, body):
    return [{"id": "banque-retail", "name": "Banque de détail", "includes": ["transactions", "customers", "accounts", "kyc", "screening"]}]


def apply_archetype(request, principal, db, params, body):
    return {"applied": body.get("archetype_id") or "banque-retail", "organization_id": principal.tenant_id}


def onboard(request, principal, db, params, body):
    tenant = db.get(Tenant, principal.tenant_id)
    if tenant and body.get("name"):
        tenant.name = str(body["name"])
    return {"organization_id": principal.tenant_id, "name": tenant.name if tenant else ""}


def delete_api_key(request, principal, db, params, body):
    row = db.get(ApiCredential, params["apiKeyId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "api key not found")
    db.delete(row)
    return {"id": params["apiKeyId"]}


def create_user(request, principal, db, params, body):
    if not body.get("email") or not body.get("role"):
        raise marble_error(400, "invalid_payload", "email and role are required")
    from app.security import hash_password

    row = User(tenant_id=principal.tenant_id, email=str(body["email"]), full_name=body.get("name") or body["email"], password_hash=hash_password(secrets.token_urlsafe(18)), role=str(body["role"]), active=True)
    db.add(row)
    db.flush()
    return {"id": row.id, "email": row.email, "name": row.full_name, "role": row.role}


def get_user(request, principal, db, params, body):
    row = db.get(User, params["userId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "user not found")
    return {"id": row.id, "email": row.email, "name": row.full_name, "role": row.role, "active": row.active}


def update_user(request, principal, db, params, body):
    row = db.get(User, params["userId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "user not found")
    if body.get("role"):
        row.role = str(body["role"])
    if "active" in body:
        row.active = bool(body["active"])
    if body.get("name"):
        row.full_name = str(body["name"])
    return get_user(request, principal, db, params, body)


def delete_user(request, principal, db, params, body):
    row = db.get(User, params["userId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "user not found")
    row.active = False
    return {"id": row.id, "active": False}


def user_grants(request, principal, db, params, body):
    row = db.get(User, params["userId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "user not found")
    return [{"organization_id": row.tenant_id, "role": row.role}]


def list_tenants(request, principal, db, params, body):
    return list_orgs(request, principal, db, params, body)


def update_tenant(request, principal, db, params, body):
    if params["tenantId"] != principal.tenant_id:
        raise marble_error(404, "not_found", "organization not found")
    return onboard(request, principal, db, params, body)


def merge_tenants(request, principal, db, params, body):
    raise marble_error(422, "unprocessable_entity", "merging organizations is refused in this deployment")


def replace_grant(request, principal, db, params, body):
    return update_user(request, principal, db, {"userId": params["userId"]}, {"role": body.get("role") or "analyst"})


def revoke_grant(request, principal, db, params, body):
    return delete_user(request, principal, db, {"userId": params["userId"]}, body)


def audit_events(request, principal, db, params, body):
    rows = db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == principal.tenant_id).order_by(AuditEvent.created_at.desc()).limit(200)).all()
    return _page([{"id": row.id, "actor": row.actor, "action": row.action, "entity_type": row.entity_type, "entity_id": row.entity_id, "created_at": iso(row.created_at)} for row in rows])


def patch_table(request, principal, db, params, body):
    tables = data_model_dto(db, principal.tenant_id)["tables"]
    table = next((item for item in tables.values() if item["id"] == params["tableId"]), None)
    if not table:
        raise marble_error(404, "not_found", "table not found")
    meta = get_doc(db, principal.tenant_id, "table_meta", table["id"])
    if meta:
        meta.payload = {**meta.payload, "description": body.get("description", meta.payload.get("description"))}
        touch(meta)
    return table


def delete_table(request, principal, db, params, body):
    tables = data_model_dto(db, principal.tenant_id)["tables"]
    table = next((item for item in tables.values() if item["id"] == params["tableId"]), None)
    if not table:
        raise marble_error(404, "not_found", "table not found")
    for row in db.scalars(select(DataModelField).where(DataModelField.tenant_id == principal.tenant_id, DataModelField.object_type == table["name"])).all():
        db.delete(row)
    return {"id": params["tableId"]}


def patch_field(request, principal, db, params, body):
    row = db.get(DataModelField, params["fieldId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "field not found")
    if "nullable" in body:
        row.required = not bool(body["nullable"])
    if body.get("name"):
        row.name = str(body["name"])
    return {"id": row.id, "name": row.name, "nullable": not row.required}


def delete_field(request, principal, db, params, body):
    row = db.get(DataModelField, params["fieldId"])
    if row and row.tenant_id == principal.tenant_id and row.name not in {"object_id", "updated_at"}:
        db.delete(row)
    return {"id": params["fieldId"]}


def data_model_openapi(request, principal, db, params, body):
    tables = data_model_dto(db, principal.tenant_id)["tables"]
    paths = {f"/ingest/{name}": {"post": {"summary": f"Ingest {name}"}} for name in tables}
    return {"openapi": "3.0.3", "info": {"title": "Clarity data model", "version": params.get("version") or "current"}, "paths": paths}


def pivot_create(request, principal, db, params, body):
    if not body.get("table") or not body.get("field"):
        raise marble_error(400, "invalid_payload", "table and field are required")
    row = add_doc(db, principal.tenant_id, "pivot", f"{body['table']}.{body['field']}", body)
    return {"id": row.id, **body}


def link_create(request, principal, db, params, body):
    row = add_doc(db, principal.tenant_id, "data_link", body.get("name") or "link", body)
    return {"id": row.id, **body}


def navigation(request, principal, db, params, body):
    row = add_doc(db, principal.tenant_id, "navigation", params["tableId"], body, parent_id=params["tableId"])
    return {"id": row.id, **body}


def exported_fields(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "exported_fields", params["tableId"])
    return {"table_id": params["tableId"], "fields": (row.payload.get("fields") if row else ["object_id", "updated_at"])}


def update_exported(request, principal, db, params, body):
    fields = body.get("fields") or []
    row = get_doc(db, principal.tenant_id, "exported_fields", params["tableId"])
    if row:
        row.payload = {"fields": fields}
        touch(row)
    else:
        add_doc(db, principal.tenant_id, "exported_fields", params["tableId"], {"fields": fields}, doc_id=params["tableId"])
    return {"table_id": params["tableId"], "fields": fields}


def inbox_meta(request, principal, db, params, body):
    rows = db.scalars(select(Inbox).where(Inbox.tenant_id == principal.tenant_id)).all()
    return [{"id": row.id, "name": row.name, "cases": db.scalar(select(func.count()).select_from(CaseRecord).where(CaseRecord.inbox_id == row.id)) or 0} for row in rows]


def get_inbox(request, principal, db, params, body):
    row = db.get(Inbox, params["inboxId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "inbox not found")
    return {"id": row.id, "name": row.name}


def update_inbox(request, principal, db, params, body):
    row = db.get(Inbox, params["inboxId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "inbox not found")
    if body.get("name"):
        row.name = str(body["name"])
    return {"id": row.id, "name": row.name}


def delete_inbox(request, principal, db, params, body):
    row = db.get(Inbox, params["inboxId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "inbox not found")
    used = db.scalar(select(func.count()).select_from(CaseRecord).where(CaseRecord.inbox_id == row.id)) or 0
    if used:
        raise marble_error(422, "unprocessable_entity", "inbox still contains cases")
    db.delete(row)
    return {"id": params["inboxId"]}


def sla(request, principal, db, params, body):
    add_doc(db, principal.tenant_id, "inbox_sla", "sla", body or {"hours": 72})
    return body or {"hours": 72}


def inbox_users(request, principal, db, params, body):
    parent = params.get("inboxId")
    rows = list_docs(db, principal.tenant_id, "inbox_user", parent) if parent else list_docs(db, principal.tenant_id, "inbox_user")
    return [{"id": row.id, **row.payload} for row in rows]


def add_inbox_user(request, principal, db, params, body):
    if not body.get("user_id"):
        raise marble_error(400, "invalid_payload", "user_id is required")
    row = add_doc(db, principal.tenant_id, "inbox_user", body["user_id"], {"user_id": body["user_id"], "inbox_id": params["inboxId"], "role": body.get("role") or "analyst"}, parent_id=params["inboxId"])
    return {"id": row.id, **row.payload}


def update_tag(request, principal, db, params, body):
    row = db.get(Tag, params["tagId"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "tag not found")
    if body.get("name"):
        row.name = str(body["name"])
    return {"id": row.id, "name": row.name, "target": row.target}


def delete_tag(request, principal, db, params, body):
    row = db.get(Tag, params["tagId"])
    if row and row.tenant_id == principal.tenant_id:
        db.delete(row)
    return {"id": params["tagId"]}


def webhook_one(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "webhook", params["webhookId"])
    if not row:
        raise marble_error(404, "not_found", "webhook not found")
    return {"id": row.id, "name": row.name, **{key: value for key, value in row.payload.items() if "secret" not in key}}


def update_webhook(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "webhook", params["webhookId"])
    if not row:
        raise marble_error(404, "not_found", "webhook not found")
    row.payload = {**row.payload, **{key: value for key, value in body.items() if key != "secret"}}
    touch(row)
    return webhook_one(request, principal, db, params, body)


def delete_webhook(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "webhook", params["webhookId"])
    if row:
        db.delete(row)
    return {"id": params["webhookId"]}


def webhook_secret(request, principal, db, params, body):
    secret = "whsec_" + secrets.token_urlsafe(18)
    row = add_doc(db, principal.tenant_id, "webhook_secret", "secret", {"webhook_id": params["webhookId"], "hash": hash_token(secret)}, parent_id=params["webhookId"])
    return {"id": row.id, "secret": secret}


def revoke_secret(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "webhook_secret", params["secretId"])
    if row:
        db.delete(row)
    return {"id": params["secretId"]}


def scenario_workflows(request, principal, db, params, body):
    return [{"id": row.id, "name": row.name, **row.payload} for row in list_docs(db, principal.tenant_id, "scenario_workflow", params["scenarioId"])]


def reorder_workflows(request, principal, db, params, body):
    order = body.get("rule_ids") or []
    for index, rule_id in enumerate(order):
        row = get_doc(db, principal.tenant_id, "scenario_workflow_rule", str(rule_id))
        if row:
            row.payload = {**row.payload, "display_order": index}
            touch(row)
    return {"rule_ids": order}


def create_workflow_rule(request, principal, db, params, body):
    row = add_doc(db, principal.tenant_id, "scenario_workflow_rule", body.get("name") or "rule", body)
    return {"id": row.id, **body}


def get_workflow_rule(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "scenario_workflow_rule", params["ruleId"])
    if not row:
        raise marble_error(404, "not_found", "rule not found")
    return {"id": row.id, "name": row.name, **row.payload}


def update_workflow_rule(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "scenario_workflow_rule", params["ruleId"])
    if not row:
        raise marble_error(404, "not_found", "rule not found")
    row.payload = {**row.payload, **body}
    if body.get("name"):
        row.name = str(body["name"])
    touch(row)
    return {"id": row.id, "name": row.name, **row.payload}


def child_create(kind: str, parent_param: str):
    def _create(request, principal, db, params, body):
        row = add_doc(db, principal.tenant_id, kind, body.get("name") or kind, body, parent_id=params[parent_param])
        return {"id": row.id, **body}

    return _create


def child_update(kind: str, param: str):
    def _update(request, principal, db, params, body):
        row = get_doc(db, principal.tenant_id, kind, params[param])
        if not row:
            raise marble_error(404, "not_found", "not found")
        row.payload = {**row.payload, **body}
        touch(row)
        return {"id": row.id, **row.payload}

    return _update


def test_run_get(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "scenario_test_run", params["test_run_id"])
    if not row:
        raise marble_error(404, "not_found", "test run not found")
    return {"scenario_test_run": {"id": row.id, **{key: row.payload[key] for key in ("scenario_id", "ref_iteration_id", "test_iteration_id", "start_date", "end_date", "creator_id", "status")}}}


def cancel_test(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "scenario_test_run", params["test_run_id"])
    if not row:
        raise marble_error(404, "not_found", "test run not found")
    row.status = "canceled"
    row.payload = {**row.payload, "status": "canceled"}
    return {"id": row.id, "status": "canceled"}


def test_scores(request, principal, db, params, body):
    return {"test_run_id": params["test_run_id"], "buckets": []}


def iteration_metadata(request, principal, db, params, body):
    return [{"id": row.id, "scenario_id": row.payload.get("scenario_id"), "version": row.payload.get("version"), "status": row.status} for row in list_docs(db, principal.tenant_id, "scenario_iteration")]


def rule_metadata(request, principal, db, params, body):
    return [{"id": row.id, "name": row.name, "scenario_iteration_id": row.payload.get("scenario_iteration_id")} for row in list_docs(db, principal.tenant_id, "scenario_rule")]


def get_rule(request, principal, db, params, body):
    from app.catalog import rule_dto

    row = get_doc(db, principal.tenant_id, "scenario_rule", params["ruleId"])
    if not row:
        raise marble_error(404, "not_found", "rule not found")
    return rule_dto(row)


def draft_from_iteration(request, principal, db, params, body):
    source = get_doc(db, principal.tenant_id, "scenario_iteration", params["scenarioIterationId"])
    if not source:
        raise marble_error(404, "not_found", "iteration not found")
    row = add_doc(db, principal.tenant_id, "scenario_iteration", "draft", {**source.payload, "version": None, "archived": False}, parent_id=source.parent_id, status="draft")
    return {"id": row.id, "scenario_id": row.payload.get("scenario_id"), "version": None, "created_at": iso(row.created_at), "updated_at": iso(row.updated_at), "archived": False, "body": row.payload.get("body")}


def screening_config(request, principal, db, params, body):
    row = add_doc(db, principal.tenant_id, "iteration_screening", body.get("name") or "screening", {**body, "production": False, "dataset": "opensanctions" if request.app.state.settings.opensanctions_api_key else "clarity-synthetic-watchlist"}, parent_id=params["scenarioIterationId"])
    return {"id": row.id, **row.payload}


def upsert_screening_config(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "iteration_screening", params["screeningConfigId"])
    if not row:
        raise marble_error(404, "not_found", "screening config not found")
    row.payload = {**row.payload, **body}
    touch(row)
    return {"id": row.id, **row.payload}


def publication_prepare(request, principal, db, params, body):
    return {"status": "ready", "errors": []}


def publication_start(request, principal, db, params, body):
    return {"status": "ready"}


def publication_get(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "scenario_publication", params["scenarioPublicationId"])
    if not row:
        raise marble_error(404, "not_found", "publication not found")
    return {"id": row.id, **row.payload, "created_at": iso(row.created_at)}


def publication_create(request, principal, db, params, body):
    scenario = db.get(Scenario, str(body.get("scenario_id") or ""))
    if not scenario or scenario.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "scenario not found")
    row = add_doc(db, principal.tenant_id, "scenario_publication", scenario.name, {"scenario_id": scenario.id, "scenario_iteration_id": body.get("scenario_iteration_id") or scenario.iteration_id, "status": "published"}, parent_id=scenario.id, status="published")
    return {"id": row.id, **row.payload}


def continuous_get(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "continuous_screening", params["stable_id"])
    if not row:
        raise marble_error(404, "not_found", "config not found")
    return {"id": row.id, **row.payload}


def continuous_update(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "continuous_screening", params["stable_id"])
    if not row:
        raise marble_error(404, "not_found", "config not found")
    row.payload = {**row.payload, **body, "production": bool(request.app.state.settings.opensanctions_api_key)}
    touch(row)
    return {"id": row.id, **row.payload}


def continuous_objects(request, principal, db, params, body):
    return [{"id": row.id, **row.payload} for row in list_docs(db, principal.tenant_id, "continuous_object")]


def continuous_object_create(request, principal, db, params, body):
    if not body.get("object_id") or not body.get("object_type"):
        raise marble_error(400, "invalid_payload", "object_type and object_id are required")
    tenant = db.get(Tenant, principal.tenant_id)
    screening = persist_screening(db, tenant, {"Person": {"name": body.get("name") or body["object_id"]}}, rule_id=None, decision_id=None, customer_id=None, counterparty=body["object_id"])
    row = add_doc(db, principal.tenant_id, "continuous_object", body["object_id"], {**body, "screening_id": screening.id, "production": screening.production})
    return {"id": row.id, "screening_id": screening.id, "production": screening.production}


def continuous_object_delete(request, principal, db, params, body):
    for row in list_docs(db, principal.tenant_id, "continuous_object"):
        if row.payload.get("object_id") == body.get("object_id"):
            db.delete(row)
    return {"deleted": True}


def continuous_lists(kind: str):
    def _list(request, principal, db, params, body):
        if kind == "screenings":
            rows = db.scalars(select(ScreeningCheck).where(ScreeningCheck.tenant_id == principal.tenant_id).limit(50)).all()
            return [{"id": row.id, "status": row.status, "production": row.production} for row in rows]
        return []

    return _list


def dismiss_continuous(request, principal, db, params, body):
    row = db.get(ScreeningCheck, params["id"])
    if row and row.tenant_id == principal.tenant_id:
        row.status = "no_hit"
    return {"id": params["id"], "status": "no_hit"}


def load_more(request, principal, db, params, body):
    row = db.get(ScreeningCheck, params["id"])
    if not row or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "screening not found")
    matches = db.scalars(select(ScreeningMatch).where(ScreeningMatch.screening_id == row.id)).all()
    return {"id": row.id, "matches": [{"id": item.id, "status": item.status, "score": item.score} for item in matches]}


def scoring_settings(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "scoring_settings", principal.tenant_id)
    return row.payload if row else {"levels": [{"level": 1, "name": "low"}, {"level": 2, "name": "medium"}, {"level": 4, "name": "high"}]}


def scoring_settings_update(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "scoring_settings", principal.tenant_id)
    if row:
        row.payload = body
        touch(row)
    else:
        add_doc(db, principal.tenant_id, "scoring_settings", "scoring", body, doc_id=principal.tenant_id)
    return body


def scoring_one(request, principal, db, params, body):
    rows = [row for row in list_docs(db, principal.tenant_id, "scoring_ruleset") if row.payload.get("record_type") == params["recordType"] and row.status == "live"]
    if not rows:
        raise marble_error(404, "not_found", "ruleset not found")
    return {"id": rows[-1].id, **rows[-1].payload}


def scoring_update(request, principal, db, params, body):
    row = add_doc(db, principal.tenant_id, "scoring_ruleset", params["recordType"], {"record_type": params["recordType"], "factors": body.get("factors") or [], "status": "draft"}, status="draft")
    return {"id": row.id, **row.payload}


def scoring_versions(request, principal, db, params, body):
    rows = [row for row in list_docs(db, principal.tenant_id, "scoring_ruleset") if row.payload.get("record_type") == params["recordType"]]
    return [{"id": row.id, "version": row.payload.get("version"), "status": row.status} for row in rows]


def score_latest(request, principal, db, params, body):
    row = db.scalar(select(RiskAssessment).where(RiskAssessment.tenant_id == principal.tenant_id, RiskAssessment.object_type == params["recordType"], RiskAssessment.object_id == params["recordId"], RiskAssessment.current.is_(True)))
    if not row:
        raise marble_error(404, "not_found", "score not found")
    return {"score": row.score, "risk_level": row.risk_level, "tier": row.tier, "factors": row.factors, "source": row.source, "created_at": iso(row.created_at)}


def score_override(request, principal, db, params, body):
    if not isinstance(body.get("risk_level"), int):
        raise marble_error(400, "invalid_payload", "risk_level is required")
    current = db.scalar(select(RiskAssessment).where(RiskAssessment.tenant_id == principal.tenant_id, RiskAssessment.object_type == params["recordType"], RiskAssessment.object_id == params["recordId"], RiskAssessment.current.is_(True)))
    if current:
        current.current = False
    row = RiskAssessment(tenant_id=principal.tenant_id, object_type=params["recordType"], object_id=params["recordId"], source="override", score=current.score if current else 0, risk_level=body["risk_level"], tier="override", factors=(current.factors if current else []) + [{"code": "manual_override", "explanation": body.get("rationale") or "Niveau imposé."}], current=True, rationale=body.get("rationale"), overridden_by=principal.user_id)
    db.add(row)
    db.flush()
    return {"id": row.id, "risk_level": row.risk_level, "source": "override"}


def score_history(request, principal, db, params, body):
    rows = db.scalars(select(RiskAssessment).where(RiskAssessment.tenant_id == principal.tenant_id, RiskAssessment.object_type == params["recordType"], RiskAssessment.object_id == params["recordId"])).all()
    return [{"id": row.id, "score": row.score, "risk_level": row.risk_level, "source": row.source, "current": row.current, "created_at": iso(row.created_at)} for row in rows]


def score_distribution(request, principal, db, params, body):
    rows = db.scalars(select(RiskAssessment).where(RiskAssessment.tenant_id == principal.tenant_id, RiskAssessment.object_type == params["recordType"], RiskAssessment.current.is_(True))).all()
    return {"record_type": params["recordType"], "tiers": dict(Counter(row.tier for row in rows))}


def graph_list(request, principal, db, params, body):
    return [{"id": row.id, **row.payload} for row in list_docs(db, principal.tenant_id, "graph_relation")]


def graph_create(request, principal, db, params, body):
    row = add_doc(db, principal.tenant_id, "graph_relation", body.get("name") or "relation", body)
    return {"id": row.id, **body}


def graph_generate(request, principal, db, params, body):
    customer = db.get(Customer, params["recordId"])
    nodes = [{"id": params["recordId"], "type": params["recordType"], "label": customer.display_name if customer else params["recordId"]}]
    edges = []
    if customer and customer.kind == "business":
        from app.models import Party

        for party in db.scalars(select(Party).where(Party.business_customer_id == customer.id)).all():
            nodes.append({"id": party.id, "type": party.role, "label": party.full_name})
            edges.append({"source": customer.id, "target": party.id, "kind": party.role})
    return {"nodes": nodes, "edges": edges}


def unavailability(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "unavailability", principal.user_id or principal.tenant_id)
    if not row:
        return {"until": None}
    return row.payload


def set_unavailability(request, principal, db, params, body):
    key = principal.user_id or principal.tenant_id
    row = get_doc(db, principal.tenant_id, "unavailability", key)
    if row:
        row.payload = body
        touch(row)
    else:
        add_doc(db, principal.tenant_id, "unavailability", "away", body, doc_id=key)
    return body


def cancel_unavailability(request, principal, db, params, body):
    row = get_doc(db, principal.tenant_id, "unavailability", principal.user_id or principal.tenant_id)
    if row:
        db.delete(row)
    return {"until": None}


def legacy_analytics(request, principal, db, params, body):
    return analytics("legacy")(request, principal, db, params, body)


def _stored(operation_id: str, request, principal, db, params, body):
    kind = "console_" + operation_id
    if request.method == "GET":
        return [{"id": row.id, **row.payload} for row in list_docs(db, principal.tenant_id, kind)]
    if request.method == "DELETE":
        for value in params.values():
            row = get_doc(db, principal.tenant_id, kind, value)
            if row:
                db.delete(row)
        return {"deleted": True}
    row = add_doc(db, principal.tenant_id, kind, operation_id, {"params": params, "body": body})
    return {"id": row.id, "operation": operation_id, "params": params}


HANDLERS = {
    "postToken": post_token,
    "getCredentials": get_credentials,
    "listMyOrganizations": list_orgs,
    "listCases": list_cases,
    "createCase": create_case,
    "getCase": get_case,
    "updateCase": update_case,
    "assignUser": assign_user,
    "unassignUser": unassign_user,
    "getPaginatedCaseDecisions": case_decisions,
    "addDecisionsToCase": add_decisions,
    "addEntitiesToCase": add_entities,
    "removeEntitiesFromCase": remove_entities,
    "addCommentToCase": add_comment,
    "snoozeCase": snooze_case,
    "unsnoozeCase": unsnooze_case,
    "updateTagsForCase": update_case_tags,
    "downloadCaseFile": download_case_file,
    "reviewDecision": review_decision,
    "getPivotObjectsForCase": pivot_objects,
    "getPivotRelatedCases": related_by_pivot,
    "getObjectRelatedCases": related_by_object,
    "getNextCase": next_case,
    "escalateCase": escalate_case,
    "getMostRecentCaseReview": latest_review,
    "enqueueReviewForCase": enqueue_review,
    "getCaseReviewById": get_review,
    "addOrUpdateCaseReviewFeedback": review_feedback,
    "listCaseReviews": list_reviews,
    "downloadCaseData": investigation_data,
    "enrichPivotObjectOfCaseWithKyc": enrich_kyc,
    "massUpdateCases": mass_update,
    "sarList": sar_list,
    "sarCreate": sar_create,
    "sarUpdate": sar_update,
    "sarDelete": sar_delete,
    "sarDownload": sar_download,
    "createAnnotation": annotation_create,
    "getAnnotationsByTableNameAndObjectId": annotations_for_object,
    "getAnnotation": get_annotation,
    "deleteAnnotation": delete_annotation,
    "downloadAnnotationFile": download_annotation_file,
    "getIngestedObject": ingested_object,
    "listClientObjects": list_client_objects,
    "getCasesForObject": cases_for_object,
    "createIngestion": create_ingestion,
    "getIngestionUploadLogs": upload_logs,
    "getClient360Tables": client360_tables,
    "searchClient360": client360_search,
    "createCustomerAggregate": aggregate_create,
    "updateCustomerAggregate": aggregate_update,
    "deleteCustomerAggregate": aggregate_delete,
    "listCustomerAggregates": aggregate_compute,
    "getDecision": get_decision,
    "getDecisionActiveSnoozes": active_snoozes,
    "getScenarioIterationActiveSnoozes": active_snoozes,
    "createSnoozeForDecision": create_snooze,
    "getRuleSnooze": get_snooze,
    "listScreenings": list_screenings,
    "listOpenSanctionDatasets": datasets,
    "listScreeningAvailableFilters": screening_filters,
    "searchScreeningMatches": search_screening,
    "refineScreening": search_screening,
    "freeformSearch": freeform,
    "listFreeformSearches": list_kind("freeform_search"),
    "getFreeformSearch": lambda request, principal, db, params, body: {"id": params["id"], **(get_doc(db, principal.tenant_id, "freeform_search", params["id"]).payload if get_doc(db, principal.tenant_id, "freeform_search", params["id"]) else {})},
    "saveFreeformSearch": save_kind("freeform_search"),
    "listScreeningSavedSearches": list_kind("saved_search"),
    "saveScreeningSearch": save_kind("saved_search"),
    "deleteScreeningSavedSearch": delete_kind("saved_search", "id"),
    "updateScreeningMatch": update_match,
    "updateContinuousScreeningMatch": update_match,
    "enrichScreeningMatch": enrich_match,
    "getScreeningAiSuggestions": ai_suggestions,
    "enqueueScreeningAiSuggestions": enqueue_screening_ai,
    "listScreeningFiles": list_screening_files,
    "uploadScreeningFile": upload_screening_file,
    "downloadScreeningFile": download_screening_file,
    "getDatasetsFreshness": freshness,
    "getEnrichedData": entity_detail,
    "getCustomList": get_custom_list,
    "updateCustomList": update_custom_list,
    "deleteCustomList": delete_kind("custom_list", "customListId"),
    "downloadListValuesAsCsvFile": download_values,
    "deleteCustomListValue": delete_value,
    "getAISettings": ai_settings,
    "upsertAISettings": upsert_ai,
    "GenerateAiDescriptionForAstExpression": ast_ai_description,
    "generateRuleAst": generate_ast,
    "getAppConfig": app_config,
    "exportOrganization": org_export,
    "importOrganization": org_import,
    "importOrganizationFromFile": org_import,
    "listArchetypes": archetypes,
    "applyArchetype": apply_archetype,
    "createInitialOrg": onboard,
    "deleteApiKey": delete_api_key,
    "createUser": create_user,
    "getUser": get_user,
    "updateUser": update_user,
    "deleteUser": delete_user,
    "listUserGrants": user_grants,
    "listTenants": list_tenants,
    "updateTenant": update_tenant,
    "mergeTenants": merge_tenants,
    "replaceOrganizationGrant": replace_grant,
    "revokeOrganizationGrant": revoke_grant,
    "listOrganizations": list_orgs,
    "createOrganization": onboard,
    "getOrganization": list_orgs,
    "updateOrganization": lambda request, principal, db, params, body: update_tenant(request, principal, db, {"tenantId": params.get("organizationId") or principal.tenant_id}, body),
    "deleteOrganization": merge_tenants,
    "updateOrganizationSubnets": lambda request, principal, db, params, body: add_doc(db, principal.tenant_id, "subnets", "subnets", body).payload,
    "listOrganizationUsers": lambda request, principal, db, params, body: [{"id": row.id, "email": row.email, "role": row.role} for row in db.scalars(select(User).where(User.tenant_id == principal.tenant_id)).all()],
    "listAuditEvents": audit_events,
    "patchDataModelTable": patch_table,
    "deleteDataModelTable": delete_table,
    "patchDataModelField": patch_field,
    "deleteDataModelField": delete_field,
    "postDataModelTableLink": link_create,
    "deleteDataModelLink": delete_kind("data_link", "linkId"),
    "getDataModelOpenApi": data_model_openapi,
    "getDataModelOpenApiOfVersion": data_model_openapi,
    "listDataModelPivots": list_kind("pivot"),
    "createDataModelPivot": pivot_create,
    "deleteDataModelPivot": delete_kind("pivot", "pivotId"),
    "postDataModelTableNavigationOption": navigation,
    "getDataModelTableExportedFields": exported_fields,
    "updateDataModelTableExportedFields": update_exported,
    "listInboxesMetadata": inbox_meta,
    "getInbox": get_inbox,
    "updateInbox": update_inbox,
    "deleteInbox": delete_inbox,
    "putInboxesSla": sla,
    "getInboxMetadata": inbox_meta,
    "listInboxUsers": inbox_users,
    "addInboxUser": add_inbox_user,
    "listAllInboxUsers": inbox_users,
    "getInboxUser": lambda request, principal, db, params, body: {"id": params["inboxUserId"], **(get_doc(db, principal.tenant_id, "inbox_user", params["inboxUserId"]).payload if get_doc(db, principal.tenant_id, "inbox_user", params["inboxUserId"]) else {})},
    "updateInboxUser": child_update("inbox_user", "inboxUserId"),
    "deleteInboxUser": delete_kind("inbox_user", "inboxUserId"),
    "updateTag": update_tag,
    "deleteTag": delete_tag,
    "getWebhook": webhook_one,
    "updateWebhook": update_webhook,
    "deleteWebhook": delete_webhook,
    "createWebhookSecret": webhook_secret,
    "revokeWebhookSecret": revoke_secret,
    "listWorkflows": scenario_workflows,
    "reorderWorkflows": reorder_workflows,
    "createWorkflowRule": create_workflow_rule,
    "getWorkflowRule": get_workflow_rule,
    "updateWorkflowRule": update_workflow_rule,
    "deleteWorkflowRule": delete_kind("scenario_workflow_rule", "ruleId"),
    "createWorkflowCondition": child_create("workflow_condition", "ruleId"),
    "updateWorkflowCondition": child_update("workflow_condition", "conditionId"),
    "deleteWorkflowCondition": delete_kind("workflow_condition", "conditionId"),
    "createWorkflowAction": child_create("workflow_action", "ruleId"),
    "updateWorkflowAction": child_update("workflow_action", "actionId"),
    "deleteWorkflowAction": delete_kind("workflow_action", "actionId"),
    "getTestRun": test_run_get,
    "cancelTestRun": cancel_test,
    "getDecisionData": test_scores,
    "getRuleData": test_scores,
    "listScenarioIterationsMetadata": iteration_metadata,
    "listScenarioIterationRulesMetadata": rule_metadata,
    "getScenarioIterationRule": get_rule,
    "createDraftFromScenarioIteration": draft_from_iteration,
    "createScreeningConfig": screening_config,
    "upsertScreeningConfig": upsert_screening_config,
    "deleteScreeningConfig": delete_kind("iteration_screening", "screeningConfigId"),
    "getScenarioPublicationPreparationStatus": publication_prepare,
    "startScenarioPublicationPreparation": publication_start,
    "getScenarioPublication": publication_get,
    "createScenarioPublication": publication_create,
    "getContinuousScreeningConfig": continuous_get,
    "updateContinuousScreeningConfig": continuous_update,
    "listContinuousScreeningObjects": continuous_objects,
    "createContinuousScreeningObject": continuous_object_create,
    "deleteContinuousScreeningObject": continuous_object_delete,
    "listContinuousScreeningDatasetUpdates": lambda request, principal, db, params, body: [],
    "listContinuousScreeningUpdateJobs": lambda request, principal, db, params, body: [],
    "listContinuousScreeningClientDataIndexing": lambda request, principal, db, params, body: [],
    "listContinuousScreenings": continuous_lists("screenings"),
    "dismissContinuousScreening": dismiss_continuous,
    "loadMoreContinuousScreeningMatches": load_more,
    "getScoringSettings": scoring_settings,
    "updateScoringSettings": scoring_settings_update,
    "getScoringRuleset": scoring_one,
    "updateScoringRuleset": scoring_update,
    "listScoringRulesetVersions": scoring_versions,
    "getScoringRulesetPreparationStatus": lambda request, principal, db, params, body: {"status": "ready"},
    "getScoringDryRun": lambda request, principal, db, params, body: {"status": "idle"},
    "getScoreLatest": score_latest,
    "overrideScoringScore": score_override,
    "getScoreHistory": score_history,
    "getScoreDistribution": score_distribution,
    "listGraphRelations": graph_list,
    "createGraphRelation": graph_create,
    "deleteGraphRelation": delete_kind("graph_relation", "relationId"),
    "generateRelationshipGraph": graph_generate,
    "getUnavailability": unavailability,
    "setUnavailability": set_unavailability,
    "cancelUnavailability": cancel_unavailability,
    "legacyListAnalytics": legacy_analytics,
    "getAvailableFilters": lambda request, principal, db, params, body: {"filters": ["outcome", "status", "inbox_id"]},
}
for _metric in (
    "getDecisionOutcomesPerDay",
    "getRuleHitTable",
    "getScreeningHits",
    "getDecisionsScoreDistribution",
    "getRuleVsDecisionOutcome",
    "getCaseStatusByDate",
    "getCaseStatusByInbox",
    "getCasesAnalyticsSarCompleted",
    "getCasesAnalyticsSarDelay",
    "getCasesAnalyticsSarDelayDistribution",
    "getCasesAnalyticsCasesCreated",
    "getCasesAnalyticsFalsePositiveRate",
    "getCasesAnalyticsCasesDuration",
    "getCasesAnalyticsOpenCasesByAge",
    "getCaseSlaStatusByDate",
):
    HANDLERS[_metric] = analytics(_metric)
