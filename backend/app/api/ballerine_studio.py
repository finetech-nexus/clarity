"""Ballerine configuration, collection flow and the case-management routes."""

from fastapi import APIRouter, Body, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.ballerine import create_transaction, create_transaction_alt, run_workflow, _tenant, _workflow
from app.catalog import add_doc, get_doc, list_docs, resolve_workflow_definition, touch, workflow_definition_dto
from app.config import ROOT
from app.deps import ballerine_principal, db_session
from app.models import ConfigDocument
from app.security import hash_token
from app.timeutil import iso, parse_dt, utcnow
from app.engine import PROCESS_CATALOG
from app.models import Alert, AuditEvent, Customer, IdentityDocument, IndividualProfile, MonitoringRule, RiskAssessment, Transaction, new_id
from app.security import Principal
from app.services import advance_workflow, assess_customer, document_record, nest_error, start_customer_workflow, store_bytes
from app.timeutil import iso, parse_dt, utcnow


def journey_principal(request: Request, db: Session = Depends(db_session)) -> Principal:
    header = request.headers.get("authorization", "")
    token = header.split(" ", 1)[1].strip() if header.lower().startswith("bearer ") else ""
    if token:
        digest = hash_token(token)
        rows = db.scalars(select(ConfigDocument).where(ConfigDocument.kind == "workflow_token")).all()
        for row in rows:
            if row.payload.get("token_hash") != digest:
                continue
            if not row.payload.get("end_user_id"):
                raise nest_error(401, "No EndUser is set for this token")
            if parse_dt(row.payload["expires_at"]) < utcnow():
                raise nest_error(401, "token expired")
            return Principal(row.tenant_id, "collection", row.payload.get("end_user_id"), None, "end_user", row.payload.get("workflow_runtime_id"))
    return ballerine_principal(request, db)


def _runtime(principal: Principal, workflow_runtime_id: str | None) -> str:
    if principal.role == "end_user":
        if workflow_runtime_id and workflow_runtime_id != principal.credential_id:
            raise nest_error(401, "token does not match this workflow")
        return principal.credential_id or ""
    if not workflow_runtime_id:
        raise nest_error(400, "workflowRuntimeId is required")
    return workflow_runtime_id

router = APIRouter(prefix="/api/v1", tags=["ballerine-studio"])


def _definition_or_404(db: Session, principal: Principal, definition_id: str) -> ConfigDocument:
    row = get_doc(db, principal.tenant_id, "workflow_definition", definition_id)
    if not row:
        raise nest_error(404, f"No WorkflowDefinition with ID {definition_id} was found")
    return row


@router.get("/workflow-definition")
def list_definitions(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    return [workflow_definition_dto(row) for row in list_docs(db, principal.tenant_id, "workflow_definition")]


@router.post("/workflow-definition", status_code=201)
def create_definition(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    name = body.get("name")
    process = body.get("processKey") or (body.get("definition") or {}).get("processKey")
    if not isinstance(name, str) or not name:
        raise nest_error(400, "name is required")
    if process not in PROCESS_CATALOG:
        raise nest_error(400, "processKey must be one of " + ", ".join(PROCESS_CATALOG))
    entity = body.get("entityType") or "individual"
    if entity not in {"individual", "business", "any"}:
        raise nest_error(400, "entityType must be individual, business or any")
    row = add_doc(
        db,
        principal.tenant_id,
        "workflow_definition",
        name,
        {
            "displayName": body.get("displayName") or name,
            "version": 1,
            "definitionType": body.get("definitionType") or "bpmn",
            "processKey": process,
            "entityType": entity,
            "definition": body.get("definition") or {"initial": PROCESS_CATALOG[process]["start"], "states": {}},
            "config": body.get("config") or {},
            "extensions": body.get("extensions") or {},
            "contextSchema": body.get("contextSchema") or {},
            "submitStates": body.get("submitStates") or [],
        },
        status="published",
    )
    return workflow_definition_dto(row)


@router.get("/workflow-definition/{definition_id}/input-context-schema")
def input_context_schema(definition_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    row = _definition_or_404(db, principal, definition_id)
    return row.payload.get("contextSchema") or {}


@router.post("/workflow-definition/{definition_id}/upgrade")
def upgrade_definition(definition_id: str, body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    row = _definition_or_404(db, principal, definition_id)
    payload = dict(row.payload)
    for key in ("displayName", "definition", "config", "extensions", "submitStates", "contextSchema", "processKey", "entityType"):
        if key in body:
            payload[key] = body[key]
    if "name" in body:
        row.name = str(body["name"])
    if payload.get("processKey") not in PROCESS_CATALOG:
        raise nest_error(400, "processKey is not executable")
    payload["version"] = int(payload.get("version") or 1) + 1
    row.payload = payload
    touch(row)
    return workflow_definition_dto(row)


@router.post("/workflow-definition/{definition_id}/copy")
def copy_definition(definition_id: str, body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    source = _definition_or_404(db, principal, definition_id)
    copied = dict(source.payload)
    name = body.get("name") or f"{source.name}-copy"
    row = add_doc(db, principal.tenant_id, "workflow_definition", name, copied, status="published")
    if body.get("displayName"):
        row.payload = {**row.payload, "displayName": body["displayName"]}
    return workflow_definition_dto(row)


@router.put("/workflow-definition/{definition_id}/documents-schema")
def replace_documents_schema(definition_id: str, documents: list = Body(), principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    row = _definition_or_404(db, principal, definition_id)
    if not isinstance(documents, list):
        raise nest_error(400, "documentsSchema must be a list")
    cleaned = []
    for index, item in enumerate(documents):
        if not isinstance(item, dict) or not item.get("type") or not item.get("category"):
            raise nest_error(400, "each document type needs type and category")
        issuer = item.get("issuer") if isinstance(item.get("issuer"), dict) else {}
        cleaned.append(
            {
                "id": str(item.get("id") or f"doc-{index + 1}"),
                "category": str(item["category"]),
                "type": str(item["type"]),
                "issuer": {"country": str(issuer.get("country") or "ZZ")},
                "version": int(item.get("version") or 1),
                "propertiesSchema": item.get("propertiesSchema") if isinstance(item.get("propertiesSchema"), dict) else {"type": "object", "properties": {}},
                "required": bool(item.get("required", True)),
            }
        )
    row.payload = {**row.payload, "documentsSchema": cleaned}
    touch(row)
    return workflow_definition_dto(row)


@router.put("/workflow-definition/{definition_id}/definition")
def replace_definition(definition_id: str, body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    row = _definition_or_404(db, principal, definition_id)
    row.payload = {**row.payload, "definition": body}
    touch(row)
    return workflow_definition_dto(row)


@router.get("/external/ui-definition")
@router.get("/ui-definition")
def list_ui(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    return [row.payload | {"id": row.id, "name": row.name} for row in list_docs(db, principal.tenant_id, "ui_definition")]


@router.post("/ui-definition", status_code=201)
def create_ui(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    if not body.get("workflowDefinitionId"):
        raise nest_error(400, "workflowDefinitionId is required")
    _definition_or_404(db, principal, str(body["workflowDefinitionId"]))
    row = add_doc(
        db,
        principal.tenant_id,
        "ui_definition",
        body.get("name") or "ui",
        {"workflowDefinitionId": body["workflowDefinitionId"], "uiSchema": body.get("uiSchema") or {}, "locales": body.get("locales") or {}},
        parent_id=str(body["workflowDefinitionId"]),
    )
    return row.payload | {"id": row.id, "name": row.name}


@router.get("/ui-definition/workflow-definition/{definition_id}")
def ui_for_definition(definition_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    _definition_or_404(db, principal, definition_id)
    return [row.payload | {"id": row.id, "name": row.name} for row in list_docs(db, principal.tenant_id, "ui_definition", definition_id)]


@router.put("/external/ui-definition/{ui_id}")
def update_ui(ui_id: str, body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    row = get_doc(db, principal.tenant_id, "ui_definition", ui_id)
    if not row:
        raise nest_error(404, "ui definition not found")
    row.payload = {**row.payload, **{key: body[key] for key in ("uiSchema", "locales", "name") if key in body}}
    if "name" in body:
        row.name = str(body["name"])
    touch(row)
    return row.payload | {"id": row.id, "name": row.name}


@router.get("/external/alert-definition")
def list_alert_definitions(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    stored = [row.payload | {"id": row.id, "name": row.name} for row in list_docs(db, principal.tenant_id, "alert_definition")]
    rules = db.scalars(select(MonitoringRule).where(MonitoringRule.tenant_id == principal.tenant_id)).all()
    stored.extend({"id": row.id, "name": row.name, "scenario": row.scenario, "enabled": row.enabled, "description": row.description} for row in rules)
    return stored


@router.get("/external/filters")
@router.get("/internal/filters")
def list_filters(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    return [row.payload | {"id": row.id, "name": row.name} for row in list_docs(db, principal.tenant_id, "filter")]


@router.post("/external/filters", status_code=201)
def create_filter(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    if not body.get("name"):
        raise nest_error(400, "name is required")
    row = add_doc(db, principal.tenant_id, "filter", str(body["name"]), {"entity": body.get("entity") or "cases", "query": body.get("query") or {}})
    return row.payload | {"id": row.id, "name": row.name}


@router.get("/external/filters/{filter_id}")
def get_filter(filter_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    row = get_doc(db, principal.tenant_id, "filter", filter_id)
    if not row:
        raise nest_error(404, "filter not found")
    return row.payload | {"id": row.id, "name": row.name}


@router.post("/external/end-users", status_code=201)
def create_end_user(body: dict, request: Request, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    for field in ("firstName", "lastName", "correlationId"):
        if not body.get(field):
            raise nest_error(400, f"{field} is required")
    tenant = _tenant(db, principal)
    customer, workflow = start_customer_workflow(
        db,
        request.app.state.engine,
        tenant,
        principal,
        kind="individual",
        external_ref=str(body["correlationId"]),
        data=body,
        context={"entity": {"type": "individual", "id": body["correlationId"], "data": body}},
        config={},
        synthetic=tenant.synthetic,
        definition_key="clarityKycIndividual",
    )
    return {"id": customer.id, "correlationId": customer.external_ref, "firstName": body["firstName"], "lastName": body["lastName"], "workflowRuntimeId": workflow.ballerine_runtime_id}


@router.get("/external/end-users")
def list_end_users(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Customer).where(Customer.tenant_id == principal.tenant_id, Customer.kind == "individual")).all()
    return [{"id": row.id, "correlationId": row.external_ref, "firstName": row.individual.given_name if row.individual else "", "lastName": row.individual.family_name if row.individual else "", "email": row.individual.email if row.individual else None} for row in rows]


@router.get("/external/end-users/{end_user_id}")
def get_end_user(end_user_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    row = db.get(Customer, end_user_id)
    if not row or row.tenant_id != principal.tenant_id or row.kind != "individual":
        raise nest_error(404, "end user not found")
    profile: IndividualProfile | None = row.individual
    return {"id": row.id, "correlationId": row.external_ref, "firstName": profile.given_name if profile else "", "lastName": profile.family_name if profile else ""}


@router.post("/external/businesses", status_code=201)
def create_business(body: dict, request: Request, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    if not body.get("companyName") or not body.get("correlationId"):
        raise nest_error(400, "companyName and correlationId are required")
    tenant = _tenant(db, principal)
    customer, workflow = start_customer_workflow(
        db,
        request.app.state.engine,
        tenant,
        principal,
        kind="business",
        external_ref=str(body["correlationId"]),
        data={"legalName": body["companyName"], **body},
        context={"entity": {"type": "business", "id": body["correlationId"], "data": body}},
        config={},
        synthetic=tenant.synthetic,
        definition_key="clarityKybBusiness",
    )
    return {"id": customer.id, "correlationId": customer.external_ref, "companyName": body["companyName"], "workflowRuntimeId": workflow.ballerine_runtime_id}


@router.get("/external/businesses")
def list_businesses(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Customer).where(Customer.tenant_id == principal.tenant_id, Customer.kind == "business")).all()
    return [{"id": row.id, "correlationId": row.external_ref, "companyName": row.business.legal_name if row.business else row.display_name} for row in rows]


@router.post("/external/transactions/bulk", status_code=201)
def bulk_transactions(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    records = body if isinstance(body, list) else body.get("transactions") or body.get("data")
    if not isinstance(records, list) or not records:
        raise nest_error(400, "a list of transactions is required")
    created = [create_transaction(item, principal, db) for item in records]
    return {"results": created}


@router.post("/external/transactions/alt/bulk", status_code=201)
def bulk_transactions_alt(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    records = body.get("data") if isinstance(body, dict) else None
    if not isinstance(records, list) or not records:
        raise nest_error(400, "data must be a list")
    return {"results": [create_transaction_alt({"data": item}, principal, db) for item in records]}


@router.post("/external/notes", status_code=201)
def create_note(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    if not body.get("content") or not body.get("noteableId") or not body.get("noteableType"):
        raise nest_error(400, "content, noteableId and noteableType are required")
    row = add_doc(
        db,
        principal.tenant_id,
        "note",
        str(body["noteableType"]),
        {"content": body["content"], "noteableId": body["noteableId"], "noteableType": body["noteableType"]},
        parent_id=str(body["noteableId"]),
    )
    return {"id": row.id, "content": body["content"], "noteableId": body["noteableId"], "noteableType": body["noteableType"], "createdAt": iso(row.created_at)}


@router.get("/external/notes/{noteable_type}/{noteable_id}")
def list_notes(noteable_type: str, noteable_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    rows = [row for row in list_docs(db, principal.tenant_id, "note", noteable_id) if row.payload.get("noteableType") == noteable_type]
    return [{"id": row.id, "content": row.payload.get("content"), "createdAt": iso(row.created_at)} for row in rows]


@router.get("/external/alerts")
def list_alerts(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Alert).where(Alert.tenant_id == principal.tenant_id).order_by(Alert.created_at.desc())).all()
    return [{"id": row.id, "title": row.title, "status": row.status, "priority": row.priority, "createdAt": iso(row.created_at)} for row in rows]


@router.post("/external/alerts", status_code=201)
def create_alert(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    if not body.get("title"):
        raise nest_error(400, "title is required")
    row = Alert(
        tenant_id=principal.tenant_id,
        title=str(body["title"]),
        priority=str(body.get("priority") or "medium"),
        status="open",
        dedup_key=f"manual:{body['title']}",
    )
    db.add(row)
    db.flush()
    return {"id": row.id, "title": row.title, "status": row.status}


@router.patch("/external/alerts/decision")
def decide_alert(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    if not body.get("alertId") or body.get("decision") not in {"approve", "reject", "escalate"}:
        raise nest_error(400, "alertId and decision approve|reject|escalate are required")
    row = db.get(Alert, str(body["alertId"]))
    if not row or row.tenant_id != principal.tenant_id:
        raise nest_error(404, "alert not found")
    row.disposition = body["decision"]
    row.status = "closed" if body["decision"] != "escalate" else "escalated"
    row.rationale = body.get("reason")
    return {"id": row.id, "status": row.status, "decision": row.disposition}


@router.post("/external/assessments", status_code=201)
def create_assessment(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    if not body.get("workflowRuntimeDataId"):
        raise nest_error(400, "workflowRuntimeDataId is required")
    workflow = _workflow(db, principal, str(body["workflowRuntimeDataId"]))
    if not workflow.customer_id:
        raise nest_error(404, "customer not found")
    tenant = _tenant(db, principal)
    customer = db.get(Customer, workflow.customer_id)
    if not customer:
        raise nest_error(404, "customer not found")
    assessment = assess_customer(db, tenant, customer, "ruleset")
    return {"id": assessment.id, "workflowRuntimeDataId": workflow.ballerine_runtime_id, "status": "completed", "production": False, "score": assessment.score, "tier": assessment.tier}


@router.get("/external/assessments/latest-by-workflow-runtime-data-id/{runtime_id}")
def latest_assessment(runtime_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    row = db.scalar(
        select(RiskAssessment).where(RiskAssessment.tenant_id == principal.tenant_id, RiskAssessment.customer_id == workflow.customer_id, RiskAssessment.current.is_(True))
    )
    if not row:
        raise nest_error(404, "assessment not found")
    return {"id": row.id, "status": "completed", "score": row.score, "tier": row.tier, "production": False}


@router.get("/external/assessments/kyb_and_ownership")
def kyb_list(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Customer).where(Customer.tenant_id == principal.tenant_id, Customer.kind == "business")).all()
    return [{"id": row.id, "companyName": row.business.legal_name if row.business else row.display_name, "status": row.canonical_status, "production": False} for row in rows]


@router.post("/external/storage")
async def upload_storage(file: UploadFile = File(...), principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    content = await file.read()
    if not content:
        raise nest_error(400, "file is required")
    if len(content) > 5_000_000:
        raise nest_error(400, "file exceeds 5MB")
    path, digest = store_bytes(ROOT / "data" / "uploads", principal.tenant_id, file.filename or "upload.bin", content)
    document = IdentityDocument(tenant_id=principal.tenant_id, category="storage", doc_type="file", storage_path=path, sha256=digest, properties={"fileName": file.filename})
    db.add(document)
    db.flush()
    return {"id": document.id, "fileName": file.filename, "sha256": digest, "provider": "clarity-local-private", "production": False}


@router.get("/collection-flow/context")
def collection_context(workflowRuntimeId: str | None = None, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, _runtime(principal, workflowRuntimeId))
    return {"context": workflow.context, "state": workflow.state, "status": workflow.status, "auth": "workflow-token" if principal.role == "end_user" else "api-key"}


@router.patch("/collection-flow/sync/context")
def collection_sync(body: dict, workflowRuntimeId: str | None = None, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    from sqlalchemy.orm.attributes import flag_modified

    workflow = _workflow(db, principal, _runtime(principal, workflowRuntimeId))
    if not isinstance(body.get("context"), dict):
        raise nest_error(400, "context is required")
    workflow.context = {**(workflow.context or {}), **body["context"]}
    flag_modified(workflow, "context")
    _bind_uploaded_files(db, workflow, workflow.context)
    return {"context": workflow.context, "state": workflow.state, "id": workflow.ballerine_runtime_id}


@router.post("/collection-flow/send-event")
def collection_event(body: dict, request: Request, workflowRuntimeId: str | None = None, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    from app.services import EVENT_ACTIONS, ApiFailure

    workflow = _workflow(db, principal, _runtime(principal, workflowRuntimeId))
    action = EVENT_ACTIONS.get(str(body.get("name") or ""))
    if not action:
        raise nest_error(400, "event name is not valid for the current state")
    tenant = _tenant(db, principal)
    try:
        advance_workflow(db, request.app.state.engine, tenant, principal, workflow, action, str((body.get("payload") or {}).get("reason") or ""))
    except ApiFailure as exc:
        raise nest_error(exc.status, (exc.body or {}).get("message", "Request failed")) from exc
    return {"state": workflow.state, "status": workflow.status}


@router.post("/collection-flow/final-submission")
def collection_finish(workflowRuntimeId: str | None = None, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, _runtime(principal, workflowRuntimeId))
    return {"workflowRuntimeId": workflow.ballerine_runtime_id, "state": workflow.state, "status": workflow.status, "submitted": workflow.state in {"approved", "rejected", "closed"}}


@router.post("/internal/workflow-definition/create-demo", status_code=201)
def create_demo(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    row = resolve_workflow_definition(db, principal.tenant_id, "kyc-demo")
    if not row:
        raise nest_error(404, "demo workflow is not seeded")
    return workflow_definition_dto(row)


def _collection_workflow(db: Session, principal: Principal, workflow_runtime_id: str | None):
    return _workflow(db, principal, _runtime(principal, workflow_runtime_id))


def _bind_uploaded_files(db: Session, workflow, context: dict) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    documents = context.get("documents")
    if not isinstance(documents, list):
        return
    for document in documents:
        if not isinstance(document, dict):
            continue
        pages = document.get("pages") if isinstance(document.get("pages"), list) else []
        for page in pages:
            if not isinstance(page, dict):
                continue
            file_id = page.get("ballerineFileId") or page.get("id")
            if not file_id:
                continue
            row = db.get(IdentityDocument, str(file_id))
            if not row or row.tenant_id != workflow.tenant_id:
                continue
            row.workflow_id = workflow.id
            row.customer_id = workflow.customer_id
            row.category = str(document.get("category") or row.category)
            row.doc_type = str(document.get("type") or row.doc_type)
            properties = dict(row.properties or {})
            if isinstance(document.get("properties"), dict):
                properties.update(document["properties"])
            side = page.get("metadata", {}).get("side") if isinstance(page.get("metadata"), dict) else None
            if side:
                properties["side"] = side
                properties["title"] = f"{row.doc_type} {side}".strip()
            row.properties = properties
            flag_modified(row, "properties")


def _stored_file(document: IdentityDocument) -> dict:
    properties = document.properties or {}
    return {
        "id": document.id,
        "ballerineFileId": document.id,
        "fileId": document.id,
        "fileName": properties.get("fileName"),
        "mimeType": properties.get("mime"),
        "uri": properties.get("uri"),
        "provider": "clarity-local-private",
        "workflowRuntimeDataId": properties.get("workflowRuntimeId"),
        "createdAt": iso(document.created_at),
    }


@router.post("/collection-flow/files")
@router.post("/collection-flow/files/old")
async def upload_collection_file(
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(journey_principal),
    db: Session = Depends(db_session),
):
    from pathlib import Path

    workflow = _collection_workflow(db, principal, None)
    content = await file.read()
    if not content:
        raise nest_error(422, "file is required")
    if len(content) > 20 * 1024 * 1024:
        raise nest_error(422, "file exceeds 20MB")
    root = Path(request.app.state.settings.upload_dir)
    path, digest = store_bytes(root, principal.tenant_id, file.filename or "document.bin", content)
    document = IdentityDocument(
        tenant_id=principal.tenant_id,
        customer_id=workflow.customer_id,
        workflow_id=workflow.id,
        category="collection_file",
        doc_type="file",
        status="provided",
        storage_path=path,
        sha256=digest,
        properties={
            "fileName": file.filename or "document.bin",
            "mime": file.content_type or "application/octet-stream",
            "uri": f"clarity-local-private://{digest}",
            "workflowRuntimeId": workflow.ballerine_runtime_id,
            "title": file.filename or "document.bin",
        },
    )
    db.add(document)
    db.flush()
    return _stored_file(document)


@router.get("/collection-flow/files")
def list_collection_files(ids: str = "", principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    workflow = _collection_workflow(db, principal, None)
    wanted = [item.strip() for item in ids.split(",") if item.strip()]
    query = select(IdentityDocument).where(IdentityDocument.tenant_id == principal.tenant_id, IdentityDocument.workflow_id == workflow.id)
    if wanted:
        query = query.where(IdentityDocument.id.in_(wanted))
    return [_stored_file(row) for row in db.scalars(query).all()]


@router.get("/collection-flow/files/{file_id}")
def get_collection_file(file_id: str, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    workflow = _collection_workflow(db, principal, None)
    document = db.get(IdentityDocument, file_id)
    if not document or document.tenant_id != principal.tenant_id or document.workflow_id != workflow.id:
        raise nest_error(404, "file not found")
    return _stored_file(document)


@router.get("/collection-flow/documents")
def collection_documents(workflowRuntimeId: str | None = None, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    workflow = _collection_workflow(db, principal, workflowRuntimeId)
    rows = db.scalars(select(IdentityDocument).where(IdentityDocument.workflow_id == workflow.id)).all()
    return [document_record(row) for row in rows]


@router.get("/collection-flow/documents/{document_id}")
def collection_document(document_id: str, workflowRuntimeId: str | None = None, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    workflow = _collection_workflow(db, principal, workflowRuntimeId)
    document = db.get(IdentityDocument, document_id)
    if not document or document.workflow_id != workflow.id:
        raise nest_error(404, "document not found")
    return document_record(document)


@router.post("/collection-flow/documents", status_code=201)
async def collection_add_document(
    metadata: str = Form(None),
    category: str = Form(...),
    type: str = Form(...),
    file: UploadFile = File(...),
    workflowRuntimeId: str | None = Form(None),
    principal: Principal = Depends(journey_principal),
    db: Session = Depends(db_session),
):
    workflow = _collection_workflow(db, principal, workflowRuntimeId)
    content = await file.read()
    if not content:
        raise nest_error(422, "file is required")
    path, digest = store_bytes(ROOT / "data" / "uploads", principal.tenant_id, file.filename or "document.bin", content)
    document = IdentityDocument(
        tenant_id=principal.tenant_id,
        customer_id=workflow.customer_id,
        workflow_id=workflow.id,
        category=category,
        doc_type=type,
        status="provided",
        storage_path=path,
        sha256=digest,
        metadata_json={"metadata": metadata} if metadata else {},
    )
    db.add(document)
    db.flush()
    return document_record(document)


@router.put("/collection-flow/documents/{document_id}")
async def collection_replace_document(
    document_id: str,
    file: UploadFile = File(...),
    workflowRuntimeId: str | None = Form(None),
    principal: Principal = Depends(journey_principal),
    db: Session = Depends(db_session),
):
    workflow = _collection_workflow(db, principal, workflowRuntimeId)
    document = db.get(IdentityDocument, document_id)
    if not document or document.workflow_id != workflow.id:
        raise nest_error(404, "document not found")
    content = await file.read()
    if not content:
        raise nest_error(422, "file is required")
    path, digest = store_bytes(ROOT / "data" / "uploads", principal.tenant_id, file.filename or "document.bin", content)
    document.storage_path = path
    document.sha256 = digest
    document.status = "provided"
    document.version = int(document.version or 1) + 1
    return document_record(document)


@router.delete("/collection-flow/documents/{document_id}")
def collection_delete_document(document_id: str, workflowRuntimeId: str | None = None, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    workflow = _collection_workflow(db, principal, workflowRuntimeId)
    document = db.get(IdentityDocument, document_id)
    if not document or document.workflow_id != workflow.id:
        raise nest_error(404, "document not found")
    db.delete(document)
    return {"id": document_id, "deleted": True}


@router.delete("/collection-flow/documents/{document_id}/files")
def collection_delete_document_files(document_id: str, workflowRuntimeId: str | None = None, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    workflow = _collection_workflow(db, principal, workflowRuntimeId)
    document = db.get(IdentityDocument, document_id)
    if not document or document.workflow_id != workflow.id:
        raise nest_error(404, "document not found")
    document.storage_path = None
    document.sha256 = None
    document.status = "requested"
    return document_record(document)


@router.post("/collection-flow/entity", status_code=201)
def collection_create_entity(body: dict, workflowRuntimeId: str | None = None, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    workflow = _collection_workflow(db, principal, workflowRuntimeId)
    entity = {"id": new_id(), **body}
    context = dict(workflow.context or {})
    context["entities"] = [*(context.get("entities") or []), entity]
    workflow.context = context
    return entity


@router.put("/collection-flow/entity/{entity_id}")
def collection_update_entity(entity_id: str, body: dict, workflowRuntimeId: str | None = None, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    workflow = _collection_workflow(db, principal, workflowRuntimeId)
    context = dict(workflow.context or {})
    entities = list(context.get("entities") or [])
    found = next((item for item in entities if item.get("id") == entity_id), None)
    if not found:
        raise nest_error(404, "entity not found")
    found.update(body)
    found["id"] = entity_id
    context["entities"] = entities
    workflow.context = context
    return found


@router.delete("/collection-flow/entity/{entity_id}")
def collection_delete_entity(entity_id: str, workflowRuntimeId: str | None = None, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    workflow = _collection_workflow(db, principal, workflowRuntimeId)
    context = dict(workflow.context or {})
    entities = [item for item in (context.get("entities") or []) if item.get("id") != entity_id]
    if len(entities) == len(context.get("entities") or []):
        raise nest_error(404, "entity not found")
    context["entities"] = entities
    workflow.context = context
    return {"id": entity_id, "deleted": True}


@router.post("/collection-flow/end-user")
def collection_update_end_user(body: dict, principal: Principal = Depends(journey_principal), db: Session = Depends(db_session)):
    if principal.role != "end_user":
        raise nest_error(401, "No EndUser is set for this token")
    customer = db.scalar(select(Customer).where(Customer.tenant_id == principal.tenant_id, Customer.external_ref == principal.user_id))
    if not customer:
        raise nest_error(404, "end user not found")
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if data.get("firstName") or data.get("lastName"):
        customer.display_name = " ".join(part for part in (data.get("firstName"), data.get("lastName")) if part)
    return {"id": customer.external_ref, "data": data}


@router.get("/collection-flow/business/business-information")
def collection_business_information(registrationNumber: str | None = None, countryCode: str | None = None):
    raise nest_error(501, "The company registry is not configured. No company record is returned.")


@router.get("/case-management/workflow-definition/{definition_id}")
def case_workflow_definition(definition_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    return workflow_definition_dto(_definition_or_404(db, principal, definition_id))


@router.post("/case-management")
def case_management_run(body: dict, request: Request, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    return run_workflow(body, request, principal, db)


@router.get("/case-management/transactions")
def case_transactions(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Transaction).where(Transaction.tenant_id == principal.tenant_id).order_by(Transaction.occurred_at.desc()).limit(100)).all()
    return [{"id": row.id, "correlationId": row.correlation_id, "amount": float(row.amount), "currency": row.currency, "date": iso(row.occurred_at)} for row in rows]


@router.get("/case-management/profiles/individuals")
def case_individuals(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Customer).where(Customer.tenant_id == principal.tenant_id, Customer.kind == "individual")).all()
    return [{"id": row.id, "correlationId": row.external_ref, "name": row.display_name} for row in rows]


@router.get("/workflow-logs/{workflow_id}")
def workflow_logs(workflow_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, workflow_id)
    rows = db.scalars(
        select(AuditEvent).where(AuditEvent.tenant_id == principal.tenant_id, AuditEvent.entity_id.in_([workflow.id, workflow.ballerine_runtime_id])).order_by(AuditEvent.created_at.desc())
    ).all()
    return {
        "data": [{"id": row.id, "type": row.action, "createdAt": iso(row.created_at), "workflowId": workflow.ballerine_runtime_id} for row in rows],
        "total": len(rows),
        "page": 1,
    }
