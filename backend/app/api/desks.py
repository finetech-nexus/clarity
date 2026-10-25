"""Case desks for the KYC review and the AML investigation."""

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import list_docs
from app.config import ROOT
from app.deps import clarity_principal, db_session
from app.models import (
    Alert,
    AuditEvent,
    CaseComment,
    CaseRecord,
    Customer,
    Decision,
    Evidence,
    IdentityDocument,
    Inbox,
    RegulatoryReport,
    ScreeningCheck,
    ScreeningMatch,
    Tag,
    Tenant,
    Transaction,
    User,
    WorkflowInstance,
    WorkflowTask,
)
from app.search import present
from app.security import Principal
from app.services import (
    actor_label,
    advance_workflow,
    audit,
    case_payload,
    clarity_error,
    customer_view,
    document_record,
    store_bytes,
)
from app.timeutil import iso, utcnow

router = APIRouter(prefix="/api/clarity/v1/desks", tags=["desks"])


def _tenant(db: Session, principal: Principal) -> Tenant:
    tenant = db.get(Tenant, principal.tenant_id)
    if not tenant:
        raise clarity_error(404, "not_found", "Organisation introuvable.")
    return tenant


def _workflow(db: Session, principal: Principal, runtime_id: str) -> WorkflowInstance:
    row = db.scalar(
        select(WorkflowInstance).where(
            WorkflowInstance.tenant_id == principal.tenant_id,
            WorkflowInstance.ballerine_runtime_id == runtime_id,
        )
    )
    if not row:
        row = db.get(WorkflowInstance, runtime_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Dossier de vérification introuvable.")
    return row


def _case(db: Session, principal: Principal, case_id: str) -> CaseRecord:
    row = db.get(CaseRecord, case_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Dossier introuvable.")
    return row


def _document_view(document: IdentityDocument) -> dict:
    payload = document_record(document)
    title = (document.properties or {}).get("title") or document.doc_type
    payload.update(
        {
            "title": title,
            "fileName": (document.properties or {}).get("fileName") or f"{document.doc_type}.bin",
            "mime": (document.properties or {}).get("mime") or "application/octet-stream",
            "synthetic": bool((document.properties or {}).get("synthetic")),
            "url": f"/api/clarity/v1/desks/documents/{document.id}/content",
        }
    )
    return payload


def _kyc_card(db: Session, workflow: WorkflowInstance) -> dict:
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    documents = db.scalars(select(IdentityDocument).where(IdentityDocument.workflow_id == workflow.id)).all()
    task = db.scalar(select(WorkflowTask).where(WorkflowTask.workflow_id == workflow.id, WorkflowTask.status == "open"))
    return {
        "id": workflow.ballerine_runtime_id,
        "workflow_id": workflow.id,
        "name": customer.display_name if customer else workflow.definition_name,
        "definition": workflow.definition_name,
        "status": workflow.status,
        "state": workflow.state,
        "task": None if not task else {"key": task.task_key, "name": task.name},
        "documents": len(documents),
        "revisions": sum(1 for document in documents if document.decision == "revisions"),
        "created_at": iso(workflow.created_at),
    }


@router.get("/kyc")
def kyc_queue(q: str = "", limit: int | None = None, offset: int | None = None, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(
        select(WorkflowInstance).where(WorkflowInstance.tenant_id == principal.tenant_id).order_by(WorkflowInstance.created_at.desc())
    ).all()
    listed = [_kyc_card(db, row) for row in rows]
    return present(listed, q, lambda row: {"name": row["name"], "status": row["status"], "state": row["state"] or ""}, limit, offset)


@router.get("/kyc/{runtime_id}")
def kyc_case(runtime_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    documents = db.scalars(select(IdentityDocument).where(IdentityDocument.workflow_id == workflow.id).order_by(IdentityDocument.created_at.asc())).all()
    if customer and not documents:
        documents = db.scalars(select(IdentityDocument).where(IdentityDocument.customer_id == customer.id)).all()
    task = db.scalar(select(WorkflowTask).where(WorkflowTask.workflow_id == workflow.id, WorkflowTask.status == "open"))
    return {
        "case": _kyc_card(db, workflow),
        "task": None if not task else {"key": task.task_key, "name": task.name},
        "customer": None if not customer else customer_view(db, customer),
        "documents": [_document_view(document) for document in documents],
        "tracker": _document_tracker(db, principal, workflow, documents),
        "notice": "Les pièces affichées sont des specimens. Une décision sur une pièce n'est pas une vérification d'identité.",
    }


def _document_tracker(db: Session, principal: Principal, workflow: WorkflowInstance, documents: list[IdentityDocument]) -> list[dict]:
    schema: list[dict] = []
    for row in list_docs(db, principal.tenant_id, "workflow_definition"):
        if row.payload.get("processKey") != workflow.definition_key:
            continue
        schema = list(row.payload.get("documentsSchema") or [])
        if row.name in {"kyc", "kyb", "kyc-demo"}:
            break
    used: set[str] = set()
    items = []
    for spec in schema:
        match = next((document for document in documents if document.id not in used and document.doc_type == spec.get("type") and document.category == spec.get("category")), None)
        if match:
            used.add(match.id)
        items.append(
            {
                "id": spec.get("id"),
                "type": spec.get("type"),
                "category": spec.get("category"),
                "country": (spec.get("issuer") or {}).get("country"),
                "required": bool(spec.get("required", True)),
                "status": (match.decision or match.status) if match else "missing",
                "document_id": match.id if match else None,
            }
        )
    for document in documents:
        if document.id in used:
            continue
        items.append(
            {
                "id": document.id,
                "type": document.doc_type,
                "category": document.category,
                "country": document.issuing_country,
                "required": False,
                "status": document.decision or document.status,
                "document_id": document.id,
            }
        )
    return items


@router.post("/kyc/{runtime_id}/decision")
def decide_kyc(runtime_id: str, body: dict, request: Request, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    action = {"approve": "approve", "reject": "reject", "revision": "remediate"}.get(str(body.get("action") or ""))
    if not action:
        raise clarity_error(400, "invalid", "L'action doit être approve, reject ou revision.")
    workflow = _workflow(db, principal, runtime_id)
    advance_workflow(db, request.app.state.engine, _tenant(db, principal), principal, workflow, action, str(body.get("rationale") or ""))
    return kyc_case(runtime_id, principal, db)


@router.post("/documents/{document_id}/decision")
def decide_document(document_id: str, body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    document = db.get(IdentityDocument, document_id)
    if not document or document.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Document introuvable.")
    decision = body.get("decision")
    mapping = {"approve": "approved", "reject": "rejected", "revision": "revisions", None: None}
    if decision not in mapping:
        raise clarity_error(400, "invalid", "La décision doit être approve, reject, revision ou null.")
    document.decision = mapping[decision]
    document.decision_reason = body.get("reason")
    document.comment = body.get("comment")
    audit(db, principal.tenant_id, actor_label(principal), "document.decision", "document", document.id, {"decision": document.decision})
    return _document_view(document)


@router.get("/documents/{document_id}/content")
def document_content(document_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    document = db.get(IdentityDocument, document_id)
    if not document or document.tenant_id != principal.tenant_id or not document.storage_path:
        raise clarity_error(404, "not_found", "Fichier introuvable.")
    return FileResponse(document.storage_path, media_type=(document.properties or {}).get("mime") or "application/octet-stream", filename=(document.properties or {}).get("fileName"))


def _case_row(db: Session, case: CaseRecord) -> dict:
    payload = case_payload(db, case)
    payload["type"] = "decision"
    payload["due_at"] = iso(case.due_at)
    payload["escalated"] = case.escalated
    payload["customer_id"] = case.customer_id
    return payload


@router.get("/cases")
def case_inbox(q: str = "", limit: int | None = None, offset: int | None = None, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(CaseRecord).where(CaseRecord.tenant_id == principal.tenant_id).order_by(CaseRecord.created_at.desc())).all()
    listed = [_case_row(db, row) for row in rows]
    return present(
        listed,
        q,
        lambda row: {"name": row["name"], "status": row["status"], "outcome": row["outcome"] or "", "assignee": (row.get("assignee") or {}).get("name") or ""},
        limit,
        offset,
    )


@router.get("/cases/{case_id}")
def case_manager(case_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    case = _case(db, principal, case_id)
    customer = db.get(Customer, case.customer_id) if case.customer_id else None
    notes = db.scalars(select(CaseComment).where(CaseComment.case_id == case.id).order_by(CaseComment.created_at.asc())).all()
    files = db.scalars(select(Evidence).where(Evidence.case_id == case.id)).all()
    reports = db.scalars(select(RegulatoryReport).where(RegulatoryReport.case_id == case.id)).all()
    alerts = db.scalars(select(Alert).where(Alert.case_id == case.id)).all()
    decisions = db.scalars(select(Decision).where(Decision.case_id == case.id).order_by(Decision.created_at.desc())).all()
    events = db.scalars(
        select(AuditEvent).where(AuditEvent.tenant_id == principal.tenant_id, AuditEvent.entity_id == case.id).order_by(AuditEvent.created_at.asc())
    ).all()
    documents = []
    matches = []
    if customer:
        documents = db.scalars(select(IdentityDocument).where(IdentityDocument.customer_id == customer.id)).all()
        checks = db.scalars(select(ScreeningCheck).where(ScreeningCheck.customer_id == customer.id)).all()
        check_ids = [item.id for item in checks]
        if check_ids:
            matches = db.scalars(select(ScreeningMatch).where(ScreeningMatch.screening_id.in_(check_ids))).all()
    transactions = []
    for alert in alerts:
        if not alert.transaction_id:
            continue
        tx = db.get(Transaction, alert.transaction_id)
        if tx:
            transactions.append({"id": tx.id, "amount": float(tx.amount), "currency": tx.currency, "country": tx.country, "occurred_at": iso(tx.occurred_at), "description": tx.description})
    users = db.scalars(select(User).where(User.tenant_id == principal.tenant_id, User.active.is_(True))).all()
    tags = db.scalars(select(Tag).where(Tag.tenant_id == principal.tenant_id)).all()
    inbox = db.get(Inbox, case.inbox_id)
    return {
        "case": _case_row(db, case),
        "inbox": {"id": inbox.id, "name": inbox.name} if inbox else None,
        "customer": None if not customer else customer_view(db, customer),
        "documents": [_document_view(document) for document in documents],
        "files": [{"id": item.id, "fileName": item.file_name, "url": f"/api/clarity/v1/desks/files/{item.id}/content"} for item in files],
        "comments": [{"id": note.id, "comment": note.comment, "created_at": iso(note.created_at)} for note in notes],
        "events": [{"id": event.id, "action": event.action, "actor": event.actor, "created_at": iso(event.created_at)} for event in events],
        "alerts": [{"id": alert.id, "title": alert.title, "status": alert.status, "priority": alert.priority, "rationale": alert.rationale} for alert in alerts],
        "decisions": [
            {"id": row.id, "outcome": row.outcome, "score": row.score, "rules": row.rules, "created_at": iso(row.created_at)}
            for row in decisions
        ],
        "transactions": transactions,
        "screening": [
            {
                "id": match.id,
                "name": (match.payload or {}).get("caption") or (match.payload or {}).get("name") or match.entity_id,
                "status": match.status,
                "score": match.score,
            }
            for match in matches
        ],
        "reports": [
            {"id": item.id, "type": item.report_type, "status": item.status, "submission_channel": item.submission_channel}
            for item in reports
        ],
        "users": [{"id": user.id, "name": user.full_name, "email": user.email} for user in users],
        "available_tags": [{"id": tag.id, "name": tag.name} for tag in tags],
    }


@router.get("/files/{file_id}/content")
def file_content(file_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    row = db.get(Evidence, file_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Fichier introuvable.")
    return FileResponse(row.storage_path, filename=row.file_name)


@router.post("/cases/{case_id}/comment")
def comment_case(case_id: str, body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    case = _case(db, principal, case_id)
    text = str(body.get("comment") or "").strip()
    if not text:
        raise clarity_error(400, "invalid", "La note est vide.")
    if case.status == "pending":
        case.status = "investigating"
    note = CaseComment(tenant_id=principal.tenant_id, case_id=case.id, user_id=principal.user_id, comment=text)
    db.add(note)
    audit(db, principal.tenant_id, actor_label(principal), "case.comment", "case", case.id, {})
    db.flush()
    return {"id": note.id}


@router.post("/cases/{case_id}/assign")
def assign_case(case_id: str, body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    case = _case(db, principal, case_id)
    user = db.get(User, str(body.get("user_id") or ""))
    if not user or user.tenant_id != principal.tenant_id:
        raise clarity_error(400, "invalid", "Utilisateur introuvable.")
    case.assignee_id = user.id
    case.status = "investigating" if case.status == "pending" else case.status
    case.updated_at = utcnow()
    audit(db, principal.tenant_id, actor_label(principal), "case.assigned", "case", case.id, {"user_id": user.id})
    return _case_row(db, case)


@router.post("/cases/{case_id}/close")
def close_case(case_id: str, body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    case = _case(db, principal, case_id)
    outcome = body.get("outcome") or "unset"
    if outcome not in {"unset", "confirmed_risk", "valuable_alert", "false_positive"}:
        raise clarity_error(400, "invalid", "Issue inconnue.")
    case.status = "closed"
    case.outcome = outcome
    case.updated_at = utcnow()
    audit(db, principal.tenant_id, actor_label(principal), "case.closed", "case", case.id, {"outcome": outcome})
    return _case_row(db, case)


@router.post("/cases/{case_id}/reopen")
def reopen_case(case_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    case = _case(db, principal, case_id)
    case.status = "pending"
    case.updated_at = utcnow()
    audit(db, principal.tenant_id, actor_label(principal), "case.reopened", "case", case.id, {})
    return _case_row(db, case)


@router.post("/cases/{case_id}/snooze")
def snooze_case(case_id: str, body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    from datetime import timedelta

    case = _case(db, principal, case_id)
    days = int(body.get("days") or 2)
    case.snoozed_until = utcnow() + timedelta(days=days)
    case.updated_at = utcnow()
    audit(db, principal.tenant_id, actor_label(principal), "case.snoozed", "case", case.id, {"days": days})
    return _case_row(db, case)


@router.post("/cases/{case_id}/files")
async def upload_case_file(
    case_id: str,
    file: UploadFile = File(...),
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    case = _case(db, principal, case_id)
    content = await file.read()
    if not content:
        raise clarity_error(400, "invalid", "Le fichier est vide.")
    path, digest = store_bytes(ROOT / "data" / "uploads", principal.tenant_id, file.filename or "piece.bin", content)
    row = Evidence(tenant_id=principal.tenant_id, case_id=case.id, customer_id=case.customer_id, file_name=file.filename or "piece.bin", storage_path=path, sha256=digest)
    db.add(row)
    audit(db, principal.tenant_id, actor_label(principal), "case.file", "case", case.id, {"file": row.file_name})
    db.flush()
    return {"id": row.id, "fileName": row.file_name, "url": f"/api/clarity/v1/desks/files/{row.id}/content"}
