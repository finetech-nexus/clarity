import csv
import io
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import add_doc, get_doc, list_docs
from app.config import ROOT
from app.deps import db_session, marble_principal
from app.models import (
    BatchExecution,
    CaseComment,
    CaseRecord,
    CaseTag,
    Customer,
    Decision,
    Evidence,
    IngestedObject,
    Inbox,
    RiskAssessment,
    RuleSnooze,
    Scenario,
    ScreeningCheck,
    ScreeningMatch,
    Tag,
    Tenant,
    User,
    WatchlistEntry,
    WhitelistEntry,
)
from app.security import Principal
from app.services import (
    ApiFailure,
    actor_label,
    audit,
    case_payload,
    decision_payload,
    execute_scenario,
    ingest_object,
    persist_screening,
    marble_error,
    store_bytes,
    outcome_metadata,
    require_uuid,
)
from app.timeutil import iso, parse_dt, parse_go_duration, utcnow

v1 = APIRouter(prefix="/v1", tags=["marble-v1"])
v1beta = APIRouter(prefix="/v1beta", tags=["marble-v1beta"])


def _tenant(db: Session, principal: Principal) -> Tenant:
    tenant = db.get(Tenant, principal.tenant_id)
    if not tenant:
        raise marble_error(401, None) if False else marble_error(404, "not_found", "tenant not found")
    return tenant


def _flag(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes"}


def _page(items: list, limit: int) -> tuple[list, dict]:
    has_more = len(items) > limit
    page = items[:limit]
    pagination = {"has_more": has_more}
    if has_more and page:
        pagination["next_page_id"] = page[-1].id
    return page, pagination


def _decision_filters(db: Session, principal: Principal, params: dict):
    start_raw = params.get("start")
    end_raw = params.get("end")
    now = utcnow()
    if not start_raw and not end_raw:
        start, end = now - timedelta(days=30), now + timedelta(seconds=1)
    else:
        try:
            start = parse_dt(start_raw) if start_raw else now - timedelta(days=30)
            end = parse_dt(end_raw) if end_raw else now
        except (TypeError, ValueError) as exc:
            raise marble_error(400, "invalid_payload", "start and end must be date-time values") from exc
        if end - start > timedelta(days=90):
            raise marble_error(400, "invalid_payload", "date range cannot exceed 90 days")
    if params.get("review_status") and params.get("outcome") != "block_and_review":
        raise marble_error(400, "invalid_payload", "review_status can only be used when outcome is block_and_review")
    query = select(Decision).where(
        Decision.tenant_id == principal.tenant_id,
        Decision.created_at >= start,
        Decision.created_at < end,
    )
    for field, column in (
        ("scenario_id", Decision.scenario_id),
        ("outcome", Decision.outcome),
        ("review_status", Decision.review_status),
        ("case_id", Decision.case_id),
        ("batch_execution_id", Decision.batch_execution_id),
    ):
        if params.get(field):
            query = query.where(column == params[field])
    if params.get("trigger_object_id"):
        query = query.where(Decision.trigger_object["object_id"].as_string() == params["trigger_object_id"])
    order = params.get("order") or "DESC"
    if order not in {"ASC", "DESC"}:
        raise marble_error(400, "invalid_payload", "order must be ASC or DESC")
    query = query.order_by(Decision.created_at.asc() if order == "ASC" else Decision.created_at.desc())
    if params.get("after"):
        cursor = db.get(Decision, params["after"])
        if cursor and cursor.tenant_id == principal.tenant_id:
            if order == "ASC":
                query = query.where(Decision.created_at > cursor.created_at)
            else:
                query = query.where(Decision.created_at < cursor.created_at)
    return query


def _limit(value: int | None) -> int:
    if value is None:
        return 50
    if value < 1 or value > 100:
        raise marble_error(400, "invalid_payload", "limit must be between 1 and 100")
    return value


def _trigger(body: dict) -> dict:
    trigger = body.get("trigger_object")
    if not isinstance(trigger, dict) or "object_id" not in trigger or "updated_at" not in trigger:
        raise marble_error(400, "invalid_payload", "trigger_object.object_id and trigger_object.updated_at are required")
    try:
        parse_dt(str(trigger["updated_at"]))
    except ValueError as exc:
        raise marble_error(400, "invalid_payload", "trigger_object.updated_at must be a date-time") from exc
    return trigger


@v1.get("/decisions")
def list_decisions(
    request: Request,
    scenario_id: str | None = None,
    outcome: str | None = None,
    review_status: str | None = None,
    trigger_object_id: str | None = None,
    case_id: str | None = None,
    batch_execution_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    order: str | None = None,
    after: str | None = None,
    principal: Principal = Depends(marble_principal),
    db: Session = Depends(db_session),
):
    size = _limit(limit)
    query = _decision_filters(
        db,
        principal,
        {
            "scenario_id": scenario_id,
            "outcome": outcome,
            "review_status": review_status,
            "trigger_object_id": trigger_object_id,
            "case_id": case_id,
            "batch_execution_id": batch_execution_id,
            "start": start,
            "end": end,
            "order": order,
            "after": after,
        },
    )
    rows = list(db.scalars(query.limit(size + 1)).all())
    page, pagination = _page(rows, size)
    include = _flag(request.query_params.get("include_screening_matches"))
    return {"data": [decision_payload(db, row, include) for row in page], "pagination": pagination}


@v1.post("/decisions/all")
def create_all_decisions(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    if "trigger_object_type" not in body:
        raise marble_error(400, "invalid_payload", "trigger_object_type is required")
    trigger = _trigger(body)
    tenant = _tenant(db, principal)
    scenarios = db.scalars(
        select(Scenario).where(
            Scenario.tenant_id == tenant.id,
            Scenario.active.is_(True),
            Scenario.trigger_object_type == body["trigger_object_type"],
        )
    ).all()
    decisions = [execute_scenario(db, tenant, scenario, trigger, trigger_object_type=body["trigger_object_type"]) for scenario in scenarios]
    return {
        "data": [decision_payload(db, decision, False) for decision in decisions],
        "metadata": outcome_metadata(decisions),
        "pagination": {"has_more": False},
    }


@v1.post("/decisions")
def create_decision(
    body: dict,
    include_screening_matches: str | None = None,
    principal: Principal = Depends(marble_principal),
    db: Session = Depends(db_session),
):
    if "scenario_id" not in body:
        raise marble_error(400, "invalid_payload", "scenario_id is required")
    scenario_id = require_uuid(str(body["scenario_id"]), "scenario_id")
    trigger = _trigger(body)
    scenario = db.get(Scenario, scenario_id)
    if not scenario or scenario.tenant_id != principal.tenant_id or not scenario.active:
        raise marble_error(404, "not_found", "scenario not found")
    tenant = _tenant(db, principal)
    decision = execute_scenario(db, tenant, scenario, trigger, trigger_object_type=scenario.trigger_object_type)
    return {
        "data": [decision_payload(db, decision, _flag(include_screening_matches))],
        "metadata": outcome_metadata([decision]),
        "pagination": {"has_more": False},
    }


@v1.get("/decisions/{decision_id}")
def get_decision(decision_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    require_uuid(decision_id, "decisionId")
    decision = db.get(Decision, decision_id)
    if not decision or decision.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "decision not found")
    return {"data": decision_payload(db, decision, True)}


@v1.post("/decisions/{decision_id}/snooze", status_code=201)
def snooze_rule(decision_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    require_uuid(decision_id, "decisionId")
    decision = db.get(Decision, decision_id)
    if not decision or decision.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "decision not found")
    if not decision.case_id:
        raise marble_error(422, "unprocessable_entity", "decision must be part of a case")
    if "rule_id" not in body or "duration" not in body:
        raise marble_error(400, "invalid_payload", "rule_id and duration are required")
    rule_id = require_uuid(str(body["rule_id"]), "rule_id")
    if not any(rule.get("rule_id") == rule_id for rule in decision.rules):
        raise marble_error(422, "unprocessable_entity", "rule was not executed on this decision")
    try:
        duration = parse_go_duration(str(body["duration"]))
    except ValueError as exc:
        raise marble_error(400, "invalid_payload", str(exc)) from exc
    pivot = str(decision.trigger_object.get("object_id") or "")
    db.add(
        RuleSnooze(
            tenant_id=principal.tenant_id,
            decision_id=decision.id,
            rule_id=rule_id,
            pivot_value=pivot,
            until=utcnow() + duration,
        )
    )
    return Response(status_code=201)


@v1.get("/decisions/{decision_id}/screenings")
def decision_screenings(decision_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    require_uuid(decision_id, "decisionId")
    decision = db.get(Decision, decision_id)
    if not decision or decision.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "decision not found")
    payload = decision_payload(db, decision, True)
    return {"data": payload["screenings"]}


@v1.get("/screening/entities/{entity_id}")
def screening_entity(entity_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    entry = db.get(WatchlistEntry, entity_id)
    if not entry or (entry.tenant_id not in {None, principal.tenant_id}):
        raise marble_error(404, "not_found", "entity not found")
    return {
        "data": {
            "id": entry.id,
            "schema": entry.schema_name,
            "datasets": entry.datasets,
            "properties": {
                **entry.properties,
                "name": entry.names,
                "notes": ["SYNTHETIC FIXTURE — not an OpenSanctions entity. No external dataset was queried."],
            },
            "synthetic": True,
        }
    }


def _query_body(body: dict) -> dict:
    if not isinstance(body, dict) or not any(key in body for key in ("Thing", "Person", "Organization", "Vehicle")):
        raise marble_error(400, "invalid_payload", "query must contain Thing, Person, Organization or Vehicle")
    return body


@v1.post("/screening/search")
def freeform_search(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    from app.services import find_candidates

    tenant = _tenant(db, principal)
    candidates = find_candidates(db, tenant, _query_body(body), None)
    return {"data": [item["payload"] for item in candidates]}


@v1.post("/screening/whitelists", status_code=201)
def add_whitelist(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    if "counterparty" not in body or "entity_id" not in body:
        raise marble_error(400, "invalid_payload", "counterparty and entity_id are required")
    existing = db.scalar(
        select(WhitelistEntry).where(
            WhitelistEntry.tenant_id == principal.tenant_id,
            WhitelistEntry.counterparty == body["counterparty"],
            WhitelistEntry.entity_id == body["entity_id"],
        )
    )
    if not existing:
        db.add(WhitelistEntry(tenant_id=principal.tenant_id, counterparty=str(body["counterparty"]), entity_id=str(body["entity_id"])))
    return Response(status_code=201)


@v1.delete("/screening/whitelists", status_code=204)
def delete_whitelist(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    if "entity_id" not in body:
        raise marble_error(400, "invalid_payload", "entity_id is required")
    query = select(WhitelistEntry).where(WhitelistEntry.tenant_id == principal.tenant_id, WhitelistEntry.entity_id == body["entity_id"])
    if body.get("counterparty"):
        query = query.where(WhitelistEntry.counterparty == body["counterparty"])
    for row in db.scalars(query).all():
        db.delete(row)
    return Response(status_code=204)


@v1.post("/screening/whitelists/search")
def search_whitelist(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    if not isinstance(body, dict) or not any(body.get(key) for key in ("counterparty", "entity_id")):
        raise marble_error(400, "invalid_payload", "counterparty or entity_id is required")
    query = select(WhitelistEntry).where(WhitelistEntry.tenant_id == principal.tenant_id)
    if body.get("counterparty"):
        query = query.where(WhitelistEntry.counterparty == body["counterparty"])
    if body.get("entity_id"):
        query = query.where(WhitelistEntry.entity_id == body["entity_id"])
    rows = db.scalars(query).all()
    return {"data": [{"counterparty": row.counterparty, "entity_id": row.entity_id} for row in rows]}


@v1.post("/screening/{screening_id}/refine")
def refine_screening(screening_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    require_uuid(screening_id, "screeningId")
    screening = db.get(ScreeningCheck, screening_id)
    if not screening or screening.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "screening not found")
    query = _query_body(body)
    from app.services import persist_screening

    tenant = _tenant(db, principal)
    for match in db.scalars(select(ScreeningMatch).where(ScreeningMatch.screening_id == screening.id)).all():
        db.delete(match)
    db.flush()
    replacement = persist_screening(
        db,
        tenant,
        query,
        rule_id=screening.rule_id,
        decision_id=screening.decision_id,
        customer_id=screening.customer_id,
        counterparty=None,
    )
    db.delete(screening)
    decision = db.get(Decision, replacement.decision_id) if replacement.decision_id else None
    if decision:
        return {"data": decision_payload(db, decision, True)["screenings"][-1]}
    return {"data": {"id": replacement.id, "status": replacement.status, "match_count": replacement.match_count}}


@v1.post("/screening/{screening_id}/search")
def search_screening(screening_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    require_uuid(screening_id, "screeningId")
    screening = db.get(ScreeningCheck, screening_id)
    if not screening or screening.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "screening not found")
    from app.services import find_candidates

    tenant = _tenant(db, principal)
    candidates = find_candidates(db, tenant, _query_body(body), None)
    return {"data": [item["payload"] for item in candidates]}


@v1.post("/screening/matches/{match_id}")
def review_match(match_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    require_uuid(match_id, "matchId")
    match = db.get(ScreeningMatch, match_id)
    if not match or match.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "match not found")
    if "status" not in body:
        raise marble_error(400, "invalid_payload", "status is required")
    from app.services import disposition_match

    tenant = _tenant(db, principal)
    disposition_match(db, tenant, principal, match, str(body["status"]), bool(body.get("whitelist", False)))
    return {"data": {"id": match.id, "queries": match.query_ids, "status": match.status, "payload": match.payload}}


@v1.get("/batch-executions")
def list_batches(
    limit: int | None = None,
    principal: Principal = Depends(marble_principal),
    db: Session = Depends(db_session),
):
    size = _limit(limit)
    rows = list(
        db.scalars(
            select(BatchExecution)
            .where(BatchExecution.tenant_id == principal.tenant_id)
            .order_by(BatchExecution.created_at.desc())
            .limit(size + 1)
        ).all()
    )
    page, _pagination = _page(rows, size)
    data = []
    for row in page:
        scenario = db.get(Scenario, row.scenario_id)
        data.append(
            {
                "id": row.id,
                "scenario": {
                    "id": scenario.id,
                    "iteration_id": scenario.iteration_id,
                    "version": scenario.version,
                }
                if scenario
                else None,
                "manual": row.manual,
                "status": row.status,
                "decisions_created": row.decisions_created,
                "created_at": iso(row.created_at),
                "finished_at": iso(row.finished_at),
            }
        )
    return {"data": data}


def _ingest(object_type: str, payload, monitor: str | None, skip_screening: str | None, principal: Principal, db: Session) -> Response:
    tenant = _tenant(db, principal)
    items = payload if isinstance(payload, list) else [payload]
    if isinstance(payload, list) and (len(items) < 1 or len(items) > 100):
        raise marble_error(400, "invalid_payload", "batch must contain between 1 and 100 objects")
    statuses = [
        ingest_object(db, tenant, object_type, item, _flag(monitor), _flag(skip_screening))
        for item in items
        if isinstance(item, dict)
    ]
    if len(statuses) != len(items):
        raise marble_error(400, "invalid_payload", "each item must be an object")
    return Response(status_code=201 if 201 in statuses else 200)


@v1.post("/ingest/{object_type}")
@v1.patch("/ingest/{object_type}")
def ingest_one(
    object_type: str,
    body: dict,
    monitor: str | None = None,
    skip_screening: str | None = None,
    principal: Principal = Depends(marble_principal),
    db: Session = Depends(db_session),
):
    return _ingest(object_type, body, monitor, skip_screening, principal, db)


@v1.post("/ingest/{object_type}/batch")
@v1.patch("/ingest/{object_type}/batch")
def ingest_batch(
    object_type: str,
    body: list,
    monitor: str | None = None,
    skip_screening: str | None = None,
    principal: Principal = Depends(marble_principal),
    db: Session = Depends(db_session),
):
    return _ingest(object_type, body, monitor, skip_screening, principal, db)


def _case_or_404(db: Session, principal: Principal, case_id: str) -> CaseRecord:
    require_uuid(case_id, "caseId")
    case = db.get(CaseRecord, case_id)
    if not case or case.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "case not found")
    return case


@v1beta.get("/cases")
def list_cases(
    inbox_id: str | None = None,
    status: str | None = None,
    assigned_to: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    order: str | None = None,
    after: str | None = None,
    principal: Principal = Depends(marble_principal),
    db: Session = Depends(db_session),
):
    size = _limit(limit)
    direction = order or "DESC"
    if direction not in {"ASC", "DESC"}:
        raise marble_error(400, "invalid_payload", "order must be ASC or DESC")
    if status and status not in {"pending", "investigating", "closed"}:
        raise marble_error(400, "invalid_payload", "invalid status")
    query = select(CaseRecord).where(CaseRecord.tenant_id == principal.tenant_id)
    if inbox_id:
        query = query.where(CaseRecord.inbox_id == require_uuid(inbox_id, "inbox_id"))
    if status:
        query = query.where(CaseRecord.status == status)
    if assigned_to:
        query = query.where(CaseRecord.assignee_id == require_uuid(assigned_to, "assigned_to"))
    if start:
        query = query.where(CaseRecord.created_at >= parse_dt(start))
    if end:
        query = query.where(CaseRecord.created_at < parse_dt(end))
    query = query.order_by(CaseRecord.created_at.asc() if direction == "ASC" else CaseRecord.created_at.desc())
    if after:
        cursor = db.get(CaseRecord, after)
        if cursor and cursor.tenant_id == principal.tenant_id:
            comparator = CaseRecord.created_at > cursor.created_at if direction == "ASC" else CaseRecord.created_at < cursor.created_at
            query = query.where(comparator)
    rows = list(db.scalars(query.limit(size + 1)).all())
    page, pagination = _page(rows, size)
    return {"data": [case_payload(db, row) for row in page], "pagination": pagination}


@v1beta.post("/cases")
def create_case(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    if "inbox_id" not in body or "name" not in body:
        raise marble_error(400, "invalid_payload", "inbox_id and name are required")
    inbox_id = require_uuid(str(body["inbox_id"]), "inbox_id")
    inbox = db.get(Inbox, inbox_id)
    if not inbox or inbox.tenant_id != principal.tenant_id:
        raise marble_error(422, "unprocessable_entity", "inbox not found")
    assignee_id = None
    if body.get("assignee_email"):
        user = db.scalar(select(User).where(User.tenant_id == principal.tenant_id, User.email == body["assignee_email"]))
        if not user:
            raise marble_error(422, "unprocessable_entity", "assignee not found")
        assignee_id = user.id
    case = CaseRecord(
        tenant_id=principal.tenant_id,
        inbox_id=inbox.id,
        name=str(body["name"]),
        assignee_id=assignee_id,
        due_at=utcnow() + timedelta(days=5),
    )
    db.add(case)
    db.flush()
    for decision_id in body.get("decisions") or []:
        decision = db.get(Decision, require_uuid(str(decision_id), "decision_id"))
        if not decision or decision.tenant_id != principal.tenant_id:
            raise marble_error(422, "unprocessable_entity", "decision not found")
        if decision.case_id:
            raise marble_error(422, "unprocessable_entity", "decision is already assigned to a case")
        decision.case_id = case.id
    audit(db, principal.tenant_id, actor_label(principal), "case.create", "case", case.id, {})
    return {"data": case_payload(db, case)}


@v1beta.get("/cases/{case_id}")
def get_case(case_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    return {"data": case_payload(db, _case_or_404(db, principal, case_id))}


@v1beta.patch("/cases/{case_id}")
def update_case(case_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    case = _case_or_404(db, principal, case_id)
    if "name" in body:
        case.name = str(body["name"])
    if "inbox" in body:
        inbox = db.get(Inbox, require_uuid(str(body["inbox"]), "inbox"))
        if not inbox or inbox.tenant_id != principal.tenant_id:
            raise marble_error(422, "unprocessable_entity", "inbox not found")
        case.inbox_id = inbox.id
    if "outcome" in body:
        if body["outcome"] not in {"unset", "confirmed_risk", "valuable_alert", "false_positive"}:
            raise marble_error(400, "invalid_payload", "invalid outcome")
        case.outcome = body["outcome"]
    case.updated_at = utcnow()
    return {"data": case_payload(db, case)}


@v1beta.post("/cases/{case_id}/close")
def close_case(case_id: str, body: dict | None = None, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    from app.services import assess_customer

    case = _case_or_404(db, principal, case_id)
    if case.status == "closed":
        raise marble_error(422, "unprocessable_entity", "case is already closed")
    outcome = (body or {}).get("outcome", "unset")
    if outcome not in {"unset", "confirmed_risk", "valuable_alert", "false_positive"}:
        raise marble_error(400, "invalid_payload", "invalid outcome")
    case.status = "closed"
    case.outcome = outcome
    case.updated_at = utcnow()
    audit(db, principal.tenant_id, actor_label(principal), "case.close", "case", case.id, {"outcome": outcome})
    if case.customer_id and outcome in {"confirmed_risk", "valuable_alert", "false_positive"}:
        customer = db.get(Customer, case.customer_id)
        if customer:
            assess_customer(db, _tenant(db, principal), customer, source="ruleset")
    return {"data": case_payload(db, case)}


@v1beta.post("/cases/{case_id}/reopen")
def reopen_case(case_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    case = _case_or_404(db, principal, case_id)
    if case.status != "closed":
        raise marble_error(422, "unprocessable_entity", "case is not closed")
    case.status = "pending"
    case.updated_at = utcnow()
    return {"data": case_payload(db, case)}


@v1beta.post("/cases/{case_id}/escalate")
def escalate_case(case_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    case = _case_or_404(db, principal, case_id)
    if case.status == "closed":
        raise marble_error(422, "unprocessable_entity", "case is closed")
    case.status = "investigating"
    case.escalated = True
    case.updated_at = utcnow()
    audit(db, principal.tenant_id, actor_label(principal), "case.escalate", "case", case.id, {})
    return {"data": case_payload(db, case)}


@v1beta.get("/cases/{case_id}/comments")
def list_comments(case_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    case = _case_or_404(db, principal, case_id)
    rows = db.scalars(select(CaseComment).where(CaseComment.case_id == case.id).order_by(CaseComment.created_at.asc())).all()
    data = []
    for row in rows:
        user = db.get(User, row.user_id) if row.user_id else None
        data.append(
            {
                "id": row.id,
                "user": {"id": user.id, "name": user.full_name} if user else {"id": principal.tenant_id, "name": "api"},
                "comment": row.comment,
                "created_at": iso(row.created_at),
            }
        )
    return {"data": data}


@v1beta.post("/cases/{case_id}/comments", status_code=201)
def create_comment(case_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    case = _case_or_404(db, principal, case_id)
    comment = body.get("comment")
    if not isinstance(comment, str) or not comment or len(comment) > 16384:
        raise marble_error(400, "invalid_payload", "comment is required and must be at most 16384 characters")
    service_user = db.scalar(select(User).where(User.tenant_id == principal.tenant_id, User.role == "admin"))
    row = CaseComment(tenant_id=principal.tenant_id, case_id=case.id, user_id=service_user.id if service_user else None, comment=comment)
    db.add(row)
    db.flush()
    user = db.get(User, row.user_id) if row.user_id else None
    return {
        "data": {
            "id": row.id,
            "user": {"id": user.id, "name": user.full_name} if user else {"id": principal.tenant_id, "name": "api"},
            "comment": row.comment,
            "created_at": iso(row.created_at),
        }
    }


@v1beta.get("/tags")
def list_tags(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Tag).where(Tag.tenant_id == principal.tenant_id)).all()
    return {"data": [{"id": row.id, "name": row.name, "target": row.target} for row in rows]}


@v1beta.get("/risk-levels/{object_type}/{object_id}")
def get_risk(object_type: str, object_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    row = db.scalar(
        select(RiskAssessment).where(
            RiskAssessment.tenant_id == principal.tenant_id,
            RiskAssessment.object_type == object_type,
            RiskAssessment.object_id == object_id,
            RiskAssessment.current.is_(True),
        )
    )
    if not row:
        raise marble_error(404, "not_found", "risk level not found")
    user = db.get(User, row.overridden_by) if row.overridden_by else None
    payload = {
        "source": row.source,
        "risk_level": row.risk_level,
        "current": row.current,
        "created_at": iso(row.created_at),
    }
    if user:
        payload["overridden_by"] = {"id": user.id, "name": user.full_name}
    if row.stale_at:
        payload["stale_at"] = iso(row.stale_at)
    return {"data": payload}


@v1beta.post("/risk-levels/{object_type}/{object_id}")
def override_risk(
    object_type: str,
    object_id: str,
    body: dict,
    principal: Principal = Depends(marble_principal),
    db: Session = Depends(db_session),
):
    if "risk_level" not in body or not isinstance(body["risk_level"], int) or body["risk_level"] < 1:
        raise marble_error(400, "invalid_payload", "risk_level must be an integer greater than or equal to 1")
    current = db.scalar(
        select(RiskAssessment).where(
            RiskAssessment.tenant_id == principal.tenant_id,
            RiskAssessment.object_type == object_type,
            RiskAssessment.object_id == object_id,
            RiskAssessment.current.is_(True),
        )
    )
    if not current:
        raise marble_error(404, "not_found", "risk level not found")
    current.current = False
    current.stale_at = utcnow()
    admin = db.scalar(select(User).where(User.tenant_id == principal.tenant_id, User.role == "checker"))
    row = RiskAssessment(
        tenant_id=principal.tenant_id,
        customer_id=current.customer_id,
        object_type=object_type,
        object_id=object_id,
        source="override",
        score=current.score,
        risk_level=body["risk_level"],
        tier=current.tier,
        factors=current.factors + [{"code": "manual_override", "points": 0, "explanation": "Niveau imposé manuellement. L'explication du score précédent est conservée."}],
        current=True,
        overridden_by=admin.id if admin else None,
        rationale="Override via Marble-compatible API",
    )
    db.add(row)
    db.flush()
    audit(db, principal.tenant_id, actor_label(principal), "risk.override", "risk_assessment", row.id, {"risk_level": body["risk_level"]})
    return {
        "data": {
            "source": row.source,
            "risk_level": row.risk_level,
            "current": True,
            "created_at": iso(row.created_at),
            "overridden_by": {"id": admin.id, "name": admin.full_name} if admin else None,
        }
    }


@v1beta.post("/cases/{case_id}/tags")
def add_case_tags(case_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    case = _case_or_404(db, principal, case_id)
    tag_ids = body.get("tag_ids")
    if not isinstance(tag_ids, list) or not tag_ids:
        raise marble_error(400, "invalid_payload", "tag_ids is required")
    for tag_id in tag_ids:
        tag = db.get(Tag, str(tag_id))
        if not tag or tag.tenant_id != principal.tenant_id or tag.target != "case":
            raise marble_error(400, "invalid_payload", "every tag must exist and target case")
        if not db.scalar(select(CaseTag).where(CaseTag.case_id == case.id, CaseTag.tag_id == tag.id)):
            db.add(CaseTag(case_id=case.id, tag_id=tag.id))
    db.flush()
    return {"data": [{"id": link.tag_id} for link in db.scalars(select(CaseTag).where(CaseTag.case_id == case.id)).all()]}


@v1beta.delete("/cases/{case_id}/tags/{tag_id}")
def remove_case_tag(case_id: str, tag_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    case = _case_or_404(db, principal, case_id)
    link = db.scalar(select(CaseTag).where(CaseTag.case_id == case.id, CaseTag.tag_id == tag_id))
    if not link:
        raise marble_error(404, "not_found", "tag not found on case")
    db.delete(link)
    return {"data": {"id": tag_id}}


def _file_payload(row: Evidence) -> dict:
    return {"id": row.id, "file_name": row.file_name, "created_at": iso(row.created_at)}


@v1beta.get("/cases/{case_id}/files")
def list_case_files(case_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    case = _case_or_404(db, principal, case_id)
    rows = db.scalars(select(Evidence).where(Evidence.tenant_id == principal.tenant_id, Evidence.case_id == case.id)).all()
    return {"data": [_file_payload(row) for row in rows]}


@v1beta.post("/cases/{case_id}/files")
async def upload_case_file(
    case_id: str,
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(marble_principal),
    db: Session = Depends(db_session),
):
    case = _case_or_404(db, principal, case_id)
    content = await file.read()
    if not content:
        raise marble_error(400, "invalid_payload", "file is required")
    if len(content) > 5_242_880:
        raise marble_error(400, "invalid_payload", "file exceeds 5MB")
    path, digest = store_bytes(ROOT / "data" / "uploads", principal.tenant_id, file.filename or "case-file.bin", content, db=db)
    row = Evidence(tenant_id=principal.tenant_id, case_id=case.id, file_name=file.filename or "case-file.bin", storage_path=path, sha256=digest)
    db.add(row)
    db.flush()
    return {"data": _file_payload(row)}


@v1beta.get("/cases/{case_id}/files/{file_id}/download")
def download_case_file(case_id: str, file_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    case = _case_or_404(db, principal, case_id)
    row = db.get(Evidence, file_id)
    if not row or row.case_id != case.id or row.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "file not found")
    return FileResponse(row.storage_path, filename=row.file_name)


@v1beta.post("/decisions/async", status_code=201)
def create_async_decisions(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    objects = body.get("trigger_objects")
    if not isinstance(objects, list) or not 1 <= len(objects) <= 100:
        raise marble_error(400, "invalid_payload", "trigger_objects must contain between 1 and 100 objects")
    if "trigger_object_type" not in body and "scenario_id" not in body:
        raise marble_error(400, "invalid_payload", "trigger_object_type or scenario_id is required")
    tenant = _tenant(db, principal)
    scenario = None
    object_type = body.get("trigger_object_type")
    if body.get("scenario_id"):
        scenario = db.get(Scenario, str(body["scenario_id"]))
        if not scenario or scenario.tenant_id != tenant.id:
            raise marble_error(404, "not_found", "scenario not found")
        object_type = object_type or scenario.trigger_object_type
    scenarios = [scenario] if scenario else db.scalars(select(Scenario).where(Scenario.tenant_id == tenant.id, Scenario.active.is_(True), Scenario.trigger_object_type == object_type)).all()
    created = []
    for trigger in objects:
        if not isinstance(trigger, dict):
            raise marble_error(400, "invalid_payload", "each trigger object must be an object")
        decision_ids = []
        for item in scenarios:
            decision = execute_scenario(db, tenant, item, trigger, trigger_object_type=object_type or item.trigger_object_type)
            decision_ids.append(decision.id)
        record = add_doc(
            db,
            tenant.id,
            "async_decision",
            object_type or "",
            {"object_type": object_type or "", "scenario_id": scenario.id if scenario else None, "status": "completed", "decision_ids": decision_ids},
            status="completed",
        )
        created.append({"id": record.id})
    return {"data": created}


@v1beta.get("/decisions/async/{execution_id}")
def get_async_decision(execution_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    require_uuid(execution_id, "executionId")
    row = get_doc(db, principal.tenant_id, "async_decision", execution_id)
    if not row:
        raise marble_error(404, "not_found", "execution not found")
    return {
        "data": {
            "id": row.id,
            "object_type": row.payload.get("object_type") or "",
            "scenario_id": row.payload.get("scenario_id"),
            "status": row.payload.get("status"),
            "decision_ids": row.payload.get("decision_ids") or [],
            "created_at": iso(row.created_at),
            "updated_at": iso(row.updated_at),
        }
    }


def _ingestion_log(row) -> dict:
    payload = row.payload or {}
    return {
        "id": row.id,
        "object_type": payload.get("object_type") or row.name,
        "status": payload.get("status") or row.status,
        "started_at": payload.get("started_at") or iso(row.created_at),
        "finished_at": payload.get("finished_at"),
        "rows_processed": int(payload.get("rows_processed") or 0),
        "rows_ingested": int(payload.get("rows_ingested") or 0),
    }


@v1beta.get("/ingest/{object_type}/uploads")
def list_ingestion_uploads(
    object_type: str,
    status: str | None = None,
    limit: int | None = None,
    principal: Principal = Depends(marble_principal),
    db: Session = Depends(db_session),
):
    if status and status not in {"pending", "processing", "success", "failure"}:
        raise marble_error(400, "invalid_payload", "invalid status")
    rows = [row for row in list_docs(db, principal.tenant_id, "batch_ingestion") if (row.payload or {}).get("object_type") == object_type]
    if status:
        rows = [row for row in rows if (row.payload or {}).get("status") == status]
    page, pagination = _page(rows, _limit(limit))
    return {"data": [_ingestion_log(row) for row in page], "pagination": pagination}


@v1beta.put("/ingest/{object_type}/uploads", status_code=202)
def create_ingestion_upload(object_type: str, request: Request, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    row = add_doc(
        db,
        principal.tenant_id,
        "batch_ingestion",
        object_type,
        {"object_type": object_type, "status": "pending", "started_at": iso(utcnow()), "rows_processed": 0, "rows_ingested": 0},
        status="pending",
    )
    upload_url = str(request.base_url).rstrip("/") + f"/v1beta/ingest-uploads/{row.id}"
    row.payload = {**row.payload, "upload_url": upload_url}
    return {"data": {"upload_url": upload_url}}


@v1beta.put("/ingest-uploads/{upload_id}")
async def receive_ingestion_upload(upload_id: str, request: Request, db: Session = Depends(db_session)):
    from app.models import ConfigDocument

    require_uuid(upload_id, "uploadId")
    row = db.get(ConfigDocument, upload_id)
    if not row or row.kind != "batch_ingestion":
        raise marble_error(404, "not_found", "upload not found")
    tenant = db.get(Tenant, row.tenant_id)
    raw = await request.body()
    processed = ingested = 0
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        for item in reader:
            processed += 1
            if not item.get("object_id") or not item.get("updated_at"):
                continue
            try:
                ingest_object(db, tenant, row.payload["object_type"], dict(item), False, True)
                ingested += 1
            except ApiFailure:
                continue
        status = "success" if processed and ingested == processed else "failure"
    except UnicodeDecodeError:
        status = "failure"
    row.payload = {**row.payload, "status": status, "rows_processed": processed, "rows_ingested": ingested, "finished_at": iso(utcnow())}
    row.status = status
    return _ingestion_log(row)


def _ai_summary(row) -> dict:
    return {
        "id": row.id,
        "case_id": row.parent_id,
        "status": row.payload.get("status") or row.status,
        "reaction": row.payload.get("reaction"),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


@v1beta.get("/cases/{case_id}/ai_reviews")
def list_ai_reviews(case_id: str, limit: int | None = None, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    _case_or_404(db, principal, case_id)
    rows = list_docs(db, principal.tenant_id, "ai_review", case_id)
    page, pagination = _page(rows, _limit(limit))
    return {"data": [_ai_summary(row) for row in page], "pagination": pagination}


@v1beta.post("/cases/{case_id}/ai_reviews", status_code=202)
def enqueue_ai_review(case_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    from app.providers import complete_ai

    case = _case_or_404(db, principal, case_id)
    pending = [row for row in list_docs(db, principal.tenant_id, "ai_review", case.id) if row.status == "pending"]
    if pending:
        raise marble_error(409, "conflict", "a review is already pending")
    text = complete_ai(
        "Tu aides un analyste. N'affirme pas qu'il y a blanchiment et ne confirme pas un rapprochement.",
        f"Dossier {case.name} statut {case.status}.",
    )
    local = text is None
    row = add_doc(
        db,
        principal.tenant_id,
        "ai_review",
        "review",
        {
            "status": "completed",
            "reaction": None,
            "production": False,
            "provider": "configured-model" if not local else "clarity-local-analyst",
            "content": {
                "version": "v1",
                "output": text or "Revue locale. Ceci n'est pas une conclusion de blanchiment et ne confirme aucun rapprochement.",
                "sanity_check": None,
            },
        },
        parent_id=case.id,
        status="completed",
    )
    return {"data": {"ai_review_id": row.id}}


@v1beta.get("/cases/{case_id}/ai_reviews/{ai_review_id}")
def get_ai_review(case_id: str, ai_review_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    _case_or_404(db, principal, case_id)
    require_uuid(ai_review_id, "aiReviewId")
    row = get_doc(db, principal.tenant_id, "ai_review", ai_review_id)
    if not row or row.parent_id != case_id:
        raise marble_error(404, "not_found", "review not found")
    return {"data": {**_ai_summary(row), "content": row.payload.get("content")}}


@v1beta.post("/continuous-screenings/objects", status_code=201)
def add_continuous_object(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    for field in ("object_type", "object_id", "config_stable_id"):
        if not body.get(field):
            raise marble_error(400, "invalid_payload", f"{field} is required")
    config_id = require_uuid(str(body["config_stable_id"]), "config_stable_id")
    config = get_doc(db, principal.tenant_id, "continuous_screening", config_id)
    if not config:
        raise marble_error(404, "not_found", "screening config not found")
    monitor_name = f"{body['object_type']}:{body['object_id']}:{config.id}"
    enrolled = next((row for row in list_docs(db, principal.tenant_id, "monitored_object") if row.name == monitor_name), None)
    if not enrolled:
        add_doc(db, principal.tenant_id, "monitored_object", monitor_name, {"object_type": body["object_type"], "object_id": body["object_id"], "config_stable_id": config.id})
    if body.get("skip_screen"):
        return Response(status_code=201)
    tenant = _tenant(db, principal)
    ingested = db.scalar(
        select(IngestedObject).where(
            IngestedObject.tenant_id == tenant.id,
            IngestedObject.object_type == str(body["object_type"]),
            IngestedObject.object_id == str(body["object_id"]),
        )
    )
    name = (ingested.payload.get("name") if ingested else None) or str(body["object_id"])
    screening = persist_screening(db, tenant, {"Person": {"name": name}}, counterparty=str(body["object_id"]))
    matches = db.scalars(select(ScreeningMatch).where(ScreeningMatch.screening_id == screening.id)).all()
    return {
        "id": screening.id,
        "continuous_screening_config_stable_id": config.id,
        "case_id": None,
        "object_type": body["object_type"],
        "object_id": body["object_id"],
        "status": screening.status,
        "trigger_type": "manual",
        "partial": False,
        "number_of_matches": screening.match_count,
        "matches": [{"id": match.id, "status": match.status, "score": match.score} for match in matches],
        "created_at": iso(screening.created_at) if getattr(screening, "created_at", None) else iso(utcnow()),
        "updated_at": iso(utcnow()),
        "production": False,
    }


@v1beta.delete("/continuous-screenings/objects", status_code=204)
def remove_continuous_object(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    from app.models import ConfigDocument

    for field in ("object_type", "object_id", "config_stable_id"):
        if not body.get(field):
            raise marble_error(400, "invalid_payload", f"{field} is required")
    config_id = require_uuid(str(body["config_stable_id"]), "config_stable_id")
    monitor_name = f"{body['object_type']}:{body['object_id']}:{config_id}"
    row = next((item for item in list_docs(db, principal.tenant_id, "monitored_object") if item.name == monitor_name), None)
    if not row:
        raise marble_error(404, "not_found", "monitored object not found")
    stored = db.get(ConfigDocument, row.id)
    if stored:
        db.delete(stored)
    return Response(status_code=204)


def _annotation(row) -> dict:
    payload = row.payload or {}
    return {
        "id": row.id,
        "object_type": payload.get("object_type"),
        "object_id": payload.get("object_id"),
        "annotation_type": payload.get("annotation_type"),
        "payload": payload.get("payload") or {},
        "created_at": iso(row.created_at),
    }


@v1beta.get("/records/{record_type}/{record_id}/annotations")
def list_record_annotations(record_type: str, record_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    rows = [
        row
        for row in list_docs(db, principal.tenant_id, "record_annotation")
        if (row.payload or {}).get("object_type") == record_type and (row.payload or {}).get("object_id") == record_id
    ]
    return {"data": [_annotation(row) for row in rows]}


@v1beta.post("/records/{record_type}/{record_id}/annotations", status_code=201)
def create_record_annotations(record_type: str, record_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    items = body if isinstance(body, list) else body.get("annotations") or body.get("data")
    if not isinstance(items, list) or not items:
        raise marble_error(400, "invalid_payload", "annotations are required")
    created = []
    existing = list_docs(db, principal.tenant_id, "record_annotation")
    for item in items:
        kind = item.get("annotation_type") or item.get("type")
        if kind not in {"comment", "tag", "risk_tag"}:
            raise marble_error(400, "invalid_payload", "annotation_type must be comment, tag or risk_tag")
        duplicate = None
        if kind in {"tag", "risk_tag"}:
            duplicate = next(
                (
                    row
                    for row in existing
                    if (row.payload or {}).get("object_type") == record_type
                    and (row.payload or {}).get("object_id") == record_id
                    and (row.payload or {}).get("annotation_type") == kind
                    and (row.payload or {}).get("payload") == (item.get("payload") or {})
                ),
                None,
            )
        if duplicate:
            created.append(_annotation(duplicate))
            continue
        row = add_doc(
            db,
            principal.tenant_id,
            "record_annotation",
            kind,
            {"object_type": record_type, "object_id": record_id, "annotation_type": kind, "payload": item.get("payload") or {}},
        )
        existing.append(row)
        created.append(_annotation(row))
    return {"data": created}


@v1beta.post("/records/{record_type}/{record_id}/annotations/files", status_code=201)
async def create_record_file_annotation(
    record_type: str,
    record_id: str,
    files: list[UploadFile] = File(...),
    principal: Principal = Depends(marble_principal),
    db: Session = Depends(db_session),
):
    parts = []
    for upload in files:
        content = await upload.read()
        if not content:
            continue
        path, digest = store_bytes(ROOT / "data" / "uploads", principal.tenant_id, upload.filename or "annotation.bin", content, db=db)
        parts.append({"id": digest[:32], "file_name": upload.filename or "annotation.bin", "path": path, "sha256": digest})
    if not parts:
        raise marble_error(400, "invalid_payload", "a file is required")
    row = add_doc(
        db,
        principal.tenant_id,
        "record_annotation",
        "file",
        {"object_type": record_type, "object_id": record_id, "annotation_type": "file", "payload": {"parts": parts}},
    )
    return {"data": _annotation(row)}


@v1beta.get("/records/annotations/{annotation_id}/files/{part_id}/download")
def download_annotation_file(annotation_id: str, part_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    require_uuid(annotation_id, "id")
    row = get_doc(db, principal.tenant_id, "record_annotation", annotation_id)
    if not row:
        raise marble_error(404, "not_found", "annotation not found")
    part = next((item for item in (row.payload.get("payload") or {}).get("parts") or [] if item.get("id") == part_id), None)
    if not part:
        raise marble_error(404, "not_found", "file not found")
    return FileResponse(part["path"], filename=part.get("file_name") or "annotation.bin")


@v1beta.delete("/records/annotations", status_code=204)
def delete_record_annotations(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    from app.models import ConfigDocument

    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > 100:
        raise marble_error(400, "invalid_payload", "ids must contain between 1 and 100 values")
    for item in ids:
        require_uuid(str(item), "ids")
        row = db.get(ConfigDocument, str(item))
        if row and row.tenant_id == principal.tenant_id and row.kind == "record_annotation":
            db.delete(row)
    return Response(status_code=204)
