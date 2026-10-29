"""Case desks for the KYC review and the AML investigation."""

import re
import unicodedata
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import list_docs
from app.config import ROOT, current_settings
from app.deps import clarity_principal, db_session
from app.engine import process_steps
from app.face import compare_case_faces, face_threshold, set_face_threshold, stored_face_match
from app.ocr import run_on_document
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
    document_labels,
    document_record,
    refuse_verification,
    request_documents,
    store_bytes,
    store_liveness_video,
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


_FILE_KEYS = {"fileName", "mime", "title", "synthetic", "uri", "workflowRuntimeId", "side", "extracted"}
_ACCOUNT_FIELDS = ("firstName", "lastName", "dateOfBirth")
_IDENTITY_FORM = ("firstName", "lastName", "dateOfBirth", "nationality", "gender", "documentNumber", "expiry", "address")
_IDENTITY_TYPES = {"passport", "id_card", "id-card", "drivers_license", "residence_permit"}
_QUIET_SCREENING = {"no_hit", "cleared", "false_positive", "whitelisted"}
_COLUMN_FIELDS = (
    ("type", "doc_type"),
    ("category", "category"),
    ("issuingCountry", "issuing_country"),
    ("expiry", "expiry"),
    ("issuingVersion", "issuing_version"),
)
_FIELD_LABELS = {
    "firstName": "Prénom",
    "lastName": "Nom",
    "dateOfBirth": "Date de naissance",
    "date_of_birth": "Date de naissance",
    "nationality": "Nationalité",
    "residence_country": "Pays de résidence",
    "email": "E-mail",
    "phone": "Téléphone",
    "address": "Adresse",
    "country": "Pays",
    "legal_name": "Nom",
    "type": "Type",
    "category": "Catégorie",
    "issuingCountry": "Pays émetteur",
    "expiry": "Expiration",
    "issuingVersion": "Version du document",
    "documentNumber": "Numéro du document",
    "gender": "Sexe",
    "fullName": "Nom complet",
    "trading_name": "Nom commercial",
    "registration_number": "Immatriculation",
    "jurisdiction": "Juridiction",
    "legal_form": "Forme juridique",
    "registered_address": "Adresse du siège",
    "industry": "Secteur",
    "pepPublicFunction": "Exercez-vous ou avez-vous exercé une fonction publique importante ?",
    "pepFamilyAssociate": "Êtes-vous un membre de la famille ou un proche associé d'une personne politiquement exposée ?",
    "pepFinancialInvolvement": "Une personne politiquement exposée intervient-elle dans vos activités ou vos opérations financières ?",
    "amlScore": "Score AML",
    "amount": "Montant",
    "currency": "Devise",
    "counterpartyName": "Donneur d'ordre",
    "direction": "Sens",
    "description": "Libellé",
    "amlOutcome": "Résultat AML",
    "screeningTags": "Étiquettes de criblage",
}
_AML_QUESTIONS = ("pepPublicFunction", "pepFamilyAssociate", "pepFinancialInvolvement")


def _label(key: str) -> str:
    if key in _FIELD_LABELS:
        return _FIELD_LABELS[key]
    spaced = key.replace("_", " ")
    return spaced[:1].upper() + spaced[1:] if spaced else key


def _scalar_fields(source: dict) -> list[dict]:
    rows = []
    for key, value in source.items():
        if isinstance(value, (dict, list)) or value in (None, ""):
            continue
        rows.append({"key": key, "label": _label(key), "value": str(value)})
    return rows


def _yes_no(value: object) -> str:
    if value is True:
        return "Oui"
    if value is False:
        return "Non"
    return ""


def _aml_profile(db: Session, workflow: WorkflowInstance) -> dict:
    contexts = [workflow.context or {}]
    if workflow.customer_id:
        others = db.scalars(
            select(WorkflowInstance).where(
                WorkflowInstance.tenant_id == workflow.tenant_id,
                WorkflowInstance.customer_id == workflow.customer_id,
                WorkflowInstance.id != workflow.id,
            )
        ).all()
        contexts.extend(row.context or {} for row in others)
    fallback = None
    for context in contexts:
        profile = context.get("marbleAml")
        if not isinstance(profile, dict):
            continue
        provided = profile.get("provided") if isinstance(profile.get("provided"), dict) else {}
        if any(isinstance(value, bool) for value in provided.values()):
            return profile
        fallback = fallback or profile
    return fallback or {}


def _aml_fields(db: Session, workflow: WorkflowInstance) -> list[dict]:
    profile = _aml_profile(db, workflow)
    provided = profile.get("provided") if isinstance(profile.get("provided"), dict) else {}
    system = profile.get("system") if isinstance(profile.get("system"), dict) else {}
    rows = []
    for key in _AML_QUESTIONS:
        answer = _yes_no(provided.get(key))
        if answer:
            rows.append({"key": key, "label": _label(key), "value": answer})
    if isinstance(system.get("score"), (int, float)):
        rows.append({"key": "amlScore", "label": _label("amlScore"), "value": str(system["score"])})
    outcome = system.get("outcome") or system.get("riskLevel")
    if isinstance(outcome, str) and outcome.strip():
        rows.append({"key": "amlOutcome", "label": _label("amlOutcome"), "value": outcome.strip()})
    tags = system.get("screeningTags")
    if isinstance(tags, str) and tags.strip():
        rows.append({"key": "screeningTags", "label": _label("screeningTags"), "value": tags.strip()})
    return rows


def _submitted_fields(db: Session, workflow: WorkflowInstance, customer: Customer | None) -> list[dict]:
    context = workflow.context or {}
    entity = context.get("entity") if isinstance(context.get("entity"), dict) else {}
    data = entity.get("data") if isinstance(entity.get("data"), dict) else {}
    rows = _scalar_fields(data)
    seen = {row["key"] for row in rows}
    profile = {}
    if customer and customer.individual:
        profile = {
            "legal_name": customer.individual.legal_name,
            "date_of_birth": customer.individual.date_of_birth,
            "nationality": customer.individual.nationality,
            "residence_country": customer.individual.residence_country,
            "address": customer.individual.address,
            "email": customer.individual.email,
            "phone": customer.individual.phone,
        }
    elif customer and customer.business:
        profile = {
            "legal_name": customer.business.legal_name,
            "trading_name": customer.business.trading_name,
            "registration_number": customer.business.registration_number,
            "jurisdiction": customer.business.jurisdiction,
            "legal_form": customer.business.legal_form,
            "registered_address": customer.business.registered_address,
            "industry": customer.business.industry,
        }
    for row in _scalar_fields(profile):
        if row["key"] not in seen:
            rows.append(row)
            seen.add(row["key"])
    for row in _aml_fields(db, workflow):
        if row["key"] not in seen:
            rows.append(row)
    return rows


def _clean(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return "" if not text or set(text) <= {"-", ".", "/"} else text


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", text)


def _norm_date(value: str) -> str:
    text = _clean(value)
    match = re.fullmatch(r"(\d{2})[/. -](\d{2})[/. -](\d{4})", text)
    if match:
        text = f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    return re.sub(r"\D", "", text)


def _declared_identity(workflow: WorkflowInstance, customer: Customer | None) -> dict[str, str]:
    context = workflow.context or {}
    entity = context.get("entity") if isinstance(context.get("entity"), dict) else {}
    data = entity.get("data") if isinstance(entity.get("data"), dict) else {}
    values = {
        "firstName": _clean(data.get("firstName") or data.get("given_name")),
        "lastName": _clean(data.get("lastName") or data.get("family_name")),
        "dateOfBirth": _clean(data.get("dateOfBirth") or data.get("date_of_birth")),
    }
    if customer and customer.individual and not values["dateOfBirth"]:
        values["dateOfBirth"] = _clean(customer.individual.date_of_birth)
    return values


def _document_values(document: IdentityDocument) -> dict[str, str]:
    properties = document.properties or {}
    stored = properties.get("extracted") if isinstance(properties.get("extracted"), dict) else {}
    values = {key: _clean(getattr(document, column)) for key, column in _COLUMN_FIELDS}
    for key, value in properties.items():
        if key in _FILE_KEYS or isinstance(value, (dict, list)):
            continue
        values[key] = _clean(value)
    for key, value in stored.items():
        if not isinstance(value, (dict, list)):
            values[str(key)] = _clean(value)
    if values.get("expires") and not values.get("expiry"):
        values["expiry"] = values["expires"]
    if values.get("expiry") and not values.get("expires"):
        values["expires"] = values["expiry"]
    return values


def _same(key: str, left: str, right: str) -> bool:
    if not left or not right:
        return False
    if key == "dateOfBirth":
        return _norm_date(left) == _norm_date(right)
    return _norm(left) == _norm(right)


def _extracted_fields(document: IdentityDocument, declared: dict[str, str] | None = None) -> list[dict]:
    values = _document_values(document)
    compare = document.doc_type in _IDENTITY_TYPES
    ordered = [key for key in (*_ACCOUNT_FIELDS, "nationality", "documentNumber", "expiry") if key in _ACCOUNT_FIELDS or key in values]
    skip = {"expiry"} if "expiry" in ordered else set()
    if values.get("expires") == values.get("expiry"):
        skip.add("expires")
    ordered.extend(key for key in values if key not in ordered and key not in skip)
    rows = []
    for key in ordered:
        value = values.get(key, "")
        match = None
        if compare and key in _ACCOUNT_FIELDS and declared is not None:
            match = _same(key, declared.get(key, ""), value)
        rows.append({"key": key, "label": _label(key), "value": value, "match": match})
    return rows


def _screening_alert(db: Session, customer: Customer | None) -> bool:
    if not customer:
        return False
    checks = db.scalars(select(ScreeningCheck).where(ScreeningCheck.customer_id == customer.id)).all()
    return any(check.status not in _QUIET_SCREENING for check in checks)


def _review_flags(db: Session, workflow: WorkflowInstance, customer: Customer | None, documents: list[IdentityDocument]) -> dict:
    declared = _declared_identity(workflow, customer)
    identity = [document for document in documents if document.doc_type in _IDENTITY_TYPES]
    mismatched = []
    for key in _ACCOUNT_FIELDS:
        read = next((_document_values(document).get(key, "") for document in identity if _document_values(document).get(key, "")), "")
        if not _same(key, declared.get(key, ""), read):
            mismatched.append(key)
    reasons = []
    if mismatched or not identity:
        reasons.append("correspondence")
    if _screening_alert(db, customer):
        reasons.append("screening")
    return {"alert": bool(reasons), "alert_reasons": reasons, "mismatched": mismatched}


def _identity_documents(documents: list[IdentityDocument]) -> list[IdentityDocument]:
    return [document for document in documents if document.doc_type in _IDENTITY_TYPES]


def _merged_identity(documents: list[IdentityDocument], declared: dict[str, str]) -> list[dict]:
    identity = _identity_documents(documents)
    values: dict[str, str] = {key: "" for key in _IDENTITY_FORM}
    ranked = sorted(
        identity,
        key=lambda document: (
            document.doc_type == "passport",
            bool(_document_values(document).get("firstName") and _document_values(document).get("lastName")),
        ),
        reverse=True,
    )
    for document in ranked:
        found = _document_values(document)
        for key in _IDENTITY_FORM:
            if found.get(key) and not values[key]:
                values[key] = found[key]
    rows = []
    for key in _IDENTITY_FORM:
        match = _same(key, declared.get(key, ""), values[key]) if key in _ACCOUNT_FIELDS else None
        rows.append({"key": key, "label": _label(key), "value": values[key], "match": match})
    return rows


def _write_document_fields(document: IdentityDocument, fields: dict) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    columns = dict(_COLUMN_FIELDS)
    properties = dict(document.properties or {})
    extracted = dict(properties.get("extracted") or {})
    for key, value in fields.items():
        name = str(key)
        text = "" if value is None else str(value).strip()
        if name in columns:
            setattr(document, columns[name], text or None)
        extracted[name] = text
        if name == "expiry":
            document.expiry = text or None
    properties["extracted"] = extracted
    document.properties = properties
    flag_modified(document, "properties")


def _document_view(document: IdentityDocument, declared: dict[str, str] | None = None) -> dict:
    payload = document_record(document)
    title = (document.properties or {}).get("title") or document.doc_type
    payload.update(
        {
            "title": title,
            "fileName": (document.properties or {}).get("fileName") or f"{document.doc_type}.bin",
            "mime": (document.properties or {}).get("mime") or "application/octet-stream",
            "synthetic": bool((document.properties or {}).get("synthetic")),
            "url": f"/api/clarity/v1/desks/documents/{document.id}/content",
            "extracted": _extracted_fields(document, declared),
            "ocr": (document.properties or {}).get("ocr"),
        }
    )
    return payload


def _definition_title(db: Session, workflow: WorkflowInstance) -> str:
    for row in list_docs(db, workflow.tenant_id, "workflow_definition"):
        if row.payload.get("processKey") == workflow.definition_key or row.name == workflow.definition_key:
            title = row.payload.get("displayName")
            if title:
                return str(title)
    return workflow.definition_name


def _kyc_card(db: Session, workflow: WorkflowInstance) -> dict:
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    documents = db.scalars(select(IdentityDocument).where(IdentityDocument.workflow_id == workflow.id)).all()
    task = db.scalar(select(WorkflowTask).where(WorkflowTask.workflow_id == workflow.id, WorkflowTask.status == "open"))
    return {
        "id": workflow.ballerine_runtime_id,
        "workflow_id": workflow.id,
        "name": customer.display_name if customer else workflow.definition_name,
        "definition": _definition_title(db, workflow),
        "definition_key": workflow.definition_key,
        "status": "failed" if workflow.state == "rejected" else "approved-partially" if workflow.state == "approved-partially" else workflow.status,
        "state": workflow.state,
        "task": None if not task else {"key": task.task_key, "name": task.name},
        "documents": len(documents),
        "revisions": sum(1 for document in documents if document.decision == "revisions"),
        "created_at": iso(workflow.created_at),
        "alert": _review_flags(db, workflow, customer, documents)["alert"],
    }


@router.get("/kyc")
def kyc_queue(q: str = "", kind: str = "", limit: int | None = None, offset: int | None = None, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(
        select(WorkflowInstance).where(WorkflowInstance.tenant_id == principal.tenant_id).order_by(WorkflowInstance.created_at.desc())
    ).all()
    listed = []
    for row in rows:
        if kind == "business":
            customer = db.get(Customer, row.customer_id) if row.customer_id else None
            if not customer or customer.kind != "business":
                continue
        listed.append(_kyc_card(db, row))
    return present(listed, q, lambda row: {"name": row["name"], "status": row["status"], "state": row["state"] or ""}, limit, offset)


@router.get("/kyc/{runtime_id}")
def kyc_case(runtime_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    documents = db.scalars(select(IdentityDocument).where(IdentityDocument.workflow_id == workflow.id).order_by(IdentityDocument.created_at.asc())).all()
    if customer and not documents:
        documents = db.scalars(select(IdentityDocument).where(IdentityDocument.customer_id == customer.id)).all()
    task = db.scalar(select(WorkflowTask).where(WorkflowTask.workflow_id == workflow.id, WorkflowTask.status == "open"))
    declared = _declared_identity(workflow, customer)
    flags = _review_flags(db, workflow, customer, documents)
    return {
        "case": _kyc_card(db, workflow),
        "task": None if not task else {"key": task.task_key, "name": task.name},
        "customer": None if not customer else customer_view(db, customer),
        "submitted": _submitted_fields(db, workflow, customer),
        "identity": _merged_identity(documents, declared),
        "documents": [_document_view(document, declared) for document in documents],
        "tracker": _document_tracker(db, principal, workflow, documents),
        "alert": flags["alert"],
        "alert_reasons": flags["alert_reasons"],
        "steps": process_steps(workflow.definition_key, workflow.state),
        "requested_documents": list((workflow.context or {}).get("documentsRequested") or []),
        "face_match": stored_face_match(db, workflow),
        "face_threshold": face_threshold(db, principal.tenant_id),
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
                "label": document_labels(str(spec.get("type") or ""), spec),
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
                "label": document_labels(document.doc_type),
            }
        )
    return items


@router.post("/kyc/{runtime_id}/decision")
def decide_kyc(runtime_id: str, body: dict, request: Request, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    action = {"approve": "approve", "reject": "reject", "revision": "remediate", "approve-partial": "approvePartial"}.get(str(body.get("action") or ""))
    if not action:
        raise clarity_error(400, "invalid", "L'action doit être approve, reject, revision ou approve-partial.")
    workflow = _workflow(db, principal, runtime_id)
    tenant = _tenant(db, principal)
    rationale = str(body.get("rationale") or "")
    if action == "reject":
        refuse_verification(db, request.app.state.engine, tenant, principal, workflow, rationale)
    else:
        advance_workflow(db, request.app.state.engine, tenant, principal, workflow, action, rationale)
    return kyc_case(runtime_id, principal, db)


@router.post("/kyc/{runtime_id}/document-requests")
def ask_documents(runtime_id: str, body: dict, request: Request, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    request_documents(
        db,
        request.app.state.engine,
        _tenant(db, principal),
        principal,
        workflow,
        body.get("items"),
        str(body.get("reason") or ""),
    )
    return kyc_case(runtime_id, principal, db)


@router.post("/kyc/{runtime_id}/liveness")
async def upload_liveness_video(
    runtime_id: str,
    file: UploadFile = File(...),
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    """Store a compressed liveness video the same way a document image is stored."""
    workflow = _workflow(db, principal, runtime_id)
    document = store_liveness_video(db, principal, workflow, await file.read(), file.filename or "liveness.mp4", file.content_type or "")
    return document_record(document)


@router.patch("/kyc/{runtime_id}/documents/{document_id}")
def update_document_fields(runtime_id: str, document_id: str, body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    document = db.get(IdentityDocument, document_id)
    if not document or document.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Document introuvable.")
    if document.workflow_id not in {None, workflow.id} and document.customer_id != workflow.customer_id:
        raise clarity_error(404, "not_found", "Document introuvable sur ce dossier.")
    fields = body.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise clarity_error(400, "invalid", "Les champs du document sont requis.")
    _write_document_fields(document, fields)
    audit(db, principal.tenant_id, actor_label(principal), "document.fields", "document", document.id, {"fields": list(fields)})
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    return _document_view(document, _declared_identity(workflow, customer))


@router.post("/kyc/{runtime_id}/documents/{document_id}/ocr")
def read_document_ocr(runtime_id: str, document_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    document = db.get(IdentityDocument, document_id)
    if not document or document.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Document introuvable.")
    if document.workflow_id not in {None, workflow.id} and document.customer_id != workflow.customer_id:
        raise clarity_error(404, "not_found", "Document introuvable sur ce dossier.")
    result = run_on_document(document)
    if result is None:
        raise clarity_error(400, "invalid", "Cette pièce n'a pas de fichier à lire.")
    audit(db, principal.tenant_id, actor_label(principal), "document.ocr", "document", document.id, {"engine": result["engine"], "confidence": result["confidence"]})
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    return {**result, "document": _document_view(document, _declared_identity(workflow, customer))}


def _case_documents(db: Session, workflow: WorkflowInstance) -> list[IdentityDocument]:
    documents = list(db.scalars(select(IdentityDocument).where(IdentityDocument.workflow_id == workflow.id).order_by(IdentityDocument.created_at.asc())).all())
    if workflow.customer_id and not documents:
        documents = list(db.scalars(select(IdentityDocument).where(IdentityDocument.customer_id == workflow.customer_id)).all())
    return documents


@router.post("/kyc/{runtime_id}/face-match")
def match_faces(runtime_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    compare_case_faces(db, workflow, _case_documents(db, workflow))
    return kyc_case(runtime_id, principal, db)


@router.put("/kyc/face-threshold")
def update_face_threshold(body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    saved = set_face_threshold(db, principal.tenant_id, body.get("threshold"))
    return {"threshold": saved}


@router.post("/kyc/{runtime_id}/identity/ocr")
def read_identity_documents(runtime_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    identity = _identity_documents(_case_documents(db, workflow))
    reads = []
    for document in identity:
        result = run_on_document(document)
        if result is None:
            continue
        reads.append(result["parsedData"])
        audit(db, principal.tenant_id, actor_label(principal), "document.ocr", "document", document.id, {"engine": result["engine"], "confidence": result["confidence"]})
    if not reads:
        raise clarity_error(400, "invalid", "Aucune carte d'identité ou passeport à lire.")
    merged = {key: "" for key in _IDENTITY_FORM}
    ranked = sorted(reads, key=lambda item: (bool(item.get("firstName") and item.get("lastName")), bool(item.get("dateOfBirth"))), reverse=True)
    for item in ranked:
        for key in _IDENTITY_FORM:
            if item.get(key) and not merged[key]:
                merged[key] = item[key]
    stored = {key: merged.get(key, "") for key in _IDENTITY_FORM}
    for document in identity:
        _write_document_fields(document, stored)
    return kyc_case(runtime_id, principal, db)


@router.patch("/kyc/{runtime_id}/identity")
def save_identity(runtime_id: str, body: dict, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    fields = body.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise clarity_error(400, "invalid", "Les champs d'identité sont requis.")
    identity = _identity_documents(_case_documents(db, workflow))
    if not identity:
        raise clarity_error(404, "not_found", "Aucune carte d'identité ou passeport sur ce dossier.")
    kept = {key: fields.get(key, "") for key in _IDENTITY_FORM if key in fields}
    for document in identity:
        _write_document_fields(document, kept)
        audit(db, principal.tenant_id, actor_label(principal), "document.fields", "document", document.id, {"fields": list(kept)})
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
    if not document or document.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Fichier introuvable.")
    return _stored_response(document.storage_path, document.tenant_id, (document.properties or {}).get("fileName"), (document.properties or {}).get("mime"))


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
    return _stored_response(row.storage_path, row.tenant_id, row.file_name, None)


def _stored_response(storage_path: str | None, tenant_id: str, file_name: str | None, mime: str | None) -> Response:
    path = _resolve_stored_file(storage_path, tenant_id)
    if path is None:
        raise clarity_error(404, "not_found", "Fichier introuvable.")
    name = _safe_filename(file_name or path.name)
    media = _safe_mime(mime, name)
    return Response(content=path.read_bytes(), media_type=media, headers={"Content-Disposition": f'inline; filename="{name}"'})


def _resolve_stored_file(storage_path: str | None, tenant_id: str) -> Path | None:
    if not storage_path:
        return None
    path = Path(storage_path)
    if path.is_file():
        return path
    name = path.name
    for root in (Path(current_settings().upload_dir), ROOT / "data" / "uploads"):
        candidate = root / tenant_id / name
        if candidate.is_file():
            return candidate
    return None


def _safe_filename(name: str) -> str:
    cleaned = Path(str(name)).name.replace('"', "").replace("\n", "").replace("\r", "").strip()
    return cleaned or "document.bin"


def _safe_mime(mime: str | None, filename: str) -> str:
    text = str(mime or "").split(";", 1)[0].strip().lower()
    if text.startswith("image/") or text.startswith("video/") or text in {"application/pdf", "application/octet-stream"}:
        return text
    if filename.lower().endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if filename.lower().endswith(".png"):
        return "image/png"
    if filename.lower().endswith(".webp"):
        return "image/webp"
    if filename.lower().endswith(".svg"):
        return "image/svg+xml"
    if filename.lower().endswith(".pdf"):
        return "application/pdf"
    return "application/octet-stream"


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
