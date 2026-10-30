import json
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import ROOT
from app.deps import clarity_principal, db_session
from app.ids import DEMO_PASSWORD
from app.models import (
    Alert,
    AuditEvent,
    CaseComment,
    CaseRecord,
    Customer,
    Evidence,
    MonitoringRule,
    Notification,
    ProviderConnection,
    RegulatoryReport,
    RiskAssessment,
    ScreeningMatch,
    Tenant,
    Transaction,
    User,
    WorkflowInstance,
    WorkflowTask,
)
from app.search import present
from app.security import Principal, encode_jwt, parse_roles, serialize_roles, verify_password
from app.services import (
    ApiFailure,
    actor_label,
    advance_workflow,
    assess_customer,
    audit,
    backtest,
    clarity_error,
    customer_view,
    disposition_match,
    evaluate_transaction,
    start_customer_workflow,
    store_bytes,
)
from app.timeutil import as_utc, iso, parse_dt, utcnow

router = APIRouter(prefix="/api/clarity/v1", tags=["clarity"])


class LoginBody(BaseModel):
    email: str
    password: str


class IndividualBody(BaseModel):
    given_name: str
    family_name: str
    date_of_birth: str
    nationality: str
    residence_country: str
    address: str
    email: str
    phone: str
    purpose: str
    expected_activity: str
    source_of_funds: str
    external_ref: str | None = None


class PartyBody(BaseModel):
    full_name: str
    role: str = "beneficial_owner"
    ownership_pct: float | None = None
    nationality: str | None = None
    date_of_birth: str | None = None


class BusinessBody(BaseModel):
    legal_name: str
    trading_name: str | None = None
    registration_number: str
    jurisdiction: str
    legal_form: str
    registered_address: str
    industry: str
    activities: str
    purpose: str
    expected_activity: str
    source_of_funds: str
    directors: list[PartyBody] = Field(default_factory=list)
    beneficial_owners: list[PartyBody] = Field(default_factory=list)
    external_ref: str | None = None


class ActionBody(BaseModel):
    action: str
    rationale: str = ""


class DispositionBody(BaseModel):
    status: str
    rationale: str = ""


class TransactionBody(BaseModel):
    customer_id: str
    amount: float
    currency: str = "EUR"
    country: str
    counterparty_name: str
    direction: str = "outbound"
    description: str = ""
    occurred_at: str | None = None
    synthetic: bool = True


class NoteBody(BaseModel):
    comment: str


class ReportBody(BaseModel):
    report_type: str = "tracfin_preparation"
    narrative: str


class RuleBody(BaseModel):
    name: str
    scenario: str
    parameters: dict = Field(default_factory=dict)
    description: str = ""


class BacktestBody(BaseModel):
    rule_id: str
    start: str
    end: str


class OverrideBody(BaseModel):
    risk_level: int
    rationale: str


def _tenant(db: Session, principal: Principal) -> Tenant:
    tenant = db.get(Tenant, principal.tenant_id)
    if not tenant:
        raise clarity_error(404, "not_found", "Tenant introuvable.")
    return tenant


def _engine(request: Request):
    return request.app.state.engine


@router.get("/health")
def health(request: Request):
    engine = request.app.state.engine
    return {
        "status": "ok",
        "service": "clarity",
        "workflow_engine": engine.mode,
        "flowable_error": request.app.state.flowable_error,
        "synthetic_data": True,
        "production_screening": False,
        "production_identity_verification": False,
    }


@router.get("/auth/mode")
def auth_mode(request: Request):
    return {"mode": request.app.state.settings.resolved_auth_mode}


@router.post("/auth/login")
def login(body: LoginBody, request: Request, db: Session = Depends(db_session)):
    user = db.scalar(select(User).where(User.email == body.email.strip(), User.active.is_(True)))
    if not user or not verify_password(body.password, user.password_hash):
        raise clarity_error(401, "unauthorized", "Identifiants invalides.")
    token = encode_jwt(
        request.app.state.settings.jwt_secret,
        {"tenant_id": user.tenant_id, "email": user.email, "role": user.role},
        request.app.state.settings.jwt_ttl_minutes,
    )
    audit(db, user.tenant_id, user.email, "auth.login", "user", user.id, {"mfa_enabled": user.mfa_enabled})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "name": user.full_name, "role": user.role, "mfa_enabled": user.mfa_enabled},
        "demo_password_hint": DEMO_PASSWORD if request.app.state.settings.env == "development" else None,
    }


@router.get("/auth/oidc")
def oidc_status(request: Request):
    from app.oidc import enabled

    return {"enabled": enabled(request.app.state.settings)}


@router.get("/auth/oidc/start")
def oidc_start(request: Request):
    from app.oidc import authorization_redirect, enabled

    settings = request.app.state.settings
    if not enabled(settings):
        raise clarity_error(404, "oidc_disabled", "OpenID n'est pas configuré.")
    try:
        return RedirectResponse(authorization_redirect(settings), status_code=302)
    except ApiFailure as exc:
        target = f"{settings.public_app_url.rstrip('/')}/?oidc_error=unavailable"
        if exc.status < 500:
            raise
        return RedirectResponse(target, status_code=302)


@router.get("/auth/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "", db: Session = Depends(db_session)):
    from app.oidc import identity_from_callback, provision_oidc_user

    settings = request.app.state.settings
    refused = RedirectResponse(f"{settings.public_app_url.rstrip('/')}/?oidc_error=rejected", status_code=302)
    if not code or not state:
        return refused
    try:
        claims = identity_from_callback(settings, code, state)
        user = provision_oidc_user(db, claims)
    except ApiFailure:
        return refused
    token = encode_jwt(
        settings.jwt_secret,
        {"tenant_id": user.tenant_id, "email": user.email, "role": user.role},
        settings.jwt_ttl_minutes,
    )
    audit(db, user.tenant_id, user.email, "auth.oidc", "user", user.id, {"role": user.role})
    return RedirectResponse(f"{settings.public_app_url.rstrip('/')}/#session={token}", status_code=302)


def _user_view(row: User) -> dict:
    return {
        "id": row.id,
        "email": row.email,
        "name": row.full_name,
        "role": row.role,
        "roles": parse_roles(row.role),
        "mfa_enabled": row.mfa_enabled,
        "active": row.active,
    }


@router.get("/me")
def me(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    user = db.get(User, principal.user_id)
    view = _user_view(user)
    view["tenant_id"] = user.tenant_id
    return view


def _customer_name(db: Session, customer_id: str | None) -> str | None:
    if not customer_id:
        return None
    customer = db.get(Customer, customer_id)
    return customer.display_name if customer else None


@router.get("/dashboard")
def dashboard(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    tenant_id = principal.tenant_id
    def count(model, *filters):
        return db.scalar(select(func.count()).select_from(model).where(model.tenant_id == tenant_id, *filters)) or 0

    return {
        "synthetic": True,
        "customers": count(Customer),
        "individuals": count(Customer, Customer.kind == "individual"),
        "businesses": count(Customer, Customer.kind == "business"),
        "in_review": count(Customer, Customer.canonical_status == "in_review"),
        "individuals_in_review": count(Customer, Customer.kind == "individual", Customer.canonical_status == "in_review"),
        "businesses_in_review": count(Customer, Customer.kind == "business", Customer.canonical_status == "in_review"),
        "kyc_sessions": count(WorkflowInstance, WorkflowInstance.definition_key == "clarityKycIndividual"),
        "kyb_sessions": count(WorkflowInstance, WorkflowInstance.definition_key == "clarityKybBusiness"),
        "open_alerts": count(Alert, Alert.status == "open"),
        "open_cases": count(CaseRecord, CaseRecord.status != "closed"),
        "open_tasks": count(WorkflowTask, WorkflowTask.status == "open"),
        "transactions": count(Transaction),
        "notice": "Les données de démonstration sont synthétiques. Les correspondances de criblage ne proviennent pas d'un prestataire réel.",
    }


@router.get("/customers")
def list_customers(
    kind: str | None = None,
    q: str = "",
    status: str = "",
    name: str = "",
    risk: str = "",
    sort: str = "display_name",
    direction: str = "asc",
    limit: int | None = None,
    offset: int | None = None,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    from app.services import kyc_cycle_view, latest_kyc_cycle, refresh_kyc_cycles

    refresh_kyc_cycles(db, principal.tenant_id)
    query = select(Customer).where(Customer.tenant_id == principal.tenant_id)
    if kind:
        query = query.where(Customer.kind == kind)
    rows = db.scalars(query).all()
    listed = []
    for row in rows:
        cycle = latest_kyc_cycle(db, row.id)
        view = kyc_cycle_view(cycle) if cycle else None
        listed.append(
            {
                "id": row.id,
                "kind": row.kind,
                "display_name": row.display_name,
                "status": row.canonical_status,
                "kyc_status": view["status"] if view else "",
                "kyc_label": view["label"] if view else "—",
                "risk_tier": row.risk_tier,
                "synthetic": row.synthetic,
                "external_ref": row.external_ref,
                "kyc_created_at": view["created_at"] if view else None,
                "kyc_validated_at": view["validated_at"] if view else None,
                "kyc_expires_at": view["expires_at"] if view else None,
                "reminder": view["reminder"] if view else "none",
                "reminded_at": view["reminded_at"] if view else None,
            }
        )
    if status:
        wanted = {item.strip() for item in status.split(",") if item.strip()}
        if "pending" in wanted:
            wanted.add("overdue")
        listed = [
            row
            for row in listed
            if row["kyc_status"] in wanted or ("rejected" in wanted and row["status"] == "rejected")
        ]
    if name:
        listed = [row for row in listed if name.lower() in row["display_name"].lower()]
    if risk:
        wanted = {item.strip() for item in risk.split(",") if item.strip()}
        listed = [row for row in listed if (row["risk_tier"] or "") in wanted]
    sort_key = sort if sort in {"display_name", "kyc_label", "risk_tier", "kyc_created_at", "kyc_validated_at", "kyc_expires_at"} else "display_name"
    descending = direction.strip().lower() == "desc"

    def sort_value(row: dict):
        value = row.get(sort_key)
        if value is None or value == "":
            return ("",) if descending else ("\uffff",)
        if sort_key.endswith("_at"):
            return (str(value),)
        return (str(value).casefold(),)

    listed.sort(key=sort_value, reverse=descending)
    return present(listed, q, lambda row: {"name": row["display_name"], "status": row["kyc_label"], "kind": row["kind"], "risk": row["risk_tier"] or ""}, limit, offset)


@router.post("/customers/{customer_id}/verifications/{cycle_id}/remind")
async def remind_customer_kyc(customer_id: str, cycle_id: str, request: Request, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    from app.services import kyc_cycle_view, remind_kyc_cycle

    customer = db.get(Customer, customer_id)
    if not customer or customer.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Client introuvable.")
    document_ids: list[str] = []
    raw = await request.body()
    if raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise clarity_error(400, "invalid", "Corps de requête illisible.") from exc
        chosen = payload.get("document_ids") or []
        if not isinstance(chosen, list) or any(not isinstance(item, str) or not item for item in chosen):
            raise clarity_error(400, "invalid", "Les pièces à demander sont invalides.")
        document_ids = chosen
    row = remind_kyc_cycle(db, customer, cycle_id, document_ids)
    audit(db, principal.tenant_id, actor_label(principal), "kyc.remind", "kyc_cycle", row.id, {"reminder": "manual", "documents": document_ids})
    return kyc_cycle_view(row)


@router.get("/customers/{customer_id}")
def get_customer(customer_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    customer = db.get(Customer, customer_id)
    if not customer or customer.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Client introuvable.")
    audit(db, principal.tenant_id, actor_label(principal), "customer.read", "customer", customer.id, {})
    return customer_view(db, customer)


def _onboard(db, request, principal, kind, data, external_ref):
    tenant = _tenant(db, principal)
    customer, workflow = start_customer_workflow(
        db,
        _engine(request),
        tenant,
        principal,
        kind=kind,
        external_ref=external_ref,
        data=data,
        context={"entity": {"type": kind, "id": external_ref, "data": data}, "source": "clarity"},
        config={},
        synthetic=True,
    )
    return {"customer": customer_view(db, customer), "workflow_id": workflow.id, "state": workflow.state, "engine": workflow.engine}


@router.post("/onboarding/individuals")
def onboard_individual(
    body: IndividualBody,
    request: Request,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    data = body.model_dump()
    data["firstName"] = body.given_name
    data["lastName"] = body.family_name
    data["legal_name"] = f"{body.given_name} {body.family_name}"
    return _onboard(db, request, principal, "individual", data, body.external_ref)


@router.post("/onboarding/businesses")
def onboard_business(
    body: BusinessBody,
    request: Request,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    data = body.model_dump()
    data["companyName"] = body.legal_name
    data["directors"] = [item.model_dump() for item in body.directors]
    data["beneficial_owners"] = [item.model_dump() for item in body.beneficial_owners]
    return _onboard(db, request, principal, "business", data, body.external_ref)


@router.get("/workflows")
def workflows(
    kind: str | None = None,
    q: str = "",
    limit: int | None = None,
    offset: int | None = None,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    rows = db.scalars(select(WorkflowInstance).where(WorkflowInstance.tenant_id == principal.tenant_id).order_by(WorkflowInstance.created_at.desc())).all()
    listed = []
    for row in rows:
        if kind and row.definition_key != {"individual": "clarityKycIndividual", "business": "clarityKybBusiness"}.get(kind, kind):
            continue
        customer = db.get(Customer, row.customer_id) if row.customer_id else None
        task = db.scalar(select(WorkflowTask).where(WorkflowTask.workflow_id == row.id, WorkflowTask.status == "open"))
        listed.append(
            {
                "id": row.id,
                "ballerine_runtime_id": row.ballerine_runtime_id,
                "customer_id": row.customer_id,
                "customer_name": customer.display_name if customer else None,
                "customer_kind": customer.kind if customer else None,
                "definition": row.definition_name,
                "definition_key": row.definition_key,
                "engine": row.engine,
                "status": row.status,
                "state": row.state,
                "task": task.name if task else None,
                "updated_at": iso(row.updated_at),
            }
        )
    return present(
        listed,
        q,
        lambda row: {"name": row["customer_name"] or "", "status": row["status"] or "", "state": row["state"] or "", "definition": row["definition"] or ""},
        limit,
        offset,
    )


@router.get("/tasks")
def tasks(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(
        select(WorkflowTask).where(WorkflowTask.tenant_id == principal.tenant_id, WorkflowTask.status == "open").order_by(WorkflowTask.created_at.asc())
    ).all()
    return [
        {
            "id": row.id,
            "workflow_id": row.workflow_id,
            "task_key": row.task_key,
            "name": row.name,
            "due_at": iso(row.due_at),
        }
        for row in rows
    ]


@router.post("/workflows/{workflow_id}/actions")
def workflow_action(
    workflow_id: str,
    body: ActionBody,
    request: Request,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    workflow = db.get(WorkflowInstance, workflow_id)
    if not workflow or workflow.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Workflow introuvable.")
    advance_workflow(db, _engine(request), _tenant(db, principal), principal, workflow, body.action, body.rationale)
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    return {"state": workflow.state, "status": workflow.status, "customer": customer_view(db, customer) if customer else None}


@router.get("/screening/matches")
def screening_matches(
    q: str = "",
    limit: int | None = None,
    offset: int | None = None,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    rows = db.scalars(select(ScreeningMatch).where(ScreeningMatch.tenant_id == principal.tenant_id).order_by(ScreeningMatch.created_at.desc())).all()
    listed = [
        {
            "id": row.id,
            "status": row.status,
            "score": row.score,
            "entity_id": row.entity_id,
            "name": ((row.payload.get("properties") or {}).get("name") or [row.entity_id])[0],
            "topics": (row.payload.get("properties") or {}).get("topics") or [],
            "datasets": row.payload.get("datasets") or [],
            "explanation": row.explanation,
            "note": ((row.payload.get("properties") or {}).get("notes") or [""])[0],
            "synthetic": bool(row.payload.get("synthetic")),
            "provider": row.payload.get("provider"),
            "screening_id": row.screening_id,
        }
        for row in rows
    ]
    return present(listed, q, lambda row: {"name": row["name"], "status": row["status"], "provider": row["provider"] or "", "entity": row["entity_id"]}, limit, offset)


@router.post("/screening/matches/{match_id}/disposition")
def disposition(
    match_id: str,
    body: DispositionBody,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    if not principal.allows("analyst", "investigator", "checker"):
        raise clarity_error(403, "forbidden", "Disposition réservée aux analystes.")
    match = db.get(ScreeningMatch, match_id)
    if not match or match.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Correspondance introuvable.")
    try:
        disposition_match(db, _tenant(db, principal), principal, match, body.status, False)
    except ApiFailure:
        raise
    except Exception as exc:  # noqa: BLE001
        raise clarity_error(400, "invalid", str(exc)) from exc
    return {"id": match.id, "status": match.status, "explanation": match.explanation}


@router.get("/risk")
def risk_list(q: str = "", limit: int | None = None, offset: int | None = None, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(
        select(RiskAssessment).where(RiskAssessment.tenant_id == principal.tenant_id, RiskAssessment.current.is_(True)).order_by(RiskAssessment.created_at.desc())
    ).all()
    listed = [
        {
            "id": row.id,
            "customer_id": row.customer_id,
            "customer_name": _customer_name(db, row.customer_id),
            "object_id": row.object_id,
            "methodology_version": row.methodology_version,
            "source": row.source,
            "score": row.score,
            "tier": row.tier,
            "risk_level": row.risk_level,
            "factors": row.factors,
            "rationale": row.rationale,
        }
        for row in rows
    ]
    return present(listed, q, lambda row: {"name": row["customer_name"] or "", "tier": row["tier"], "source": row["source"]}, limit, offset)


@router.post("/risk/{assessment_id}/override")
def override_risk(
    assessment_id: str,
    body: OverrideBody,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    if not principal.allows("checker"):
        raise clarity_error(403, "forbidden", "Un checker doit approuver le dépassement de score.")
    if body.risk_level < 1 or len(body.rationale.strip()) < 8:
        raise clarity_error(400, "invalid", "Niveau et justification sont requis.")
    current = db.get(RiskAssessment, assessment_id)
    if not current or current.tenant_id != principal.tenant_id or not current.current:
        raise clarity_error(404, "not_found", "Évaluation introuvable.")
    current.current = False
    current.stale_at = utcnow()
    row = RiskAssessment(
        tenant_id=principal.tenant_id,
        customer_id=current.customer_id,
        object_type=current.object_type,
        object_id=current.object_id,
        source="override",
        score=current.score,
        risk_level=body.risk_level,
        tier=current.tier,
        factors=current.factors,
        current=True,
        overridden_by=principal.user_id,
        rationale=body.rationale,
    )
    db.add(row)
    audit(db, principal.tenant_id, actor_label(principal), "risk.override", "risk_assessment", current.id, {"rationale": body.rationale})
    db.flush()
    return {"id": row.id, "risk_level": row.risk_level, "source": row.source, "rationale": row.rationale}


@router.get("/transactions")
def transactions(q: str = "", limit: int | None = None, offset: int | None = None, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Transaction).where(Transaction.tenant_id == principal.tenant_id).order_by(Transaction.occurred_at.desc())).all()
    listed = [
        {
            "id": row.id,
            "customer_id": row.customer_id,
            "customer_name": _customer_name(db, row.customer_id),
            "correlation_id": row.correlation_id,
            "amount": float(row.amount),
            "currency": row.currency,
            "country": row.country,
            "counterparty_name": row.counterparty_name,
            "direction": row.direction,
            "occurred_at": iso(row.occurred_at),
            "synthetic": row.synthetic,
        }
        for row in rows
    ]
    return present(listed, q, lambda row: {"name": row["customer_name"] or "", "country": row["country"] or "", "direction": row["direction"] or "", "counterparty": row["counterparty_name"] or "", "currency": row["currency"]}, limit, offset)


@router.post("/transactions")
def create_transaction(
    body: TransactionBody,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    customer = db.get(Customer, body.customer_id)
    if not customer or customer.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Client introuvable.")
    occurred = parse_dt(body.occurred_at) if body.occurred_at else utcnow()
    tx = Transaction(
        tenant_id=principal.tenant_id,
        customer_id=customer.id,
        correlation_id=f"clarity-{customer.id[:8]}-{int(occurred.timestamp())}",
        amount=body.amount,
        currency=body.currency,
        base_amount=body.amount,
        base_currency="EUR",
        direction=body.direction,
        country=body.country,
        counterparty_name=body.counterparty_name,
        description=body.description,
        occurred_at=occurred,
        payload={"source": "clarity", "synthetic": body.synthetic},
        synthetic=body.synthetic,
    )
    db.add(tx)
    db.flush()
    alerts = evaluate_transaction(db, _tenant(db, principal), tx)
    return {"id": tx.id, "alerts": [alert.id for alert in alerts], "note": "Les alertes signalent un schéma à investiguer."}


@router.get("/alerts")
def alerts(q: str = "", limit: int | None = None, offset: int | None = None, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Alert).where(Alert.tenant_id == principal.tenant_id).order_by(Alert.created_at.desc())).all()
    listed = [
        {
            "id": row.id,
            "title": row.title,
            "priority": row.priority,
            "status": row.status,
            "customer_id": row.customer_id,
            "customer_name": _customer_name(db, row.customer_id),
            "case_id": row.case_id,
            "rationale": row.rationale,
            "suppressed_count": row.suppressed_count,
            "disposition": row.disposition,
            "created_at": iso(row.created_at),
        }
        for row in rows
    ]
    return present(listed, q, lambda row: {"title": row["title"], "status": row["status"], "name": row["customer_name"] or "", "priority": row["priority"]}, limit, offset)


@router.post("/alerts/{alert_id}/disposition")
def disposition_alert(
    alert_id: str,
    body: DispositionBody,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    if not principal.allows("analyst", "investigator", "checker"):
        raise clarity_error(403, "forbidden", "Disposition réservée à la conformité.")
    alert = db.get(Alert, alert_id)
    if not alert or alert.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Alerte introuvable.")
    if body.status not in {"false_positive", "valuable_alert", "escalated"}:
        raise clarity_error(400, "invalid", "Statut de disposition inconnu.")
    alert.disposition = body.status
    alert.status = "closed" if body.status != "escalated" else "open"
    alert.rationale = body.rationale or alert.rationale
    audit(db, principal.tenant_id, actor_label(principal), "alert.disposition", "alert", alert.id, {"status": body.status})
    if alert.customer_id and body.status in {"valuable_alert", "escalated", "false_positive"}:
        customer = db.get(Customer, alert.customer_id)
        if customer:
            assess_customer(db, _tenant(db, principal), customer, source="ruleset")
    return {"id": alert.id, "status": alert.status, "disposition": alert.disposition}


@router.post("/monitoring/backtests")
def run_backtest(body: BacktestBody, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    return backtest(db, _tenant(db, principal), body.rule_id, parse_dt(body.start), parse_dt(body.end))


@router.get("/cases")
def cases(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(CaseRecord).where(CaseRecord.tenant_id == principal.tenant_id).order_by(CaseRecord.created_at.desc())).all()
    now = utcnow()
    payload = []
    for row in rows:
        sla = "on_time"
        if row.due_at and row.status != "closed":
            due = as_utc(row.due_at)
            if due < now:
                sla = "breached"
            elif due < now + timedelta(days=1):
                sla = "due_soon"
        payload.append(
            {
                "id": row.id,
                "name": row.name,
                "status": row.status,
                "outcome": row.outcome,
                "customer_id": row.customer_id,
                "escalated": row.escalated,
                "sla": sla,
                "due_at": iso(row.due_at),
                "created_at": iso(row.created_at),
            }
        )
    return payload


@router.get("/cases/{case_id}")
def case_detail(case_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    case = db.get(CaseRecord, case_id)
    if not case or case.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Dossier introuvable.")
    notes = db.scalars(select(CaseComment).where(CaseComment.case_id == case.id).order_by(CaseComment.created_at.asc())).all()
    reports = db.scalars(select(RegulatoryReport).where(RegulatoryReport.case_id == case.id)).all()
    evidence = db.scalars(select(Evidence).where(Evidence.case_id == case.id)).all()
    alert = db.scalar(select(Alert).where(Alert.case_id == case.id))
    transactions = []
    if alert and alert.transaction_id:
        tx = db.get(Transaction, alert.transaction_id)
        if tx:
            transactions.append({"id": tx.id, "amount": float(tx.amount), "currency": tx.currency, "occurred_at": iso(tx.occurred_at), "country": tx.country})
    return {
        "id": case.id,
        "name": case.name,
        "status": case.status,
        "outcome": case.outcome,
        "customer_id": case.customer_id,
        "escalated": case.escalated,
        "notes": [{"id": note.id, "comment": note.comment, "created_at": iso(note.created_at)} for note in notes],
        "evidence": [{"id": item.id, "file_name": item.file_name, "sha256": item.sha256} for item in evidence],
        "reports": [
            {"id": item.id, "type": item.report_type, "status": item.status, "submission_channel": item.submission_channel}
            for item in reports
        ],
        "transactions": transactions,
    }


@router.post("/cases/{case_id}/notes")
def add_note(case_id: str, body: NoteBody, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    case = db.get(CaseRecord, case_id)
    if not case or case.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Dossier introuvable.")
    if case.status == "pending":
        case.status = "investigating"
    note = CaseComment(tenant_id=principal.tenant_id, case_id=case.id, user_id=principal.user_id, comment=body.comment)
    db.add(note)
    audit(db, principal.tenant_id, actor_label(principal), "case.note", "case", case.id, {})
    db.flush()
    return {"id": note.id}


@router.post("/cases/{case_id}/escalate")
def escalate(case_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    case = db.get(CaseRecord, case_id)
    if not case or case.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Dossier introuvable.")
    case.escalated = True
    case.status = "investigating"
    audit(db, principal.tenant_id, actor_label(principal), "case.escalate", "case", case.id, {})
    return {"id": case.id, "escalated": True}


@router.post("/cases/{case_id}/reports")
def create_report(
    case_id: str,
    body: ReportBody,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    if not principal.allows("investigator", "checker"):
        raise clarity_error(403, "forbidden", "La préparation déclarative est limitée aux investigateurs.")
    case = db.get(CaseRecord, case_id)
    if not case or case.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Dossier introuvable.")
    report = RegulatoryReport(
        tenant_id=principal.tenant_id,
        case_id=case.id,
        report_type=body.report_type,
        status="draft",
        submission_channel="not_connected",
        created_by=principal.user_id,
        content={
            "narrative": body.narrative,
            "jurisdiction": "FR",
            "process": "TRACFIN",
            "notice": "Préparation interne uniquement. Aucun dépôt TRACFIN ou équivalent n'est connecté ni effectué.",
        },
    )
    db.add(report)
    audit(db, principal.tenant_id, actor_label(principal), "report.draft", "regulatory_report", case.id, {"type": body.report_type})
    db.flush()
    return {
        "id": report.id,
        "status": report.status,
        "submission_channel": report.submission_channel,
        "notice": report.content["notice"],
    }


@router.post("/cases/{case_id}/evidence")
async def upload_evidence(
    case_id: str,
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    case = db.get(CaseRecord, case_id)
    if not case or case.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Dossier introuvable.")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise clarity_error(400, "invalid", "Fichier trop volumineux.")
    path, digest = store_bytes(ROOT / "data" / "uploads", principal.tenant_id, file.filename or "evidence.bin", content, db=db)
    evidence = Evidence(tenant_id=principal.tenant_id, case_id=case.id, customer_id=case.customer_id, file_name=file.filename or "evidence.bin", storage_path=path, sha256=digest)
    db.add(evidence)
    audit(db, principal.tenant_id, actor_label(principal), "evidence.upload", "case", case.id, {"sha256": digest})
    db.flush()
    return {"id": evidence.id, "sha256": digest}


@router.get("/studio")
def studio(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    from app.catalog import studio_view

    return studio_view(db, principal.tenant_id)


@router.get("/rules")
def rules(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(MonitoringRule).where(MonitoringRule.tenant_id == principal.tenant_id)).all()
    return [
        {"id": row.id, "name": row.name, "scenario": row.scenario, "parameters": row.parameters, "enabled": row.enabled, "version": row.version, "description": row.description}
        for row in rows
    ]


@router.post("/rules")
def create_rule(body: RuleBody, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("checker", "admin"):
        raise clarity_error(403, "forbidden", "La publication d'une règle exige un checker.")
    if body.scenario not in {"threshold", "velocity", "cross_border", "round_amount"}:
        raise clarity_error(400, "invalid", "Scénario de surveillance inconnu.")
    row = MonitoringRule(
        tenant_id=principal.tenant_id,
        name=body.name,
        scenario=body.scenario,
        parameters=body.parameters,
        description=body.description or "Scénario à investiguer. Ceci n'est pas une conclusion de blanchiment.",
        version=1,
    )
    db.add(row)
    audit(db, principal.tenant_id, actor_label(principal), "rule.create", "monitoring_rule", body.name, {})
    db.flush()
    return {"id": row.id, "version": row.version}


@router.get("/audit")
def audit_log(
    q: str = "",
    actor: str = "",
    action: str = "",
    entity: str = "",
    when: str = "",
    limit: int | None = None,
    offset: int | None = None,
    principal: Principal = Depends(clarity_principal),
    db: Session = Depends(db_session),
):
    rows = db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == principal.tenant_id).order_by(AuditEvent.created_at.desc()).limit(500)).all()
    listed = [
        {"id": row.id, "actor": row.actor, "action": row.action, "entity_type": row.entity_type, "entity_id": row.entity_id, "detail": row.detail, "created_at": iso(row.created_at)}
        for row in rows
    ]

    def keep(row: dict) -> bool:
        haystacks = {
            "actor": row["actor"],
            "action": row["action"],
            "entity": f"{row['entity_type']} {row['entity_id']}",
            "when": row["created_at"] or "",
        }
        for needle, value in (("actor", actor), ("action", action), ("entity", entity), ("when", when)):
            if value and value.lower() not in haystacks[needle].lower():
                return False
        return True

    return present([row for row in listed if keep(row)], q, lambda row: {"actor": row["actor"], "action": row["action"], "entity": row["entity_type"], "when": row["created_at"] or ""}, limit, offset)


class IntegrationUpdate(BaseModel):
    provider: str | None = None
    api_key: str | None = None
    shared_secret: str | None = None
    base_url: str | None = None


class ConfigImport(BaseModel):
    source: str
    payload: dict = Field(default_factory=dict)


def _third_parties(db: Session, tenant_id: str) -> None:
    from app.integrations_store import CATALOG, _DEFAULT

    for kind, provider in _DEFAULT.items():
        row = db.scalar(select(ProviderConnection).where(ProviderConnection.tenant_id == tenant_id, ProviderConnection.kind == kind))
        if row:
            continue
        db.add(
            ProviderConnection(
                tenant_id=tenant_id,
                kind=kind,
                name=CATALOG[provider]["name"],
                mode="not_configured",
                configured=False,
                detail={"provider": provider},
            )
        )
    db.flush()


@router.get("/integrations/providers")
def integration_providers(_principal: Principal = Depends(clarity_principal)):
    from app.integrations_store import provider_catalog

    return provider_catalog()


@router.get("/integrations")
def integrations(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    from app.integrations_store import public_integration

    _third_parties(db, principal.tenant_id)
    rows = db.scalars(select(ProviderConnection).where(ProviderConnection.tenant_id == principal.tenant_id)).all()
    return [public_integration(row) for row in rows if row.kind in {"sanctions", "identity"}]


@router.patch("/integrations/{kind}")
def update_integration(kind: str, body: IntegrationUpdate, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    from app.integrations_store import public_integration, save_integration

    if kind not in {"sanctions", "identity"}:
        raise clarity_error(404, "not_found", "Ce connecteur ne se configure pas ici.")
    _third_parties(db, principal.tenant_id)
    row = db.scalar(select(ProviderConnection).where(ProviderConnection.tenant_id == principal.tenant_id, ProviderConnection.kind == kind))
    if not row:
        raise clarity_error(404, "not_found", "Connecteur introuvable.")
    save_integration(db, row, body.model_dump(exclude_none=True))
    audit(db, principal.tenant_id, actor_label(principal), "integration.update", "provider", kind, {"configured": row.configured, "provider": (row.detail or {}).get("provider")})
    return public_integration(row)


@router.post("/integrations/{kind}/check")
def check_saved_integration(kind: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    from app.integrations_store import check_integration, public_integration

    if kind not in {"sanctions", "identity"}:
        raise clarity_error(404, "not_found", "Ce connecteur ne se vérifie pas ici.")
    row = db.scalar(select(ProviderConnection).where(ProviderConnection.tenant_id == principal.tenant_id, ProviderConnection.kind == kind))
    if not row:
        raise clarity_error(404, "not_found", "Connecteur introuvable.")
    check_integration(db, row)
    return public_integration(row)


@router.post("/imports")
def import_configuration(body: ConfigImport, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    from app.imports import import_bundle

    result = import_bundle(db, principal, body.source, body.payload)
    audit(db, principal.tenant_id, actor_label(principal), "config.import", body.source, principal.tenant_id, {"count": len(result["imported"])})
    return result


@router.get("/compatibility")
def compatibility(request: Request, principal: Principal = Depends(clarity_principal)):
    from app.compatibility import replacement_report

    return replacement_report(request.app)


@router.get("/notifications")
def notifications(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Notification).where(Notification.tenant_id == principal.tenant_id).order_by(Notification.created_at.desc())).all()
    return [{"id": row.id, "title": row.title, "body": row.body, "read": row.read, "created_at": iso(row.created_at)} for row in rows]


class KycPolicyBody(BaseModel):
    kyc_validity_years: int | None = Field(default=None, ge=1, le=10)
    maker_checker_enforced: bool | None = None


def _kyc_policy_payload(tenant: Tenant | None) -> dict:
    years = tenant.kyc_validity_years if tenant else 1
    enforced = True if tenant is None else bool(getattr(tenant, "maker_checker_enforced", True))
    return {
        "kyc_validity_years": years or 1,
        "maker_checker_enforced": enforced,
    }


@router.get("/administration/kyc")
def kyc_policy(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    tenant = db.get(Tenant, principal.tenant_id)
    return _kyc_policy_payload(tenant)


@router.patch("/administration/kyc")
def update_kyc_policy(body: KycPolicyBody, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin"):
        raise clarity_error(403, "forbidden", "Réservé à l'administration.")
    tenant = db.get(Tenant, principal.tenant_id)
    if not tenant:
        raise clarity_error(404, "not_found", "Tenant introuvable.")
    if body.kyc_validity_years is not None:
        tenant.kyc_validity_years = body.kyc_validity_years
    if body.maker_checker_enforced is not None:
        tenant.maker_checker_enforced = body.maker_checker_enforced
    audit(
        db,
        principal.tenant_id,
        actor_label(principal),
        "kyc.policy",
        "tenant",
        tenant.id,
        {
            "kyc_validity_years": tenant.kyc_validity_years,
            "maker_checker_enforced": bool(tenant.maker_checker_enforced),
        },
    )
    return _kyc_policy_payload(tenant)


class FaceThresholdBody(BaseModel):
    threshold: int = Field(ge=0, le=100)


@router.get("/administration/face-threshold")
def read_face_threshold(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    from app.face import face_threshold

    return {"threshold": face_threshold(db, principal.tenant_id)}


@router.patch("/administration/face-threshold")
def update_admin_face_threshold(body: FaceThresholdBody, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin"):
        raise clarity_error(403, "forbidden", "Réservé à l'administration.")
    from app.face import set_face_threshold

    saved = set_face_threshold(db, principal.tenant_id, body.threshold)
    audit(db, principal.tenant_id, actor_label(principal), "face.threshold", "tenant", principal.tenant_id, {"threshold": saved})
    return {"threshold": saved}


class StorageBody(BaseModel):
    provider: str = "filesystem"
    bucket: str = ""
    access_key: str = ""
    secret_key: str = ""
    endpoint: str = ""
    region: str = ""
    force_path_style: bool = True


@router.get("/administration/storage")
def read_storage(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    from app.storage import storage_config, storage_view

    return storage_view(storage_config(db, principal.tenant_id))


@router.patch("/administration/storage")
def update_storage(body: StorageBody, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin"):
        raise clarity_error(403, "forbidden", "Réservé à l'administration.")
    from app.storage import set_storage_config, storage_view

    saved = set_storage_config(db, principal.tenant_id, body.model_dump())
    audit(
        db,
        principal.tenant_id,
        actor_label(principal),
        "storage.config",
        "tenant",
        principal.tenant_id,
        {"provider": saved["provider"], "bucket": saved["bucket"], "endpoint": saved["endpoint"]},
    )
    return storage_view(saved)


@router.post("/administration/purge-clients")
def purge_client_data(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin"):
        raise clarity_error(403, "forbidden", "Réservé à l'administration.")
    from app.services import purge_clients

    result = purge_clients(db, principal.tenant_id)
    audit(db, principal.tenant_id, actor_label(principal), "clients.purge", "tenant", principal.tenant_id, result)
    return result


@router.post("/administration/reseed-clients")
def reseed_client_data(request: Request, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin"):
        raise clarity_error(403, "forbidden", "Réservé à l'administration.")
    from app.seed import reseed_demo_clients
    from app.services import purge_clients

    purged = purge_clients(db, principal.tenant_id)
    seeded = reseed_demo_clients(db, request.app.state.engine)
    result = {**purged, **seeded}
    audit(db, principal.tenant_id, actor_label(principal), "clients.reseed", "tenant", principal.tenant_id, result)
    return result


@router.post("/administration/workflows/{workflow_id}/account-hook/redeliver")
def redeliver_account_hook(workflow_id: str, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    """Re-send the Ballerine-style onboarding webhook for an approved verification."""
    if not principal.allows("admin", "checker"):
        raise clarity_error(403, "forbidden", "Réservé à l'administration ou au contrôleur.")
    from app.services import deliver_account_hook

    workflow = db.get(WorkflowInstance, workflow_id)
    if not workflow or workflow.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Dossier introuvable.")
    if workflow.state not in {"approved", "approved-partially"}:
        raise clarity_error(422, "invalid_state", "Le dossier doit être approuvé pour relancer le hook compte.")
    if not workflow.customer_id:
        raise clarity_error(422, "invalid_state", "Aucun client lié à ce dossier.")
    customer = db.get(Customer, workflow.customer_id)
    if not customer:
        raise clarity_error(404, "not_found", "Client introuvable.")
    deliver_account_hook(db, workflow, customer)
    hook = (workflow.context or {}).get("account_hook") or {}
    audit(
        db,
        principal.tenant_id,
        actor_label(principal),
        "workflow.account_hook.redeliver",
        "workflow",
        workflow.id,
        {"delivered": hook.get("delivered"), "status": hook.get("status"), "url": hook.get("url")},
    )
    return {
        "workflow_id": workflow.id,
        "delivered": hook.get("delivered"),
        "status": hook.get("status"),
        "url": hook.get("url"),
        "error": hook.get("error"),
        "response": hook.get("response"),
    }


@router.get("/administration/users")
def users(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin"):
        raise clarity_error(403, "forbidden", "Réservé à l'administration.")
    rows = db.scalars(select(User).where(User.tenant_id == principal.tenant_id)).all()
    return [_user_view(row) for row in rows]


class NewUserBody(BaseModel):
    family_name: str
    given_name: str
    email: str
    roles: list[str] = []
    password: str | None = None


class UserRoleBody(BaseModel):
    role: str = ""
    roles: list[str] = []


def _keep_bootstrap_admin(row: User, roles: list[str], settings) -> None:
    protected = row.email in settings.oidc_admin_names or row.email == settings.admin_username
    if protected and "admin" not in roles:
        raise clarity_error(422, "invalid_role", "Ce compte conserve le rôle administrateur.")


@router.post("/administration/users", status_code=201)
def create_user(body: NewUserBody, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin"):
        raise clarity_error(403, "forbidden", "Réservé à l'administration.")
    email = body.email.strip().lower()
    if "@" not in email or not body.given_name.strip() or not body.family_name.strip():
        raise clarity_error(422, "invalid_user", "Le nom, le prénom et un e-mail sont requis. L'e-mail est l'identifiant.")
    if db.scalar(select(User).where(User.tenant_id == principal.tenant_id, User.email == email)):
        raise clarity_error(409, "exists", "Cet e-mail est déjà un membre de l'équipe.")
    try:
        stored = serialize_roles(body.roles)
    except ValueError as exc:
        raise clarity_error(422, "invalid_role", "Rôle inconnu.") from exc
    from app.config import current_settings
    from app.security import hash_password
    import secrets

    if current_settings().resolved_auth_mode == "password":
        secret = (body.password or "").strip()
        if len(secret) < 8:
            raise clarity_error(422, "invalid_user", "Un mot de passe d'au moins 8 caractères est requis.")
        digest = hash_password(secret)
    else:
        digest = hash_password(secrets.token_urlsafe(24))
    row = User(
        tenant_id=principal.tenant_id,
        email=email,
        full_name=f"{body.given_name.strip()} {body.family_name.strip()}",
        password_hash=digest,
        role=stored,
        active=True,
    )
    db.add(row)
    db.flush()
    audit(db, principal.tenant_id, actor_label(principal), "user.create", "user", row.id, {"roles": parse_roles(row.role)})
    return _user_view(row)


@router.patch("/administration/users/{user_id}")
def assign_user_role(user_id: str, body: UserRoleBody, request: Request, principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    if not principal.allows("admin"):
        raise clarity_error(403, "forbidden", "Réservé à l'administration.")
    wanted = body.roles or ([body.role] if body.role else [])
    try:
        stored = serialize_roles(wanted)
    except ValueError as exc:
        raise clarity_error(422, "invalid_role", "Rôle inconnu.") from exc
    row = db.get(User, user_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise clarity_error(404, "not_found", "Utilisateur introuvable.")
    _keep_bootstrap_admin(row, parse_roles(stored), request.app.state.settings)
    row.role = stored
    audit(db, principal.tenant_id, actor_label(principal), "user.role", "user", row.id, {"roles": parse_roles(stored)})
    return _user_view(row)


@router.get("/reports")
def reports(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(RegulatoryReport).where(RegulatoryReport.tenant_id == principal.tenant_id).order_by(RegulatoryReport.created_at.desc())).all()
    return [
        {
            "id": row.id,
            "case_id": row.case_id,
            "type": row.report_type,
            "status": row.status,
            "submission_channel": row.submission_channel,
            "created_at": iso(row.created_at),
        }
        for row in rows
    ]


@router.get("/reviews")
def reviews(principal: Principal = Depends(clarity_principal), db: Session = Depends(db_session)):
    rows = db.scalars(
        select(WorkflowTask).where(
            WorkflowTask.tenant_id == principal.tenant_id,
            WorkflowTask.status == "open",
            WorkflowTask.task_key.in_(["analystReview", "checkerReview", "screen", "investigate"]),
        )
    ).all()
    return [{"id": row.id, "workflow_id": row.workflow_id, "task_key": row.task_key, "name": row.name, "due_at": iso(row.due_at)} for row in rows]
