import json

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT
from app.deps import ballerine_principal, db_session
from app.models import Customer, IdentityDocument, Tenant, Transaction, WorkflowInstance
from app.security import Principal, verify_payload_signature
from app.services import (
    ApiFailure,
    advance_workflow,
    customer_view,
    document_record,
    evaluate_transaction,
    nest_error,
    start_customer_workflow,
    store_bytes,
    workflow_list_item,
)
from app.timeutil import iso, parse_dt, utcnow

router = APIRouter(prefix="/api/v1", tags=["ballerine"])


def _tenant(db: Session, principal: Principal) -> Tenant:
    tenant = db.get(Tenant, principal.tenant_id)
    if not tenant:
        raise nest_error(404, "Tenant not found")
    return tenant


def _workflow(db: Session, principal: Principal, runtime_id: str) -> WorkflowInstance:
    workflow = db.scalar(
        select(WorkflowInstance).where(
            WorkflowInstance.tenant_id == principal.tenant_id,
            WorkflowInstance.ballerine_runtime_id == runtime_id,
        )
    )
    if not workflow:
        raise nest_error(404, f"No resource was found for {runtime_id}")
    return workflow


def _translate(action):
    try:
        return action()
    except ApiFailure as exc:
        if exc.body and "statusCode" in exc.body:
            raise
        message = (exc.body or {}).get("message", "Request failed")
        raise nest_error(exc.status, message) from exc


@router.post("/external/workflows/run")
def run_workflow(body: dict, request: Request, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    if not body.get("workflowId"):
        raise nest_error(400, "workflowId is required")
    context = body.get("context")
    if not isinstance(context, dict) or not isinstance(context.get("entity"), dict):
        raise nest_error(400, "context.entity is required")
    entity = context["entity"]
    kind = entity.get("type")
    if kind not in {"individual", "business"}:
        raise nest_error(400, "entity.type must be individual or business")
    if not entity.get("id"):
        raise nest_error(400, "entity.id is required")
    data = entity.get("data") if isinstance(entity.get("data"), dict) else {}
    tenant = _tenant(db, principal)
    from app.catalog import resolve_workflow_definition

    found = resolve_workflow_definition(db, principal.tenant_id, str(body["workflowId"]))
    definition_key = None
    if found:
        if found.payload.get("entityType") not in {None, "any", kind}:
            raise nest_error(400, "workflow definition does not accept this entity type")
        definition_key = found.payload.get("processKey")
    customer, workflow = start_customer_workflow(
        db,
        request.app.state.engine,
        tenant,
        principal,
        kind=kind,
        external_ref=str(entity["id"]),
        data=data,
        context=context,
        config=body.get("config") if isinstance(body.get("config"), dict) else {},
        synthetic=tenant.synthetic,
        definition_key=definition_key,
        hold_at_start=str(body.get("workflowId") or "") == "kyc_collection_flow",
    )
    workflow.config = {**(workflow.config or {}), "requestedWorkflowId": body["workflowId"], "ballerineDefinitionId": found.id if found else None}
    return {
        "workflowDefinitionId": found.id if found else workflow.definition_key,
        "workflowRuntimeId": workflow.ballerine_runtime_id,
        "ballerineEntityId": customer.id,
        "entity": {"type": kind, "id": str(entity["id"])},
    }


@router.get("/external/workflows")
def list_workflows(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    rows = db.scalars(
        select(WorkflowInstance).where(WorkflowInstance.tenant_id == principal.tenant_id).order_by(WorkflowInstance.created_at.desc())
    ).all()
    results = []
    for workflow in rows:
        customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
        results.append(workflow_list_item(workflow, customer))
    pages = 1 if results else 0
    return {"results": results, "meta": {"pages": pages, "total": len(results)}}


@router.get("/external/workflows/{runtime_id}/context")
def workflow_context(runtime_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    return {"context": workflow.context}


@router.get("/external/workflows/{runtime_id}")
def get_workflow(runtime_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    return workflow_list_item(workflow, customer)


@router.post("/external/workflows/{runtime_id}/event")
@router.post("/external/workflows/{runtime_id}/send-event")
def send_event(
    runtime_id: str,
    body: dict,
    request: Request,
    principal: Principal = Depends(ballerine_principal),
    db: Session = Depends(db_session),
):
    if not isinstance(body.get("name"), str) or not body["name"]:
        raise nest_error(400, "name is required")
    workflow = _workflow(db, principal, runtime_id)
    from app.services import EVENT_ACTIONS, _advance_if_ready

    if body["name"] in {"USER_NEXT_STEP", "COLLECTION_FLOW_FINISHED"}:
        _advance_if_ready(db, request.app.state.engine, workflow)
        context = dict(workflow.context or {})
        context["events"] = [*(context.get("events") or []), body["name"]]
        workflow.context = context
        customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
        return workflow_list_item(workflow, customer)
    action = EVENT_ACTIONS.get(body["name"])
    if not action:
        raise nest_error(400, f"event {body['name']} is not valid for the current state")
    rationale = ""
    if isinstance(body.get("payload"), dict):
        rationale = str(body["payload"].get("rationale") or body["payload"].get("reason") or "")
    tenant = _tenant(db, principal)

    def _act():
        return advance_workflow(db, request.app.state.engine, tenant, principal, workflow, action, rationale)

    _translate(_act)
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    return workflow_list_item(workflow, customer)


@router.post("/external/workflows/{runtime_id}/hook/{event}")
async def workflow_hook(
    runtime_id: str,
    event: str,
    request: Request,
    db: Session = Depends(db_session),
):
    body = await request.json()
    signature = request.headers.get("x-hmac-signature", "")
    secret = request.app.state.settings.unified_api_shared_secret
    if not isinstance(body, dict) or not verify_payload_signature(secret, body, signature):
        raise nest_error(401, "Invalid signature")
    workflow = db.scalar(select(WorkflowInstance).where(WorkflowInstance.ballerine_runtime_id == runtime_id))
    if not workflow:
        raise nest_error(404, f"No resource was found for {runtime_id}")
    context = dict(workflow.context or {})
    hooks = list(context.get("hooks") or [])
    hooks.append({"event": event, "body": body})
    context["hooks"] = hooks
    workflow.context = context
    return {"received": True}


@router.post("/external/documents")
async def create_document(
    request: Request,
    metadata: str = Form(...),
    properties: str = Form(...),
    category: str = Form(...),
    type: str = Form(...),
    issuingVersion: str = Form(...),
    issuingCountry: str = Form(...),
    version: int = Form(...),
    status: str = Form(...),
    file: UploadFile = File(...),
    workflowRuntimeDataId: str | None = Form(None),
    principal: Principal = Depends(ballerine_principal),
    db: Session = Depends(db_session),
):
    if status not in {"provided", "requested"}:
        raise nest_error(400, "status must be provided or requested")
    try:
        metadata_json = json.loads(metadata)
        properties_json = json.loads(properties)
    except json.JSONDecodeError as exc:
        raise nest_error(400, "metadata and properties must be JSON strings") from exc
    content = await file.read()
    if not content:
        raise nest_error(422, "file is required")
    if len(content) > 10 * 1024 * 1024:
        raise nest_error(422, "file exceeds 10MB")
    path, digest = store_bytes(ROOT / "data" / "uploads", principal.tenant_id, file.filename or "document.bin", content)
    workflow = None
    if workflowRuntimeDataId:
        workflow = _workflow(db, principal, workflowRuntimeDataId)
    document = IdentityDocument(
        tenant_id=principal.tenant_id,
        customer_id=workflow.customer_id if workflow else None,
        workflow_id=workflow.id if workflow else None,
        category=category,
        doc_type=type,
        issuing_country=issuingCountry,
        issuing_version=issuingVersion,
        version=version,
        status=status,
        properties=properties_json if isinstance(properties_json, dict) else {"value": properties_json},
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {"value": metadata_json},
        storage_path=path,
        sha256=digest,
    )
    db.add(document)
    db.flush()
    return [document_record(document)]


@router.patch("/external/documents/{document_id}/decision")
def decide_document(
    document_id: str,
    body: dict,
    principal: Principal = Depends(ballerine_principal),
    db: Session = Depends(db_session),
):
    document = db.get(IdentityDocument, document_id)
    if not document or document.tenant_id != principal.tenant_id:
        raise nest_error(404, "document not found")
    decision = body.get("decision")
    mapping = {"approve": "approved", "reject": "rejected", "revision": "revisions", None: None}
    if decision not in mapping:
        raise nest_error(400, "decision must be approve, reject, revision or null")
    document.decision = mapping[decision]
    document.decision_reason = body.get("decisionReason")
    document.comment = body.get("comment")
    return [document_record(document)]


@router.post("/external/workflows/create-token")
def create_workflow_token(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    from datetime import timedelta
    import secrets

    from app.catalog import add_doc
    from app.security import hash_token
    from app.timeutil import iso, utcnow

    runtime_id = body.get("workflowRuntimeDataId")
    if not runtime_id:
        raise nest_error(400, "workflowRuntimeDataId is required")
    workflow = _workflow(db, principal, str(runtime_id))
    if body.get("endUserId"):
        end_user = str(body["endUserId"])
    else:
        end_user = workflow.customer_id or workflow.id
    token = secrets.token_urlsafe(32)
    days = int(body.get("expiry") or 30)
    add_doc(
        db,
        principal.tenant_id,
        "workflow_token",
        "collection",
        {
            "token_hash": hash_token(token),
            "token": token,
            "workflow_runtime_id": workflow.ballerine_runtime_id,
            "end_user_id": end_user,
            "expires_at": iso(utcnow() + timedelta(days=days)),
        },
        parent_id=workflow.id,
    )
    return {"token": token}


@router.post("/external/workflows/create-collection-flow-url")
def create_collection_flow_url(body: dict, request: Request, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    from sqlalchemy import select

    from app.models import ConfigDocument

    runtime_id = body.get("workflowRuntimeDataId")
    if not runtime_id:
        raise nest_error(400, "workflowRuntimeDataId is required")
    workflow = _workflow(db, principal, str(runtime_id))
    row = db.scalar(
        select(ConfigDocument)
        .where(ConfigDocument.tenant_id == principal.tenant_id, ConfigDocument.kind == "workflow_token", ConfigDocument.parent_id == workflow.id)
        .order_by(ConfigDocument.created_at.desc())
    )
    if not row:
        raise nest_error(404, f"No workflow token was found for {runtime_id}")
    base = request.app.state.settings.public_app_url.rstrip("/")
    return {"collectionFlowUrl": f"{base}/parcours?workflowId={workflow.ballerine_runtime_id}&token={row.payload['token']}"}


@router.patch("/external/workflows/{runtime_id}")
@router.post("/external/workflows/{runtime_id}")
def update_workflow(runtime_id: str, body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    from sqlalchemy.orm.attributes import flag_modified

    workflow = _workflow(db, principal, runtime_id)
    context = dict(workflow.context or {})
    if isinstance(body.get("context"), dict):
        context.update(body["context"])
    if isinstance(body.get("tags"), list):
        context["tags"] = body["tags"]
    if isinstance(body.get("state"), str) and body["state"]:
        context["collectionState"] = body["state"]
    workflow.context = context
    flag_modified(workflow, "context")
    workflow.updated_at = utcnow()
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    return workflow_list_item(workflow, customer)


@router.post("/external/kyc")
def initiate_kyc(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    required = ("endUserId", "workflowRuntimeDataId", "vendor", "language")
    missing = [field for field in required if not body.get(field)]
    if missing:
        raise nest_error(400, "missing fields: " + ", ".join(missing))
    if body.get("vendor") != "veriff":
        raise nest_error(400, "vendor must be veriff")
    workflow = _workflow(db, principal, str(body["workflowRuntimeDataId"]))
    if not workflow.customer_id:
        raise nest_error(404, "end user not found")
    from app.providers import create_veriff_session, veriff_configured

    if not veriff_configured():
        raise nest_error(
            501,
            "Identity vendor 'veriff' is not configured. No verification session was created and no email was sent.",
        )
    customer = db.get(Customer, workflow.customer_id)
    profile = customer.individual if customer else None
    try:
        session = create_veriff_session(
            profile.given_name if profile else "",
            profile.family_name if profile else "",
            str(body["endUserId"]),
        )
    except Exception as exc:
        raise nest_error(502, "Veriff did not create a session. No local session URL was invented.") from exc
    return session


@router.post("/external/transactions", status_code=201)
def create_transaction(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    required = ("date", "amount", "currency", "baseAmount", "baseCurrency", "correlationId", "originator", "beneficiary")
    missing = [field for field in required if body.get(field) in (None, "")]
    if missing:
        raise nest_error(400, "missing fields: " + ", ".join(missing))
    for side in ("originator", "beneficiary"):
        if not isinstance(body[side], dict) or not body[side].get("correlationId"):
            raise nest_error(400, f"{side}.correlationId is required")
    try:
        occurred = parse_dt(str(body["date"]))
    except ValueError as exc:
        raise nest_error(400, "date must be a date-time") from exc
    existing = db.scalar(
        select(Transaction).where(
            Transaction.tenant_id == principal.tenant_id,
            Transaction.correlation_id == str(body["correlationId"]),
        )
    )
    if existing:
        return {"id": existing.id, "correlationId": existing.correlation_id}
    originator = body["originator"]
    beneficiary = body["beneficiary"]
    customer = db.scalar(
        select(Customer).where(Customer.tenant_id == principal.tenant_id, Customer.external_ref == str(originator["correlationId"]))
    )
    counterparty = ""
    country = originator.get("bankCountry") or beneficiary.get("bankCountry")
    end_user = beneficiary.get("endUserData") if isinstance(beneficiary.get("endUserData"), dict) else {}
    business = beneficiary.get("businessData") if isinstance(beneficiary.get("businessData"), dict) else {}
    counterparty = end_user.get("firstName") or business.get("companyName") or beneficiary.get("correlationId")
    tx = Transaction(
        tenant_id=principal.tenant_id,
        customer_id=customer.id if customer else None,
        correlation_id=str(body["correlationId"]),
        amount=float(body["amount"]),
        currency=str(body["currency"]),
        base_amount=float(body["baseAmount"]),
        base_currency=str(body["baseCurrency"]),
        direction=body.get("direction"),
        country=country,
        counterparty_name=str(counterparty) if counterparty else None,
        description=body.get("description"),
        occurred_at=occurred,
        payload=body,
        synthetic=False,
    )
    db.add(tx)
    db.flush()
    tenant = _tenant(db, principal)
    evaluate_transaction(db, tenant, tx)
    return {"id": tx.id, "correlationId": tx.correlation_id}


@router.post("/external/transactions/alt", status_code=201)
def create_transaction_alt(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    data = body.get("data") if isinstance(body.get("data"), dict) else None
    if not data:
        raise nest_error(400, "data is required")
    required = (
        "tx_date_time",
        "tx_amount",
        "tx_currency",
        "tx_base_amount",
        "tx_base_currency",
        "tx_id",
        "tx_product",
        "counterparty_id",
        "counterparty_institution_id",
        "counterparty_name",
        "customer_id",
        "customer_name",
        "customer_address",
        "customer_country",
        "customer_type",
    )
    missing = [field for field in required if data.get(field) in (None, "")]
    if missing:
        raise nest_error(400, "missing fields: " + ", ".join(missing))
    translated = {
        "date": data["tx_date_time"],
        "amount": data["tx_amount"],
        "currency": data["tx_currency"],
        "baseAmount": data["tx_base_amount"],
        "baseCurrency": data["tx_base_currency"],
        "correlationId": data["tx_id"],
        "direction": data.get("tx_direction"),
        "description": data.get("tx_reference_text"),
        "originator": {"correlationId": data["customer_id"], "bankCountry": data["customer_country"]},
        "beneficiary": {"correlationId": data["counterparty_id"], "bankCountry": data.get("customer_country")},
    }
    return create_transaction(translated, principal, db)


@router.get("/external/transactions")
def list_transactions(principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Transaction).where(Transaction.tenant_id == principal.tenant_id).order_by(Transaction.occurred_at.desc())).all()
    return {
        "results": [
            {
                "id": row.id,
                "correlationId": row.correlation_id,
                "amount": float(row.amount),
                "currency": row.currency,
                "date": iso(row.occurred_at),
            }
            for row in rows
        ],
        "meta": {"total": len(rows)},
    }


@router.get("/external/documents/tracker/{runtime_id}")
def document_tracker(runtime_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    documents = db.scalars(select(IdentityDocument).where(IdentityDocument.workflow_id == workflow.id)).all()
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    records = [document_record(document) for document in documents]
    if customer and customer.kind == "business":
        return {"business": records, "individuals": {"ubos": [], "directors": []}}
    return {"business": [], "individuals": {"ubos": records, "directors": []}}


def _documents_for(db: Session, principal: Principal, workflow: WorkflowInstance, entity_ids: list[str] | None = None) -> list[IdentityDocument]:
    rows = db.scalars(select(IdentityDocument).where(IdentityDocument.tenant_id == principal.tenant_id, IdentityDocument.workflow_id == workflow.id)).all()
    if not entity_ids:
        return list(rows)
    allowed = set(entity_ids)
    if workflow.customer_id:
        customer = db.get(Customer, workflow.customer_id)
        if customer and (customer.id in allowed or customer.external_ref in allowed):
            return list(rows)
    return [row for row in rows if str((row.metadata_json or {}).get("entityId") or "") in allowed]


@router.post("/external/documents/request-upload")
def request_document_upload(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    workflow_id = body.get("workflowId")
    documents = body.get("documents")
    if not workflow_id or not isinstance(documents, list) or not documents:
        raise nest_error(400, "workflowId and documents are required")
    workflow = _workflow(db, principal, str(workflow_id))
    created = []
    for item in documents:
        if not item.get("type") or not item.get("category"):
            raise nest_error(400, "each document needs type and category")
        document = IdentityDocument(
            tenant_id=principal.tenant_id,
            customer_id=workflow.customer_id,
            workflow_id=workflow.id,
            category=str(item["category"]),
            doc_type=str(item["type"]),
            issuing_country=str(item.get("issuer", {}).get("country") or item.get("issuingCountry") or "ZZ"),
            issuing_version=str(item.get("issuingVersion") or "1"),
            status="requested",
            metadata_json={"entityId": item.get("entityId")} if item.get("entityId") else {},
        )
        db.add(document)
        created.append(document)
    db.flush()
    return [document_record(document) for document in created]


@router.get("/external/documents/by-entity-ids/{entity_ids}/{runtime_id}")
def documents_by_entities(entity_ids: str, runtime_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    return [document_record(document) for document in _documents_for(db, principal, workflow, entity_ids.split(","))]


@router.get("/external/documents/{entity_id}/{runtime_id}")
def documents_by_entity(entity_id: str, runtime_id: str, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    workflow = _workflow(db, principal, runtime_id)
    return [document_record(document) for document in _documents_for(db, principal, workflow, [entity_id])]


@router.patch("/external/documents/decision/batch")
def decide_documents_batch(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise nest_error(400, "ids are required")
    return [decide_document(str(item), {"decision": body.get("decision")}, principal, db)[0] for item in ids]


@router.patch("/external/documents/{document_id}")
def update_document(document_id: str, body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    document = db.get(IdentityDocument, document_id)
    if not document or document.tenant_id != principal.tenant_id:
        raise nest_error(404, "document not found")
    if body.get("category"):
        document.category = str(body["category"])
    if body.get("type"):
        document.doc_type = str(body["type"])
    if body.get("status") in {"provided", "requested"}:
        document.status = body["status"]
    if isinstance(body.get("properties"), dict):
        document.properties = body["properties"]
    return [document_record(document)]


@router.post("/external/documents/{runtime_id}/{file_id}")
async def reupload_document_file(
    runtime_id: str,
    file_id: str,
    file: UploadFile = File(...),
    principal: Principal = Depends(ballerine_principal),
    db: Session = Depends(db_session),
):
    workflow = _workflow(db, principal, runtime_id)
    document = db.get(IdentityDocument, file_id)
    if not document or document.tenant_id != principal.tenant_id or document.workflow_id != workflow.id:
        raise nest_error(404, "document not found")
    content = await file.read()
    if not content:
        raise nest_error(422, "file is required")
    path, digest = store_bytes(ROOT / "data" / "uploads", principal.tenant_id, file.filename or "document.bin", content)
    document.storage_path = path
    document.sha256 = digest
    document.status = "provided"
    document.version = int(document.version or 1) + 1
    return [document_record(document)]


@router.delete("/external/documents")
def delete_documents(body: dict, principal: Principal = Depends(ballerine_principal), db: Session = Depends(db_session)):
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise nest_error(400, "ids are required")
    removed = []
    for item in ids:
        document = db.get(IdentityDocument, str(item))
        if document and document.tenant_id == principal.tenant_id:
            removed.append(document_record(document))
            db.delete(document)
    return removed


@router.get("/_health/live", status_code=204)
def health_live():
    return Response(status_code=204)


@router.get("/_health/ready", status_code=204)
def health_ready(db: Session = Depends(db_session)):
    db.scalar(select(Tenant).limit(1))
    return Response(status_code=204)
