import hashlib
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ast_eval import AstError, eval_ast
from app.engine import PROCESS_CATALOG, TERMINAL
from app.models import (
    Alert,
    ApiCredential,
    Approval,
    AuditEvent,
    BatchExecution,
    BusinessProfile,
    CaseComment,
    CaseRecord,
    CaseTag,
    Customer,
    DataModelField,
    Decision,
    Evidence,
    IdentityDocument,
    IndividualProfile,
    IngestedObject,
    Inbox,
    MonitoringRule,
    Notification,
    Party,
    ProviderConnection,
    RegulatoryReport,
    RiskAssessment,
    RuleSnooze,
    Scenario,
    ScreeningCheck,
    ScreeningMatch,
    Tag,
    Tenant,
    Transaction,
    User,
    KycCycle,
    VerificationCheck,
    WatchlistEntry,
    WhitelistEntry,
    WorkflowInstance,
    WorkflowTask,
)
from app.screening import explain_match, extract_query_name, name_score, normalize_name
from app.security import Principal, hash_token
from app.timeutil import as_utc, iso, parse_dt, utcnow

DATA_MODELS = {
    "transactions": {"object_id", "updated_at", "amount", "currency", "direction", "country", "customer_id", "counterparty_name", "description"},
    "customers": {"object_id", "updated_at", "name", "type", "country", "birth_date", "nationality"},
    "accounts": {"object_id", "updated_at", "customer_id", "iban", "currency", "status"},
}

EVENT_ACTIONS = {
    "approve": "approve",
    "approved": "approve",
    "reject": "reject",
    "rejected": "reject",
    "revision": "remediate",
    "remediate": "remediate",
    "requestDocuments": "requestDocuments",
    "DOCUMENTS_SUBMITTED": "submit",
    "submit": "submit",
    "return": "return",
    "close": "close",
    "escalate": "escalate",
}


class ApiFailure(Exception):
    def __init__(self, status: int, body: dict | None):
        self.status = status
        self.body = body
        super().__init__(body)


def marble_error(status: int, code: str, *messages: str, detail: dict | None = None) -> ApiFailure:
    body: dict = {"code": code, "messages": list(messages)}
    if detail:
        body["detail"] = detail
    return ApiFailure(status, body)


def nest_error(status: int, message: str) -> ApiFailure:
    labels = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 422: "Unprocessable Entity", 501: "Not Implemented"}
    return ApiFailure(status, {"statusCode": status, "message": message, "error": labels.get(status, "Error")})


def clarity_error(status: int, code: str, message: str) -> ApiFailure:
    return ApiFailure(status, {"code": code, "message": message})


def audit(db: Session, tenant_id: str, actor: str, action: str, entity_type: str, entity_id: str, detail: dict | None = None) -> None:
    db.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail or {},
        )
    )


KYC_OVERDUE_DAYS = 30


def kyc_label(status: str) -> str:
    if status == "overdue":
        return "pending · overdue"
    if status == "approved-partially":
        return "Approuvé partiellement"
    return status


def kyc_cycle_view(row: KycCycle, runtime_id: str | None = None, documents: list | None = None) -> dict:
    return {
        "id": row.id,
        "customer_id": row.customer_id,
        "workflow_id": row.workflow_id,
        "workflow_runtime_id": runtime_id,
        "status": row.status,
        "label": kyc_label(row.status),
        "pending": row.status in {"pending", "overdue"},
        "created_at": iso(row.created_at),
        "validated_at": iso(row.validated_at),
        "expires_at": iso(row.expires_at),
        "reminder": row.reminder,
        "reminded_at": iso(row.reminded_at),
        "production": row.production,
        "documents": documents or [],
    }


def latest_kyc_cycle(db: Session, customer_id: str) -> KycCycle | None:
    return db.scalar(select(KycCycle).where(KycCycle.customer_id == customer_id).order_by(KycCycle.created_at.desc()))


def open_kyc_cycle(db: Session, customer: Customer, workflow_id: str | None = None) -> KycCycle:
    current = latest_kyc_cycle(db, customer.id)
    if current and current.status == "pending":
        if workflow_id and not current.workflow_id:
            current.workflow_id = workflow_id
        return current
    row = KycCycle(tenant_id=customer.tenant_id, customer_id=customer.id, workflow_id=workflow_id, status="pending")
    db.add(row)
    db.flush()
    return row


def validate_kyc_cycle(db: Session, customer: Customer, workflow_id: str | None, expires_at) -> KycCycle:
    row = latest_kyc_cycle(db, customer.id)
    if row is None or row.status == "valid":
        row = KycCycle(tenant_id=customer.tenant_id, customer_id=customer.id, workflow_id=workflow_id, status="pending")
        db.add(row)
        db.flush()
    row.status = "valid"
    row.workflow_id = workflow_id or row.workflow_id
    row.validated_at = utcnow()
    row.expires_at = expires_at
    row.reminder = "none"
    row.reminded_at = None
    return row


def refresh_kyc_cycles(db: Session, tenant_id: str) -> None:
    now = utcnow()
    rows = db.scalars(select(KycCycle).where(KycCycle.tenant_id == tenant_id)).all()
    for row in rows:
        if row.status not in {"valid", "expired"} or row.expires_at is None:
            continue
        expires = as_utc(row.expires_at)
        if row.status == "valid" and expires <= now:
            row.status = "expired"
            if row.reminder == "none":
                row.reminder = "automatic"
                row.reminded_at = now
                customer = db.get(Customer, row.customer_id)
                name = customer.display_name if customer else row.customer_id
                notify(db, tenant_id, "Relance KYC", f"{name} : la vérification est expirée. Une nouvelle vérification est ouverte.")
            pending = db.scalar(
                select(KycCycle).where(KycCycle.customer_id == row.customer_id, KycCycle.id != row.id, KycCycle.status == "pending")
            )
            if pending is None:
                db.add(KycCycle(tenant_id=row.tenant_id, customer_id=row.customer_id, status="pending"))
        if row.status == "expired" and expires + timedelta(days=KYC_OVERDUE_DAYS) <= now:
            row.status = "overdue"


def remind_kyc_cycle(db: Session, customer: Customer, cycle_id: str, document_ids: list[str] | None = None) -> KycCycle:
    refresh_kyc_cycles(db, customer.tenant_id)
    row = db.get(KycCycle, cycle_id)
    if not row or row.customer_id != customer.id:
        raise clarity_error(404, "not_found", "Vérification introuvable.")
    requested = [item for item in (document_ids or []) if item]
    if requested:
        found = db.scalars(
            select(IdentityDocument).where(
                IdentityDocument.customer_id == customer.id,
                IdentityDocument.id.in_(requested),
            )
        ).all()
        if len(found) != len(set(requested)):
            raise clarity_error(404, "not_found", "Pièce introuvable sur cette vérification.")
        for document in found:
            if row.workflow_id and document.workflow_id not in {None, row.workflow_id}:
                raise clarity_error(404, "not_found", "Pièce introuvable sur cette vérification.")
            document.decision = "revisions"
            document.decision_reason = "Pièce demandée lors de la relance."
        titles = ", ".join(str((document.properties or {}).get("title") or document.doc_type or "Pièce") for document in found)
        note = f"pièces demandées ({titles})"
    elif row.status == "valid":
        raise clarity_error(422, "not_due", "La relance intervient à l'expiration de la vérification.")
    else:
        note = "relance manuelle enregistrée"
    row.reminder = "manual"
    row.reminded_at = utcnow()
    notify(db, customer.tenant_id, "Relance KYC manuelle", f"{customer.display_name} : {note}.")
    return row


def notify(db: Session, tenant_id: str, title: str, body: str, user_id: str | None = None) -> None:
    db.add(Notification(tenant_id=tenant_id, user_id=user_id, title=title, body=body))


def actor_label(principal: Principal) -> str:
    return principal.email or principal.credential_id or principal.role


def credential_for_token(db: Session, token: str, surface: str) -> ApiCredential | None:
    digest = hash_token(token)
    return db.scalar(select(ApiCredential).where(ApiCredential.token_hash == digest, ApiCredential.surface == surface))


def user_by_email(db: Session, tenant_id: str, email: str) -> User | None:
    return db.scalar(select(User).where(User.tenant_id == tenant_id, User.email == email, User.active.is_(True)))


def require_uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError) as exc:
        raise marble_error(400, "invalid_payload", f"{field} must be a uuid") from exc


def ref(entity_id: str | None, name: str | None = None) -> dict | None:
    if not entity_id:
        return None
    payload = {"id": entity_id}
    if name:
        payload["name"] = name
    return payload


def decision_payload(db: Session, decision: Decision, include_matches: bool) -> dict:
    scenario = db.get(Scenario, decision.scenario_id)
    screenings = db.scalars(select(ScreeningCheck).where(ScreeningCheck.decision_id == decision.id)).all()
    screening_rows = []
    for screening in screenings:
        matches = []
        if include_matches:
            rows = db.scalars(select(ScreeningMatch).where(ScreeningMatch.screening_id == screening.id)).all()
            matches = [
                {
                    "id": row.id,
                    "queries": row.query_ids,
                    "status": row.status,
                    "payload": row.payload,
                }
                for row in rows
            ]
        screening_rows.append(
            {
                "id": screening.id,
                "rule_id": screening.rule_id,
                "status": screening.status,
                "query": screening.query,
                "partial": screening.partial,
                "match_count": screening.match_count,
                "created_at": iso(screening.created_at),
                "updated_at": iso(screening.updated_at),
                "matches": matches,
            }
        )
    case = db.get(CaseRecord, decision.case_id) if decision.case_id else None
    payload = {
        "id": decision.id,
        "outcome": decision.outcome,
        "review_status": decision.review_status,
        "scenario": {
            "id": decision.scenario_id,
            "iteration_id": decision.iteration_id,
            "version": decision.scenario_version,
            "name": scenario.name if scenario else None,
        },
        "trigger_object": decision.trigger_object,
        "score": decision.score,
        "created_at": iso(decision.created_at),
        "rules": decision.rules,
        "screenings": screening_rows,
    }
    if decision.batch_execution_id:
        payload["batch_execution_id"] = decision.batch_execution_id
    if case:
        payload["case"] = ref(case.id, case.name)
    return payload


def outcome_metadata(decisions: list[Decision]) -> dict:
    counts = {"total": len(decisions), "approve": 0, "review": 0, "block_and_review": 0, "decline": 0, "skipped": 0}
    for decision in decisions:
        if decision.outcome in counts:
            counts[decision.outcome] += 1
    return counts


def _watchlist(db: Session, tenant_id: str) -> list[WatchlistEntry]:
    return list(
        db.scalars(
            select(WatchlistEntry).where((WatchlistEntry.tenant_id == tenant_id) | (WatchlistEntry.tenant_id.is_(None)))
        ).all()
    )


def find_candidates(db: Session, tenant: Tenant, query_body: dict, counterparty: str | None) -> list[dict]:
    kind, query_name, attributes = extract_query_name(query_body)
    if not query_name:
        return []
    threshold = tenant.screening_threshold
    dob = attributes.get("birthDate") or attributes.get("birth_date")
    country = attributes.get("nationality") or attributes.get("country")
    rows = []
    for entry in _watchlist(db, tenant.id):
        if kind == "Person" and entry.schema_name not in {"Person", "Thing"}:
            continue
        if kind == "Organization" and entry.schema_name not in {"Organization", "Thing"}:
            continue
        if _whitelisted(db, tenant.id, counterparty, entry.id):
            continue
        best_name = ""
        best = 0
        for candidate in entry.names:
            score = name_score(query_name, candidate)
            if dob and entry.birth_dates and dob not in entry.birth_dates:
                score = max(0, score - 12)
            if country and entry.countries and country not in entry.countries:
                score = max(0, score - 8)
            if score > best:
                best = score
                best_name = candidate
        if best < threshold:
            continue
        payload = {
            "id": entry.id,
            "schema": entry.schema_name,
            "datasets": entry.datasets,
            "score": round(best / 100, 4),
            "properties": {
                **entry.properties,
                "name": entry.names,
                "country": entry.countries,
                "birthDate": entry.birth_dates,
                "topics": entry.topics,
                "notes": [
                    "SYNTHETIC FIXTURE — not a real sanctions, PEP or adverse-media record. No external screening provider was called."
                ],
            },
            "synthetic": True,
            "provider": "clarity-synthetic-watchlist",
        }
        rows.append(
            {
                "entity_id": entry.id,
                "score": best,
                "explanation": explain_match(query_name, best_name, best, dob, entry.countries),
                "payload": payload,
            }
        )
    from app.providers import match_opensanctions

    for candidate in match_opensanctions(query_name, kind or "Person", dob, country):
        if candidate["score"] < threshold:
            continue
        if _whitelisted(db, tenant.id, counterparty, candidate["entity_id"]):
            continue
        rows.append(candidate)
    rows.sort(key=lambda item: item["score"], reverse=True)
    return rows[:25]


def _whitelisted(db: Session, tenant_id: str, counterparty: str | None, entity_id: str) -> bool:
    if not counterparty:
        return False
    found = db.scalar(
        select(WhitelistEntry).where(
            WhitelistEntry.tenant_id == tenant_id,
            WhitelistEntry.counterparty == counterparty,
            WhitelistEntry.entity_id == entity_id,
        )
    )
    return found is not None


def persist_screening(
    db: Session,
    tenant: Tenant,
    query_body: dict,
    *,
    rule_id: str | None,
    decision_id: str | None,
    customer_id: str | None,
    counterparty: str | None,
) -> ScreeningCheck:
    query_id = str(uuid.uuid4())
    candidates = find_candidates(db, tenant, query_body, counterparty)
    status = "in_review" if candidates else "no_hit"
    screening = ScreeningCheck(
        tenant_id=tenant.id,
        customer_id=customer_id,
        decision_id=decision_id,
        rule_id=rule_id,
        status=status,
        query={"queries": {query_id: query_body}},
        match_count=len(candidates),
        partial=False,
        production=any(item.get("production") for item in candidates),
    )
    db.add(screening)
    db.flush()
    for candidate in candidates:
        db.add(
            ScreeningMatch(
                tenant_id=tenant.id,
                screening_id=screening.id,
                entity_id=candidate["entity_id"],
                status="pending",
                score=candidate["score"],
                explanation=candidate["explanation"],
                payload=candidate["payload"],
                query_ids=[query_id],
            )
        )
    return screening


def _custom_lists(db: Session, tenant_id: str) -> dict[str, list]:
    from app.models import ConfigDocument

    lists: dict[str, list] = {}
    rows = db.scalars(select(ConfigDocument).where(ConfigDocument.tenant_id == tenant_id, ConfigDocument.kind == "custom_list")).all()
    values = db.scalars(
        select(ConfigDocument).where(ConfigDocument.tenant_id == tenant_id, ConfigDocument.kind == "custom_list_value")
    ).all()
    names = {row.id: row.name for row in rows}
    for row in values:
        key = names.get(row.parent_id or "", row.payload.get("list_name") or "")
        lists.setdefault(key, []).append(row.payload.get("value"))
    return lists


def _rule_hit(rule: dict, trigger: dict, lists: dict[str, list] | None = None) -> tuple[str, str | None]:
    kind = rule["kind"]
    try:
        if kind == "ast":
            value = eval_ast(rule["ast"], trigger, lists or {})
            if rule.get("screening") or _ast_mentions(rule["ast"], "Screen"):
                return "hit", None
            return ("hit" if bool(value) else "no_hit"), None
        if kind == "threshold":
            value = trigger.get(rule["field"])
            if value is None:
                return "no_hit", None
            number = float(value)
            op = rule.get("op", "gte")
            expected = float(rule["value"])
            hit = number >= expected if op == "gte" else number > expected
            return ("hit" if hit else "no_hit"), None
        if kind == "country":
            country = str(trigger.get(rule.get("field", "country")) or "")
            return ("hit" if country in set(rule.get("values", [])) else "no_hit"), None
        if kind == "screening":
            return "hit", None
        return "error", "unknown rule kind"
    except (TypeError, ValueError, AstError) as exc:
        return "error", str(exc)


def _ast_mentions(node, name: str) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("name") == name:
        return True
    return any(_ast_mentions(child, name) for child in node.get("children") or []) or any(
        _ast_mentions(child, name) for child in (node.get("named_children") or {}).values()
    )


def _active_snooze(db: Session, tenant_id: str, rule_id: str, pivot: str) -> bool:
    now = utcnow()
    row = db.scalar(
        select(RuleSnooze).where(
            RuleSnooze.tenant_id == tenant_id,
            RuleSnooze.rule_id == rule_id,
            RuleSnooze.pivot_value == pivot,
            RuleSnooze.until > now,
        )
    )
    return row is not None


def execute_scenario(
    db: Session,
    tenant: Tenant,
    scenario: Scenario,
    trigger: dict,
    *,
    trigger_object_type: str,
    batch_execution_id: str | None = None,
) -> Decision:
    score = 0
    hard_decline = False
    executed = []
    screening_rule_id = None
    pivot = str(trigger.get("object_id") or "")
    lists = _custom_lists(db, tenant.id)
    for rule in scenario.rules:
        if _active_snooze(db, tenant.id, rule["id"], pivot):
            executed.append(
                {
                    "name": rule["name"],
                    "rule_id": rule["id"],
                    "outcome": "snoozed",
                    "score_modifier": int(rule.get("score_modifier", 0)),
                }
            )
            continue
        outcome, error = _rule_hit(rule, trigger, lists)
        row = {
            "name": rule["name"],
            "rule_id": rule["id"],
            "outcome": outcome,
            "score_modifier": int(rule.get("score_modifier", 0)),
        }
        if error:
            row["error"] = {"code": 1, "message": error}
        if outcome == "hit":
            score += int(rule.get("score_modifier", 0))
            if rule.get("hard_outcome") == "decline":
                hard_decline = True
            if rule["kind"] == "screening" or rule.get("screening"):
                screening_rule_id = rule["id"]
        executed.append(row)

    decision = Decision(
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        iteration_id=scenario.iteration_id,
        scenario_version=scenario.version,
        outcome="approve",
        review_status=None,
        score=score,
        trigger_object=trigger,
        trigger_object_type=trigger_object_type,
        batch_execution_id=batch_execution_id,
        rules=executed,
    )
    db.add(decision)
    db.flush()

    screening_status = None
    if screening_rule_id:
        name = trigger.get("counterparty_name") or trigger.get("name") or trigger.get("customer_name") or ""
        kind = "Organization" if str(trigger.get("type") or trigger.get("customer_type") or "").lower() in {"business", "company", "organization"} else "Person"
        query = {kind: {"name": name}}
        if trigger.get("birth_date"):
            query[kind]["birthDate"] = trigger["birth_date"]
        if trigger.get("country"):
            query[kind]["nationality" if kind == "Person" else "country"] = trigger["country"]
        screening = persist_screening(
            db,
            tenant,
            query,
            rule_id=screening_rule_id,
            decision_id=decision.id,
            customer_id=None,
            counterparty=pivot or name,
        )
        screening_status = screening.status

    if hard_decline:
        decision.outcome = "decline"
        decision.review_status = None
    elif screening_status == "in_review":
        decision.outcome = "block_and_review"
        decision.review_status = "pending"
    elif score >= scenario.decline_threshold:
        decision.outcome = "decline"
        decision.review_status = None
    elif score >= scenario.block_threshold:
        decision.outcome = "block_and_review"
        decision.review_status = "pending"
    elif score >= scenario.review_threshold:
        decision.outcome = "review"
        decision.review_status = None
    else:
        decision.outcome = "approve"
        decision.review_status = None
    audit(db, tenant.id, "scenario-engine", "decision.create", "decision", decision.id, {"outcome": decision.outcome, "score": score})
    return decision


def validate_ingestion(db: Session, tenant_id: str, object_type: str, payload: dict) -> None:
    if "object_id" not in payload or "updated_at" not in payload:
        raise marble_error(400, "invalid_payload", "object_id and updated_at are required")
    try:
        parse_dt(str(payload["updated_at"]))
    except ValueError as exc:
        raise marble_error(400, "invalid_payload", "updated_at must be a date-time") from exc
    allowed = {
        row.name
        for row in db.scalars(
            select(DataModelField).where(DataModelField.tenant_id == tenant_id, DataModelField.object_type == object_type)
        ).all()
    }
    if not allowed:
        raise marble_error(400, "invalid_payload", f"unknown object type {object_type}")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise marble_error(400, "invalid_payload", "unknown fields: " + ", ".join(unknown))


def ingest_object(db: Session, tenant: Tenant, object_type: str, payload: dict, monitor: bool, skip_screening: bool) -> int:
    validate_ingestion(db, tenant.id, object_type, payload)
    updated = parse_dt(str(payload["updated_at"]))
    existing = db.scalar(
        select(IngestedObject).where(
            IngestedObject.tenant_id == tenant.id,
            IngestedObject.object_type == object_type,
            IngestedObject.object_id == str(payload["object_id"]),
            IngestedObject.updated_at == updated,
        )
    )
    if existing:
        return 200
    db.add(
        IngestedObject(
            tenant_id=tenant.id,
            object_type=object_type,
            object_id=str(payload["object_id"]),
            updated_at=updated,
            payload=payload,
        )
    )
    if object_type == "transactions":
        _transaction_from_ingestion(db, tenant, payload, updated)
    if monitor and not skip_screening and payload.get("name"):
        kind = "Organization" if payload.get("type") in {"business", "company"} else "Person"
        persist_screening(db, tenant, {kind: {"name": payload["name"]}}, rule_id=None, decision_id=None, customer_id=None, counterparty=str(payload["object_id"]))
    return 201


def _transaction_from_ingestion(db: Session, tenant: Tenant, payload: dict, updated) -> Transaction:
    customer = None
    if payload.get("customer_id"):
        customer = db.scalar(
            select(Customer).where(Customer.tenant_id == tenant.id, Customer.external_ref == str(payload["customer_id"]))
        )
    tx = Transaction(
        tenant_id=tenant.id,
        customer_id=customer.id if customer else None,
        correlation_id=str(payload["object_id"]),
        amount=float(payload.get("amount") or 0),
        currency=str(payload.get("currency") or "EUR"),
        base_amount=float(payload.get("amount") or 0),
        base_currency="EUR",
        direction=payload.get("direction"),
        country=payload.get("country"),
        counterparty_name=payload.get("counterparty_name"),
        description=payload.get("description"),
        occurred_at=updated,
        payload=payload,
        synthetic=bool(tenant.synthetic),
    )
    db.add(tx)
    db.flush()
    evaluate_transaction(db, tenant, tx)
    return tx


def evaluate_transaction(db: Session, tenant: Tenant, tx: Transaction) -> list[Alert]:
    rules = db.scalars(select(MonitoringRule).where(MonitoringRule.tenant_id == tenant.id, MonitoringRule.enabled.is_(True))).all()
    created = []
    history = []
    if tx.customer_id:
        history = list(
            db.scalars(
                select(Transaction).where(Transaction.tenant_id == tenant.id, Transaction.customer_id == tx.customer_id)
            ).all()
        )
    for rule in rules:
        hit, detail = _monitoring_hit(rule, tx, history)
        if not hit:
            continue
        day = tx.occurred_at.date().isoformat()
        dedup = f"{tx.customer_id or tx.correlation_id}:{rule.id}:{day}"
        existing = db.scalar(select(Alert).where(Alert.tenant_id == tenant.id, Alert.dedup_key == dedup, Alert.status == "open"))
        if existing:
            existing.suppressed_count += 1
            continue
        alert = Alert(
            tenant_id=tenant.id,
            customer_id=tx.customer_id,
            transaction_id=tx.id,
            rule_id=rule.id,
            title=f"Scénario à investiguer : {rule.name}",
            priority=rule.parameters.get("priority", "medium"),
            dedup_key=dedup,
            rationale=detail,
        )
        db.add(alert)
        db.flush()
        case = CaseRecord(
            tenant_id=tenant.id,
            inbox_id=_default_inbox_id(db, tenant.id),
            customer_id=tx.customer_id,
            name=alert.title,
            status="pending",
            due_at=utcnow() + timedelta(days=5),
        )
        db.add(case)
        db.flush()
        alert.case_id = case.id
        notify(db, tenant.id, "Nouvelle alerte", detail)
        audit(db, tenant.id, "monitoring", "alert.create", "alert", alert.id, {"rule": rule.name})
        created.append(alert)
    maybe_open_funding_review(db, tenant, tx)
    return created


def maybe_open_funding_review(db: Session, tenant: Tenant, tx: Transaction) -> WorkflowInstance | None:
    """A transfer on a limited account opens a provenance review for the manager."""
    if not tx.customer_id or not _incoming_transfer(tx.direction):
        return None
    customer = db.get(Customer, tx.customer_id)
    if not customer or customer.canonical_status != "approved-partially":
        return None
    existing = db.scalar(
        select(WorkflowInstance).where(
            WorkflowInstance.customer_id == customer.id,
            WorkflowInstance.definition_key == "clarityFundingOrigin",
            WorkflowInstance.status == "active",
        )
    )
    if existing:
        return existing
    from app.engine import InProcessEngine

    engine = InProcessEngine()
    instance_id, task = engine.start("clarityFundingOrigin", customer.id, {})
    workflow = WorkflowInstance(
        tenant_id=tenant.id,
        customer_id=customer.id,
        definition_key="clarityFundingOrigin",
        definition_name=PROCESS_CATALOG["clarityFundingOrigin"]["name"],
        engine=engine.mode,
        engine_instance_id=instance_id,
        ballerine_runtime_id=str(uuid.uuid4()),
        context={
            "entity": {
                "type": customer.kind,
                "id": customer.external_ref or customer.id,
                "data": {
                    "amount": tx.amount,
                    "currency": tx.currency,
                    "counterpartyName": tx.counterparty_name or "",
                    "direction": tx.direction or "",
                    "description": tx.description or "",
                    "country": tx.country or "",
                },
            },
            "source": "funding-transfer",
            "transaction_id": tx.id,
        },
        config={},
        created_by="clarity",
    )
    db.add(workflow)
    db.flush()
    _sync_task(db, workflow, task)
    audit(db, tenant.id, "clarity", "workflow.start", "workflow", workflow.id, {"definition": "clarityFundingOrigin", "transaction_id": tx.id})
    return workflow


def _incoming_transfer(direction: str | None) -> bool:
    value = str(direction or "").strip().lower()
    if not value:
        return True
    return value not in {"out", "outbound", "outgoing", "debit", "sortant", "débit"}


def _default_inbox_id(db: Session, tenant_id: str) -> str:
    inbox = db.scalar(select(Inbox).where(Inbox.tenant_id == tenant_id))
    if not inbox:
        raise clarity_error(422, "inbox_missing", "Aucun inbox n'est configuré pour le tenant.")
    return inbox.id


def _monitoring_hit(rule: MonitoringRule, tx: Transaction, history: list[Transaction]) -> tuple[bool, str]:
    params = rule.parameters or {}
    scenario = rule.scenario
    amount = float(tx.base_amount or 0)
    if scenario == "threshold":
        limit = float(params.get("amount", 10000))
        if amount >= limit:
            return True, f"Montant {amount:.2f} {tx.base_currency} atteint le seuil configuré de {limit:.2f}. Schéma à investiguer, pas une conclusion de blanchiment."
    if scenario == "cross_border":
        home = set(params.get("home_countries", ["FR"]))
        if tx.country and tx.country not in home and amount >= float(params.get("amount", 1000)):
            return True, f"Opération transfrontalière vers {tx.country}. Schéma à investiguer, pas une conclusion de blanchiment."
    if scenario == "velocity":
        window = timedelta(hours=int(params.get("window_hours", 24)))
        count = 0
        occurred = as_utc(tx.occurred_at)
        for previous in history:
            previous_at = as_utc(previous.occurred_at)
            if occurred - window <= previous_at <= occurred:
                count += 1
        if count >= int(params.get("count", 5)):
            return True, f"{count} opérations sur la fenêtre configurée. Écart de comportement à investiguer, pas une conclusion de blanchiment."
    if scenario == "round_amount":
        multiple = float(params.get("multiple", 1000))
        minimum = float(params.get("minimum", 3000))
        window = timedelta(hours=int(params.get("window_hours", 48)))
        if amount >= minimum and amount % multiple == 0:
            occurred = as_utc(tx.occurred_at)
            similar = [
                previous
                for previous in history
                if occurred - window <= as_utc(previous.occurred_at) <= occurred
                and float(previous.base_amount or 0) % multiple == 0
            ]
            if len(similar) >= int(params.get("count", 3)):
                return True, "Série de montants ronds au-dessus du seuil. Schéma à investiguer, pas une conclusion de blanchiment."
    return False, ""


def backtest(db: Session, tenant: Tenant, rule_id: str, start, end) -> dict:
    rule = db.get(MonitoringRule, rule_id)
    if not rule or rule.tenant_id != tenant.id:
        raise clarity_error(404, "not_found", "Règle introuvable.")
    rows = db.scalars(
        select(Transaction).where(
            Transaction.tenant_id == tenant.id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
        )
    ).all()
    hits = 0
    for tx in rows:
        history = [item for item in rows if item.customer_id == tx.customer_id]
        hit, _detail = _monitoring_hit(rule, tx, history)
        hits += int(hit)
    return {
        "rule_id": rule.id,
        "from": iso(start),
        "to": iso(end),
        "transactions_seen": len(rows),
        "pattern_hits": hits,
        "alerts_created": 0,
        "note": "Rejeu historique des règles configurées. Aucun hit n'est une conclusion de blanchiment et aucune alerte n'a été créée.",
    }


def _advance_if_ready(db: Session, engine, workflow: WorkflowInstance) -> bool:
    from app.engine import ADVANCE

    task = db.scalar(select(WorkflowTask).where(WorkflowTask.workflow_id == workflow.id, WorkflowTask.status == "open"))
    if not task or "_" not in ADVANCE.get(workflow.definition_key, {}).get(task.task_key, {}):
        return False
    _complete_active(db, engine, workflow, {"action": "_"})
    return True


def _sync_task(db: Session, workflow: WorkflowInstance, task: dict | None) -> None:
    open_tasks = db.scalars(
        select(WorkflowTask).where(WorkflowTask.workflow_id == workflow.id, WorkflowTask.status == "open")
    ).all()
    for current in open_tasks:
        current.status = "completed"
        current.completed_at = utcnow()
    workflow.updated_at = utcnow()
    if task is None:
        return
    db.add(
        WorkflowTask(
            tenant_id=workflow.tenant_id,
            workflow_id=workflow.id,
            task_key=task["taskDefinitionKey"],
            name=task["name"],
            engine_task_id=task["id"],
            due_at=utcnow() + timedelta(days=2),
        )
    )
    workflow.state = task["taskDefinitionKey"]


def _complete_active(db: Session, engine, workflow: WorkflowInstance, variables: dict) -> None:
    task = db.scalar(select(WorkflowTask).where(WorkflowTask.workflow_id == workflow.id, WorkflowTask.status == "open"))
    if not task or not task.engine_task_id or not workflow.engine_instance_id:
        raise clarity_error(422, "no_task", "Aucune tâche ouverte.")
    if workflow.engine_instance_id and hasattr(engine, "restore"):
        serial = 1
        parts = (task.engine_task_id or "").rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            serial = int(parts[1])
        engine.restore(workflow.engine_instance_id, workflow.definition_key, task.task_key, serial)
    nxt = engine.complete(workflow.engine_instance_id, task.engine_task_id, variables)
    if not nxt:
        action = variables.get("action")
        if action == "approve":
            terminal = "approved"
        elif action == "approvePartial":
            terminal = "approved-partially"
        elif action == "reject":
            terminal = "rejected"
        elif action == "close":
            terminal = "closed"
        else:
            terminal = "completed"
        if action == "reject":
            workflow.status = "failed"
        elif action == "approvePartial":
            workflow.status = "approved-partially"
        else:
            workflow.status = "completed"
        workflow.state = terminal if terminal in TERMINAL or terminal == "completed" else terminal
        workflow.resolved_at = utcnow()
        _sync_task(db, workflow, None)
        if workflow.state == "completed":
            workflow.state = "closed"
        return
    _sync_task(db, workflow, nxt[0])


_VERIFICATION_KEYS = {"clarityKycIndividual", "clarityKybBusiness", "risk-score-improvement"}


def _open_verification(db: Session, customer: Customer) -> WorkflowInstance | None:
    """The single verification line for this client, whatever step it has reached."""
    return db.scalar(
        select(WorkflowInstance)
        .where(
            WorkflowInstance.customer_id == customer.id,
            WorkflowInstance.status == "active",
            WorkflowInstance.definition_key.in_(_VERIFICATION_KEYS),
        )
        .order_by(WorkflowInstance.created_at.desc())
    )


def _merge_context(current: dict | None, incoming: dict | None) -> dict:
    merged = dict(current or {})
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_context(merged[key], value)
        else:
            merged[key] = value
    return merged


def _kyc_is_current(db: Session, customer: Customer) -> bool:
    refresh_kyc_cycles(db, customer.tenant_id)
    cycle = latest_kyc_cycle(db, customer.id)
    return bool(cycle and cycle.status == "valid")


def _normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def _phone_digits(value: str | None) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _contact_from_data(data: dict) -> tuple[str, str]:
    additional = data.get("additionalInfo") if isinstance(data.get("additionalInfo"), dict) else {}
    email = _normalize_email(data.get("email") or additional.get("email"))
    phone = _phone_digits(data.get("phone") or data.get("phoneNumber") or additional.get("phone") or additional.get("phoneNumber"))
    return email, phone


def find_customer_by_contact(db: Session, tenant_id: str, kind: str, data: dict) -> Customer | None:
    """Reuse the same client when phone or email already belongs to an individual."""
    if kind != "individual":
        return None
    email, phone = _contact_from_data(data)
    if not email and not phone:
        return None
    profiles = db.scalars(
        select(IndividualProfile)
        .join(Customer, Customer.id == IndividualProfile.customer_id)
        .where(Customer.tenant_id == tenant_id, Customer.kind == "individual")
        .order_by(Customer.created_at.asc())
    ).all()
    for profile in profiles:
        if email and _normalize_email(profile.email) == email:
            return db.get(Customer, profile.customer_id)
        profile_phone = _phone_digits(profile.phone)
        if phone and profile_phone and (phone == profile_phone or phone.endswith(profile_phone[-9:]) or profile_phone.endswith(phone[-9:])):
            return db.get(Customer, profile.customer_id)
    return None


def _sync_customer(db: Session, customer: Customer, kind: str, data: dict) -> None:
    customer.display_name = _display_name(kind, data)
    customer.updated_at = utcnow()
    for field, keys in (
        ("purpose", ("purpose",)),
        ("expected_activity", ("expected_activity", "expectedActivity")),
        ("source_of_funds", ("source_of_funds", "sourceOfFunds")),
    ):
        value = next((data.get(key) for key in keys if data.get(key)), None)
        if value:
            setattr(customer, field, value)
    if kind == "individual":
        fresh = _individual(customer.id, data)
        current = customer.individual
        names = ("legal_name", "given_name", "family_name", "date_of_birth", "nationality", "residence_country", "address", "email", "phone")
        if current is None:
            db.add(fresh)
            customer.individual = fresh
        else:
            for name in names:
                setattr(current, name, getattr(fresh, name))
        return
    fresh_business = _business(customer.id, data)
    current_business = customer.business
    names = ("legal_name", "trading_name", "registration_number", "jurisdiction", "legal_form", "registered_address", "industry", "activities")
    if current_business is None:
        db.add(fresh_business)
        customer.business = fresh_business
        db.flush()
        return
    for name in names:
        setattr(current_business, name, getattr(fresh_business, name))


def purge_clients(db: Session, tenant_id: str) -> dict:
    """Remove clients and the verifications attached to them. Users and settings stay."""
    customers = db.scalars(select(Customer).where(Customer.tenant_id == tenant_id)).all()
    customer_ids = [row.id for row in customers]
    documents = db.scalars(select(IdentityDocument).where(IdentityDocument.tenant_id == tenant_id)).all()
    removed_files = 0
    for document in documents:
        if not document.storage_path:
            continue
        path = Path(document.storage_path)
        if path.is_file():
            path.unlink()
            removed_files += 1
    workflows = db.scalars(select(WorkflowInstance).where(WorkflowInstance.tenant_id == tenant_id)).all()
    workflow_ids = [row.id for row in workflows]
    cases = db.scalars(select(CaseRecord).where(CaseRecord.tenant_id == tenant_id)).all()
    case_ids = [row.id for row in cases]
    screenings = db.scalars(select(ScreeningCheck).where(ScreeningCheck.tenant_id == tenant_id)).all()
    screening_ids = [row.id for row in screenings]
    if case_ids:
        db.execute(delete(CaseTag).where(CaseTag.case_id.in_(case_ids)))
        db.execute(delete(CaseComment).where(CaseComment.case_id.in_(case_ids)))
    db.execute(delete(Evidence).where(Evidence.tenant_id == tenant_id))
    db.execute(delete(Alert).where(Alert.tenant_id == tenant_id))
    db.execute(delete(Approval).where(Approval.tenant_id == tenant_id))
    if workflow_ids:
        db.execute(delete(WorkflowTask).where(WorkflowTask.workflow_id.in_(workflow_ids)))
    db.execute(delete(WorkflowInstance).where(WorkflowInstance.tenant_id == tenant_id))
    if screening_ids:
        db.execute(delete(ScreeningMatch).where(ScreeningMatch.screening_id.in_(screening_ids)))
    db.execute(delete(ScreeningCheck).where(ScreeningCheck.tenant_id == tenant_id))
    db.execute(delete(RiskAssessment).where(RiskAssessment.tenant_id == tenant_id))
    db.execute(delete(Transaction).where(Transaction.tenant_id == tenant_id))
    db.execute(delete(IdentityDocument).where(IdentityDocument.tenant_id == tenant_id))
    db.execute(delete(KycCycle).where(KycCycle.tenant_id == tenant_id))
    db.execute(delete(VerificationCheck).where(VerificationCheck.tenant_id == tenant_id))
    db.execute(delete(Party).where(Party.tenant_id == tenant_id))
    if customer_ids:
        db.execute(delete(IndividualProfile).where(IndividualProfile.customer_id.in_(customer_ids)))
        db.execute(delete(BusinessProfile).where(BusinessProfile.customer_id.in_(customer_ids)))
    db.execute(delete(CaseRecord).where(CaseRecord.tenant_id == tenant_id))
    db.execute(delete(Customer).where(Customer.tenant_id == tenant_id))
    db.execute(delete(IngestedObject).where(IngestedObject.tenant_id == tenant_id, IngestedObject.object_type.in_(("clients", "counterparties"))))
    return {"customers": len(customer_ids), "verifications": len(workflow_ids), "files": removed_files}


def _progress_verification(db: Session, engine, tenant: Tenant, customer: Customer, workflow: WorkflowInstance) -> None:
    _advance_if_ready(db, engine, workflow)
    _advance_if_ready(db, engine, workflow)
    _screen_customer(db, tenant, customer)
    pending = _customer_has_pending_screening(db, customer.id)
    if not pending:
        moved = _advance_if_ready(db, engine, workflow)
        assess_customer(db, tenant, customer, source="ruleset")
        if moved:
            _advance_if_ready(db, engine, workflow)
        customer.canonical_status = "in_review"
    else:
        customer.canonical_status = "screening_pending"
        assess_customer(db, tenant, customer, source="ruleset")


def _continue_verification(
    db: Session,
    engine,
    tenant: Tenant,
    principal: Principal,
    customer: Customer,
    workflow: WorkflowInstance,
    kind: str,
    data: dict,
    context: dict,
    config: dict | None,
    definition: str,
    hold_at_start: bool,
) -> tuple[Customer, WorkflowInstance]:
    """Keep the same verification line. A later step replaces the state, not the row."""
    from sqlalchemy.orm.attributes import flag_modified

    _sync_customer(db, customer, kind, data)
    workflow.context = _merge_context(workflow.context, context)
    flag_modified(workflow, "context")
    workflow.config = {**(workflow.config or {}), **(config or {})}
    flag_modified(workflow, "config")
    workflow.updated_at = utcnow()
    if workflow.definition_key == definition or hold_at_start:
        open_kyc_cycle(db, customer, workflow.id)
        audit(db, tenant.id, actor_label(principal), "workflow.resume", "workflow", workflow.id, {"definition": definition})
        return customer, workflow
    instance_id, task = engine.start(definition, customer.id, {})
    workflow.definition_key = definition
    workflow.definition_name = PROCESS_CATALOG[definition]["name"]
    workflow.engine = engine.mode
    workflow.engine_instance_id = instance_id
    _sync_task(db, workflow, task)
    open_kyc_cycle(db, customer, workflow.id)
    _progress_verification(db, engine, tenant, customer, workflow)
    audit(db, tenant.id, actor_label(principal), "workflow.continue", "workflow", workflow.id, {"definition": definition})
    return customer, workflow


def start_customer_workflow(
    db: Session,
    engine,
    tenant: Tenant,
    principal: Principal,
    *,
    kind: str,
    external_ref: str | None,
    data: dict,
    context: dict,
    config: dict | None,
    synthetic: bool,
    definition_key: str | None = None,
    hold_at_start: bool = False,
) -> tuple[Customer, WorkflowInstance]:
    customer = None
    if external_ref:
        customer = db.scalar(select(Customer).where(Customer.tenant_id == tenant.id, Customer.external_ref == external_ref))
    if customer is None:
        customer = find_customer_by_contact(db, tenant.id, kind, data)
    definition = definition_key or ("clarityKycIndividual" if kind == "individual" else "clarityKybBusiness")
    if definition not in PROCESS_CATALOG:
        raise clarity_error(400, "unknown_workflow", "Cette définition de workflow n'a pas de processus exécutable.")
    if customer is not None:
        active = _open_verification(db, customer)
        if active is not None:
            return _continue_verification(
                db, engine, tenant, principal, customer, active, kind, data, context, config, definition, hold_at_start
            )
        if _kyc_is_current(db, customer):
            raise clarity_error(409, "kyc_valid", "La vérification de ce client est encore valable. Une mise à jour est possible après son expiration.")
    if customer is None:
        customer = Customer(
            tenant_id=tenant.id,
            kind=kind,
            display_name=_display_name(kind, data),
            external_ref=external_ref,
            canonical_status="collecting",
            purpose=data.get("purpose"),
            expected_activity=data.get("expected_activity") or data.get("expectedActivity"),
            source_of_funds=data.get("source_of_funds") or data.get("sourceOfFunds"),
            synthetic=synthetic,
            extension={"upstream_context_retained": True},
        )
        db.add(customer)
        db.flush()
    else:
        customer.canonical_status = "collecting"
    _sync_customer(db, customer, kind, data)
    if kind == "business" and customer.business is not None and not db.scalar(select(Party).where(Party.business_customer_id == customer.id)):
        _parties(db, tenant.id, customer.id, data)
    instance_id, task = engine.start(definition, customer.id, {})
    workflow = WorkflowInstance(
        tenant_id=tenant.id,
        customer_id=customer.id,
        definition_key=definition,
        definition_name=PROCESS_CATALOG[definition]["name"],
        engine=engine.mode,
        engine_instance_id=instance_id,
        ballerine_runtime_id=str(uuid.uuid4()),
        context=context,
        config=config or {},
        created_by=actor_label(principal),
    )
    db.add(workflow)
    db.flush()
    _sync_task(db, workflow, task)
    open_kyc_cycle(db, customer, workflow.id)
    if hold_at_start:
        customer.canonical_status = "collecting"
        audit(db, tenant.id, actor_label(principal), "workflow.start", "workflow", workflow.id, {"definition": definition, "held": True})
        return customer, workflow
    _progress_verification(db, engine, tenant, customer, workflow)
    audit(db, tenant.id, actor_label(principal), "workflow.start", "workflow", workflow.id, {"definition": definition})
    return customer, workflow


def _display_name(kind: str, data: dict) -> str:
    if kind == "business":
        return data.get("legal_name") or data.get("companyName") or data.get("name") or "Personne morale"
    return data.get("legal_name") or " ".join(
        part for part in [data.get("firstName") or data.get("given_name"), data.get("lastName") or data.get("family_name")] if part
    ) or data.get("name") or "Personne physique"


def _individual(customer_id: str, data: dict) -> IndividualProfile:
    given = data.get("given_name") or data.get("firstName") or ""
    family = data.get("family_name") or data.get("lastName") or ""
    additional = data.get("additionalInfo") if isinstance(data.get("additionalInfo"), dict) else {}
    address = data.get("address") or additional.get("address")
    if isinstance(address, dict):
        address = ", ".join(str(address.get(key) or "") for key in ("street", "city", "postalCode", "country") if address.get(key))
    return IndividualProfile(
        customer_id=customer_id,
        legal_name=_display_name("individual", data),
        given_name=given,
        family_name=family,
        date_of_birth=data.get("date_of_birth") or data.get("dateOfBirth"),
        nationality=data.get("nationality") or additional.get("nationality"),
        residence_country=data.get("residence_country") or data.get("country") or additional.get("country"),
        address=address,
        email=data.get("email"),
        phone=data.get("phone"),
    )


def _business(customer_id: str, data: dict) -> BusinessProfile:
    return BusinessProfile(
        customer_id=customer_id,
        legal_name=data.get("legal_name") or data.get("companyName") or data.get("name") or "",
        trading_name=data.get("trading_name") or data.get("tradingName"),
        registration_number=data.get("registration_number") or data.get("registrationNumber"),
        jurisdiction=data.get("jurisdiction") or data.get("country"),
        legal_form=data.get("legal_form") or data.get("legalForm"),
        registered_address=data.get("registered_address") or data.get("address"),
        industry=data.get("industry"),
        activities=data.get("activities") or data.get("businessActivity"),
    )


def _parties(db: Session, tenant_id: str, business_id: str, data: dict) -> None:
    for director in data.get("directors") or []:
        db.add(
            Party(
                tenant_id=tenant_id,
                business_customer_id=business_id,
                role="director",
                full_name=director.get("name") or director.get("full_name") or "",
                date_of_birth=director.get("date_of_birth") or director.get("dateOfBirth"),
                nationality=director.get("nationality"),
            )
        )
    for owner in data.get("beneficial_owners") or data.get("ubos") or []:
        db.add(
            Party(
                tenant_id=tenant_id,
                business_customer_id=business_id,
                role="beneficial_owner",
                full_name=owner.get("name") or owner.get("full_name") or "",
                date_of_birth=owner.get("date_of_birth") or owner.get("dateOfBirth"),
                nationality=owner.get("nationality"),
                ownership_pct=owner.get("ownership_pct") or owner.get("ownershipPercentage"),
            )
        )


def _screen_customer(db: Session, tenant: Tenant, customer: Customer) -> ScreeningCheck:
    if customer.kind == "individual" and customer.individual:
        profile = customer.individual
        query = {
            "Person": {
                "name": profile.legal_name,
                "birthDate": profile.date_of_birth or "",
                "nationality": profile.nationality or "",
            }
        }
    else:
        profile = customer.business
        query = {"Organization": {"name": profile.legal_name if profile else customer.display_name, "country": profile.jurisdiction if profile else ""}}
    screening = persist_screening(
        db,
        tenant,
        query,
        rule_id=None,
        decision_id=None,
        customer_id=customer.id,
        counterparty=customer.external_ref or customer.id,
    )
    for party in db.scalars(select(Party).where(Party.business_customer_id == customer.id)).all():
        persist_screening(
            db,
            tenant,
            {"Person": {"name": party.full_name, "birthDate": party.date_of_birth or "", "nationality": party.nationality or ""}},
            rule_id=None,
            decision_id=None,
            customer_id=customer.id,
            counterparty=f"{customer.id}:{party.id}",
        )
    return screening


def _require_approval_ready(db: Session, workflow: WorkflowInstance) -> None:
    if not workflow.customer_id:
        raise clarity_error(422, "documents_pending", "Toutes les pièces doivent être approuvées avant la validation du dossier.")
    documents = list(db.scalars(select(IdentityDocument).where(IdentityDocument.customer_id == workflow.customer_id)).all())
    owned = [document for document in documents if document.workflow_id == workflow.id]
    if not owned:
        owned = documents
    if not owned or any(document.decision != "approved" for document in owned):
        raise clarity_error(422, "documents_pending", "Toutes les pièces doivent être approuvées avant la validation du dossier.")
    if _customer_has_pending_screening(db, workflow.customer_id):
        raise clarity_error(422, "screening_pending", "Des correspondances de criblage ne sont pas encore vérifiées.")


def _customer_has_pending_screening(db: Session, customer_id: str) -> bool:
    screening_ids = list(db.scalars(select(ScreeningCheck.id).where(ScreeningCheck.customer_id == customer_id)).all())
    if not screening_ids:
        return False
    pending = db.scalar(
        select(ScreeningMatch).where(ScreeningMatch.screening_id.in_(screening_ids), ScreeningMatch.status == "pending")
    )
    return pending is not None


def assess_customer(db: Session, tenant: Tenant, customer: Customer, source: str = "ruleset") -> RiskAssessment:
    factors = []
    score = 0
    country = None
    if customer.individual:
        country = customer.individual.residence_country or customer.individual.nationality
    elif customer.business:
        country = customer.business.jurisdiction
    demo_high = {"KP", "IR", "SY"}
    if country in demo_high:
        score += 25
        factors.append({"code": "country", "points": 25, "explanation": f"Pays {country} présent dans la liste interne de démonstration du tenant. Cette liste n'est pas une publication officielle."})
    if customer.kind == "business":
        owners = db.scalars(select(Party).where(Party.business_customer_id == customer.id, Party.role == "beneficial_owner")).all()
        if any((owner.ownership_pct or 0) >= 25 for owner in owners) and len(owners) > 2:
            score += 10
            factors.append({"code": "ownership", "points": 10, "explanation": "Structure d'actionnariat avec plusieurs bénéficiaires effectifs."})
    pending = _customer_has_pending_screening(db, customer.id)
    confirmed = False
    screening_ids = list(db.scalars(select(ScreeningCheck.id).where(ScreeningCheck.customer_id == customer.id)).all())
    if screening_ids:
        confirmed = (
            db.scalar(
                select(ScreeningMatch).where(
                    ScreeningMatch.screening_id.in_(screening_ids), ScreeningMatch.status == "confirmed_hit"
                )
            )
            is not None
        )
    if pending:
        score += 15
        factors.append(
            {
                "code": "screening_candidate",
                "points": 15,
                "explanation": "Au moins une correspondance candidate est en attente. Le score augmente l'attention. Il ne refuse pas le client.",
            }
        )
    if confirmed:
        score += 40
        factors.append({"code": "screening_confirmed", "points": 40, "explanation": "Une correspondance a été confirmée manuellement."})
    if not factors:
        factors.append({"code": "baseline", "points": 0, "explanation": "Aucun facteur aggravant sur la méthodologie clarity-risk-1."})
    tier = "low"
    level = 1
    if score >= 60:
        tier, level = "high", 4
    elif score >= 30:
        tier, level = "medium", 2
    customer.risk_tier = tier
    previous = db.scalars(
        select(RiskAssessment).where(
            RiskAssessment.tenant_id == tenant.id,
            RiskAssessment.object_type == "customers",
            RiskAssessment.object_id == customer.id,
            RiskAssessment.current.is_(True),
        )
    ).all()
    for row in previous:
        row.current = False
        row.stale_at = utcnow()
    assessment = RiskAssessment(
        tenant_id=tenant.id,
        customer_id=customer.id,
        object_type="customers",
        object_id=customer.id,
        source=source,
        score=score,
        risk_level=level,
        tier=tier,
        factors=factors,
        current=True,
    )
    db.add(assessment)
    return assessment


def _account_hook_payload(db: Session, workflow: WorkflowInstance, customer: Customer) -> dict:
    """Payload the core-banking onboarding hook already accepts."""
    entity = dict((workflow.context or {}).get("entity") or {})
    stored = dict(entity.get("data") or {})
    individual = customer.individual
    data = {
        "email": stored.get("email") or (individual.email if individual else "") or "",
        "phone": stored.get("phone") or stored.get("phoneNumber") or (individual.phone if individual else "") or "",
        "country": stored.get("country") or (individual.nationality if individual else "") or (individual.residence_country if individual else "") or "",
        "lastName": stored.get("lastName") or (individual.family_name if individual else "") or "",
        "firstName": stored.get("firstName") or (individual.given_name if individual else "") or "",
        "dateOfBirth": stored.get("dateOfBirth") or (individual.date_of_birth if individual else "") or "",
    }
    documents = []
    rows = db.scalars(select(IdentityDocument).where(IdentityDocument.customer_id == customer.id)).all()
    owned = [document for document in rows if document.workflow_id == workflow.id] or list(rows)
    for document in owned:
        props = dict(document.properties or {})
        category = "proof_of_identity" if document.doc_type in {"passport", "id_card", "drivers_license"} else document.category
        decision = document.decision or "pending"
        if decision == "revisions":
            decision = "revision"
        documents.append(
            {
                "id": document.id,
                "type": document.doc_type,
                "version": document.version or 1,
                "category": category,
                "decision": {"status": decision, "revisionReason": document.decision_reason, "rejectionReason": None},
                "metadata": {},
                "properties": {
                    "sex": props.get("sex") or props.get("gender") or "",
                    "expires": document.expiry or props.get("expiry") or "",
                    "lastName": props.get("lastName") or data["lastName"],
                    "firstName": props.get("firstName") or data["firstName"],
                    "dateOfBirth": props.get("dateOfBirth") or data["dateOfBirth"],
                },
                "issuer": {"country": document.issuing_country or "ZZ"},
                "issuingVersion": document.issuing_version or 1,
            }
        )
    return {
        "id": str(uuid.uuid4()),
        "eventName": "workflow.completed",
        "apiVersion": "1",
        "timestamp": iso(utcnow()),
        "workflowCreatedAt": iso(workflow.created_at),
        "workflowResolvedAt": iso(workflow.resolved_at),
        "workflowDefinitionId": workflow.definition_key,
        "workflowRuntimeId": workflow.ballerine_runtime_id,
        "workflowStatus": workflow.status,
        "workflowFinalState": workflow.state,
        "ballerineEntityId": customer.id,
        "correlationId": customer.external_ref or customer.id,
        "data": {
            "entity": {
                "id": entity.get("id") or customer.external_ref or customer.id,
                "type": entity.get("type") or customer.kind,
                "ballerineEntityId": customer.id,
                "data": data,
            },
            "documents": documents,
            "metadata": {"customerId": customer.id, "customerName": customer.display_name},
        },
    }


def deliver_account_hook(db: Session, workflow: WorkflowInstance, customer: Customer) -> None:
    """Tell core banking that an approved client can be created. A delivery failure does not undo the approval."""
    import httpx
    from sqlalchemy.orm.attributes import flag_modified

    from app.config import current_settings
    from app.security import sign_payload

    payload = _account_hook_payload(db, workflow, customer)
    settings = current_settings()
    targets = [settings.core_banking_hook_url.strip()] if settings.core_banking_hook_url.strip() else []
    for subscription in (workflow.config or {}).get("subscriptions") or []:
        if not isinstance(subscription, dict):
            continue
        url = str(subscription.get("url") or "").strip()
        events = subscription.get("events") or ["workflow.completed"]
        if url and "workflow.completed" in events:
            targets.append(url)
    record: dict = {"event": "workflow.completed", "payload": payload, "delivered": False}
    if targets:
        signature = sign_payload(settings.unified_api_shared_secret, payload)
        for target in targets:
            try:
                response = httpx.post(
                    target,
                    json=payload,
                    headers={"Content-Type": "application/json", "x-hmac-signature": signature},
                    timeout=8,
                )
                record["url"] = target
                record["status"] = response.status_code
                record["delivered"] = response.status_code < 400
            except Exception as exc:  # noqa: BLE001
                record["url"] = target
                record["error"] = str(exc)
                record["delivered"] = False
    else:
        record["reason"] = "no_subscriber"
    context = dict(workflow.context or {})
    context["account_hook"] = record
    workflow.context = context
    flag_modified(workflow, "context")
    audit(db, customer.tenant_id, "clarity", "workflow.account_hook", "workflow", workflow.id, {"delivered": record["delivered"], "event": "workflow.completed"})


def _mark_partially_approved(db: Session, tenant: Tenant, customer: Customer, workflow: WorkflowInstance) -> None:
    """The client exists, but the account stays limited to its IBAN until the funding transfer is checked."""
    customer.canonical_status = "approved-partially"
    cycle = latest_kyc_cycle(db, customer.id)
    if cycle and cycle.status == "pending" and cycle.workflow_id in {None, workflow.id}:
        cycle.status = "approved-partially"
    deliver_account_hook(db, workflow, customer)


def _activate_funded_customer(db: Session, tenant: Tenant, customer: Customer, workflow: WorkflowInstance) -> None:
    """The incoming transfer was accepted, so the limited account becomes operational."""
    customer.canonical_status = "approved"
    years = tenant.kyc_validity_years or 1
    customer.next_review_at = utcnow() + timedelta(days=365 * years)
    partials = list(
        db.scalars(
            select(WorkflowInstance).where(
                WorkflowInstance.customer_id == customer.id,
                WorkflowInstance.state == "approved-partially",
            )
        ).all()
    )
    for partial in partials:
        partial.state = "approved"
        partial.status = "completed"
        partial.resolved_at = partial.resolved_at or utcnow()
    validate_kyc_cycle(db, customer, partials[0].id if partials else workflow.id, customer.next_review_at)


DOCUMENT_LABELS = {
    "passport": {"fr": "Passeport", "en": "Passport", "ar": "جواز السفر"},
    "id_card": {"fr": "Carte d'identité", "en": "Identity card", "ar": "بطاقة التعريف"},
    "id-card": {"fr": "Carte d'identité", "en": "Identity card", "ar": "بطاقة التعريف"},
    "drivers_license": {"fr": "Permis de conduire", "en": "Driving licence", "ar": "رخصة السياقة"},
    "residence_permit": {"fr": "Titre de séjour", "en": "Residence permit", "ar": "بطاقة الإقامة"},
    "proof_of_residence": {"fr": "Justificatif de résidence", "en": "Proof of residence", "ar": "إثبات الإقامة"},
    "proof_of_address": {"fr": "Justificatif de domicile", "en": "Proof of address", "ar": "إثبات العنوان"},
}


def document_labels(doc_type: str, spec: dict | None = None) -> dict:
    base = dict(DOCUMENT_LABELS.get(doc_type) or {"fr": doc_type, "en": doc_type, "ar": doc_type})
    i18n = (spec or {}).get("i18n") if isinstance((spec or {}).get("i18n"), dict) else {}
    label = i18n.get("label") if isinstance(i18n.get("label"), dict) else {}
    for lang in ("fr", "en", "ar"):
        text = str(label.get(lang) or "").strip()
        if text:
            base[lang] = text
    return base


def _document_schema(db: Session, workflow: WorkflowInstance) -> list[dict]:
    from app.catalog import list_docs

    schema: list[dict] = []
    for row in list_docs(db, workflow.tenant_id, "workflow_definition"):
        if row.payload.get("processKey") != workflow.definition_key:
            continue
        schema = list(row.payload.get("documentsSchema") or [])
        if row.name in {"kyc", "kyb", "kyc-demo"}:
            break
    return schema


def _mark_refused(db: Session, workflow: WorkflowInstance, customer: Customer) -> None:
    """A refused verification is an échec, not a finished one."""
    workflow.status = "failed"
    customer.canonical_status = "rejected"
    cycle = latest_kyc_cycle(db, customer.id)
    if cycle and cycle.status == "pending" and cycle.workflow_id in {None, workflow.id}:
        cycle.status = "rejected"


def append_workflow_note(workflow: WorkflowInstance, principal: Principal, comment: str, action: str | None = None) -> dict | None:
    """Persist a dossier note on the workflow context (analyst and checker)."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import new_id

    text = str(comment or "").strip()
    if not text:
        return None
    note = {
        "id": new_id(),
        "comment": text,
        "actor": actor_label(principal),
        "created_at": iso(utcnow()),
        "action": action,
    }
    context = dict(workflow.context or {})
    notes = list(context.get("notes") or [])
    notes.append(note)
    context["notes"] = notes
    workflow.context = context
    flag_modified(workflow, "context")
    workflow.updated_at = utcnow()
    return note


def refuse_verification(db: Session, engine, tenant: Tenant, principal: Principal, workflow: WorkflowInstance, rationale: str) -> WorkflowInstance:
    """Refuse the case even when it is not sitting on the analyst or checker step."""
    if not principal.allows("analyst", "investigator", "checker"):
        raise clarity_error(403, "forbidden", "Le refus exige le rôle analyste, investigateur ou checker.")
    if workflow.status == "failed" or workflow.state in {"approved", "approved-partially", "rejected", "closed"}:
        raise clarity_error(422, "closed", "Ce dossier est déjà terminé.")
    from app.engine import ADVANCE

    task = db.scalar(select(WorkflowTask).where(WorkflowTask.workflow_id == workflow.id, WorkflowTask.status == "open"))
    routes = ADVANCE.get(workflow.definition_key, {}).get(task.task_key, {}) if task else {}
    if task and "reject" in routes:
        return advance_workflow(db, engine, tenant, principal, workflow, "reject", rationale)
    append_workflow_note(workflow, principal, rationale, "reject")
    workflow.state = "rejected"
    workflow.resolved_at = utcnow()
    _sync_task(db, workflow, None)
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    if customer:
        _mark_refused(db, workflow, customer)
    else:
        workflow.status = "failed"
    audit(db, tenant.id, actor_label(principal), "workflow.action", "workflow", workflow.id, {"action": "reject", "state": workflow.state})
    return workflow


def request_documents(db: Session, engine, tenant: Tenant, principal: Principal, workflow: WorkflowInstance, items: list, reason: str) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise clarity_error(400, "invalid", "Indiquez au moins une pièce à demander.")
    if workflow.status == "failed" or workflow.state in {"approved", "approved-partially", "rejected", "closed"}:
        raise clarity_error(422, "closed", "Ce dossier est déjà terminé.")
    if not principal.allows("analyst", "investigator", "checker"):
        raise clarity_error(403, "forbidden", "La demande de pièce exige le rôle analyste, investigateur ou checker.")
    task = db.scalar(select(WorkflowTask).where(WorkflowTask.workflow_id == workflow.id, WorkflowTask.status == "open"))
    schema = _document_schema(db, workflow)
    requested = []
    for item in items:
        if not isinstance(item, dict):
            raise clarity_error(400, "invalid", "Chaque demande doit décrire une pièce.")
        if item.get("document_id"):
            document = db.get(IdentityDocument, str(item["document_id"]))
            if not document or document.tenant_id != principal.tenant_id or document.workflow_id != workflow.id:
                raise clarity_error(404, "not_found", "Pièce introuvable sur ce dossier.")
            spec = next((row for row in schema if row.get("type") == document.doc_type and row.get("category") == document.category), None)
            document.status = "requested"
            document.decision = "revisions"
            document.decision_reason = reason or "Pièce à renvoyer"
            mode = "resubmit"
        else:
            doc_type = str(item.get("type") or "")
            category = str(item.get("category") or "")
            spec = next((row for row in schema if row.get("type") == doc_type and row.get("category") == category), None)
            if not spec:
                raise clarity_error(422, "unknown_document", "Ce type n'est pas dans la liste de pièces du parcours.")
            issuer = spec.get("issuer") if isinstance(spec.get("issuer"), dict) else {}
            document = IdentityDocument(
                tenant_id=principal.tenant_id,
                customer_id=workflow.customer_id,
                workflow_id=workflow.id,
                category=category,
                doc_type=doc_type,
                issuing_country=str(item.get("country") or issuer.get("country") or "ZZ"),
                issuing_version=str(spec.get("version") or "1"),
                status="requested",
                decision="revisions",
                decision_reason=reason or "Pièce complémentaire",
                properties={"title": document_labels(doc_type, spec)["fr"]},
            )
            db.add(document)
            db.flush()
            mode = "additional"
        requested.append(
            {
                "id": document.id,
                "type": document.doc_type,
                "category": document.category,
                "country": document.issuing_country,
                "mode": mode,
                "reason": reason,
                "label": document_labels(document.doc_type, spec),
            }
        )
    from sqlalchemy.orm.attributes import flag_modified

    context = dict(workflow.context or {})
    context["documentsRequested"] = requested
    workflow.context = context
    flag_modified(workflow, "context")
    from app.engine import ADVANCE

    routes = ADVANCE.get(workflow.definition_key, {}).get(task.task_key, {}) if task else {}
    if task and "requestDocuments" in routes:
        advance_workflow(db, engine, tenant, principal, workflow, "requestDocuments", reason or "Pièces demandées")
    else:
        workflow.state = "documentsRequested"
        customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
        if customer:
            customer.canonical_status = "documents_requested"
        audit(db, tenant.id, actor_label(principal), "workflow.action", "workflow", workflow.id, {"action": "requestDocuments", "state": workflow.state})
    return requested


def store_liveness_video(db: Session, principal: Principal, workflow: WorkflowInstance, content: bytes, filename: str, mime: str) -> IdentityDocument:
    from app.config import ROOT

    if not content:
        raise clarity_error(422, "invalid", "La vidéo est vide.")
    if len(content) > 12 * 1024 * 1024:
        raise clarity_error(413, "too_large", "La vidéo dépasse 12 Mo. Envoyez un fichier compressé.")
    allowed = {"video/mp4", "video/webm", "video/quicktime"}
    name = filename or "liveness.mp4"
    kind = (mime or "").split(";")[0].strip().lower()
    if kind not in allowed and not name.lower().endswith((".mp4", ".webm", ".mov")):
        raise clarity_error(415, "unsupported", "Le fichier doit être une vidéo mp4, webm ou mov.")
    path, digest = store_bytes(ROOT / "data" / "uploads", principal.tenant_id, name, content, db=db)
    document = IdentityDocument(
        tenant_id=principal.tenant_id,
        customer_id=workflow.customer_id,
        workflow_id=workflow.id,
        category="biometric",
        doc_type="liveness_video",
        issuing_country="ZZ",
        status="provided",
        properties={"title": "Liveness", "fileName": name, "mime": kind or "video/mp4"},
        storage_path=path,
        sha256=digest,
    )
    db.add(document)
    db.flush()
    audit(db, principal.tenant_id, actor_label(principal), "liveness.upload", "document", document.id, {"bytes": len(content)})
    return document


def finish_document_request_if_ready(db: Session, engine, tenant: Tenant, principal: Principal, workflow: WorkflowInstance) -> bool:
    if workflow.state != "documentsRequested":
        return False
    requested = list((workflow.context or {}).get("documentsRequested") or [])
    ids = [str(item.get("id")) for item in requested if item.get("id")]
    if not ids:
        return False
    documents = list(db.scalars(select(IdentityDocument).where(IdentityDocument.id.in_(ids))).all())
    if len(documents) != len(ids) or any(document.status == "requested" or not document.storage_path for document in documents):
        return False
    task = db.scalar(select(WorkflowTask).where(WorkflowTask.workflow_id == workflow.id, WorkflowTask.status == "open"))
    if task and task.task_key == "documentsRequested":
        advance_workflow(db, engine, tenant, principal, workflow, "submit", "Pièces reçues")
    else:
        workflow.status = "active"
        _sync_task(
            db,
            workflow,
            {"taskDefinitionKey": "analystReview", "name": "Revue analyste", "id": f"local:{workflow.id}:analystReview"},
        )
        customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
        if customer:
            customer.canonical_status = "in_review"
    from sqlalchemy.orm.attributes import flag_modified

    context = dict(workflow.context or {})
    context["documentsRequested"] = []
    workflow.context = context
    flag_modified(workflow, "context")
    return True


def advance_workflow(db: Session, engine, tenant: Tenant, principal: Principal, workflow: WorkflowInstance, action: str, rationale: str) -> WorkflowInstance:
    task = db.scalar(select(WorkflowTask).where(WorkflowTask.workflow_id == workflow.id, WorkflowTask.status == "open"))
    if not task:
        raise clarity_error(422, "no_task", "Le dossier n'a pas de tâche ouverte.")
    human = {"analystReview", "checkerReview", "remediate", "investigate"}
    if task.task_key in human and principal.user_id is None and principal.role == "service":
        raise clarity_error(403, "human_required", "Cette étape exige un utilisateur nommé, pas une clé d'API.")
    if task.task_key == "analystReview" and not principal.allows("analyst", "investigator"):
        raise clarity_error(403, "forbidden", "La revue manuelle exige le rôle analyste ou investigateur.")
    if task.task_key in {"checkerReview"} and not principal.allows("checker"):
        raise clarity_error(403, "forbidden", "L'approbation exige le rôle checker.")
    context = dict(workflow.context or {})
    maker = context.get("maker_id")
    identity = principal.user_id or principal.credential_id
    if task.task_key == "checkerReview" and maker and maker == identity:
        raise clarity_error(403, "maker_checker", "La personne qui a instruit le dossier ne peut pas l'approuver.")
    if action in {"approve", "approvePartial"} and workflow.definition_key != "clarityFundingOrigin":
        _require_approval_ready(db, workflow)
    if action == "approvePartial" and task.task_key != "checkerReview":
        raise clarity_error(403, "forbidden", "L'approbation partielle est réservée au contrôleur.")
    if task.task_key == "analystReview":
        context["maker_id"] = identity
        workflow.context = context
    if action in {"approve", "reject", "approvePartial"} and task.task_key == "checkerReview":
        db.add(
            Approval(
                tenant_id=tenant.id,
                workflow_id=workflow.id,
                maker_id=maker,
                checker_id=principal.user_id,
                decision=action,
                rationale=rationale,
            )
        )
    append_workflow_note(workflow, principal, rationale, action)
    _complete_active(db, engine, workflow, {"action": action, "rationale": rationale})
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    if customer and workflow.state == "approved" and workflow.definition_key == "clarityFundingOrigin":
        _activate_funded_customer(db, tenant, customer, workflow)
    elif customer and workflow.state == "approved":
        customer.canonical_status = "approved"
        years = tenant.kyc_validity_years or 1
        customer.next_review_at = utcnow() + timedelta(days=365 * years)
        validate_kyc_cycle(db, customer, workflow.id, customer.next_review_at)
        deliver_account_hook(db, workflow, customer)
    elif customer and workflow.state == "approved-partially":
        _mark_partially_approved(db, tenant, customer, workflow)
    elif customer and workflow.state == "rejected":
        _mark_refused(db, workflow, customer)
    elif customer and action == "remediate":
        customer.canonical_status = "remediation"
    elif customer and action == "requestDocuments":
        customer.canonical_status = "documents_requested"
    elif customer and action == "submit" and workflow.state == "analystReview":
        customer.canonical_status = "in_review"
    audit(db, tenant.id, actor_label(principal), "workflow.action", "workflow", workflow.id, {"action": action, "state": workflow.state})
    return workflow


def disposition_match(db: Session, tenant: Tenant, principal: Principal, match: ScreeningMatch, status: str, whitelist: bool) -> ScreeningMatch:
    if status not in {"no_hit", "confirmed_hit"}:
        raise marble_error(400, "invalid_payload", "status must be no_hit or confirmed_hit")
    if match.status != "pending":
        raise marble_error(422, "unprocessable_entity", "match is not pending")
    match.status = status
    screening = db.get(ScreeningCheck, match.screening_id)
    if screening:
        siblings = db.scalars(select(ScreeningMatch).where(ScreeningMatch.screening_id == screening.id)).all()
        if any(item.status == "confirmed_hit" for item in siblings):
            screening.status = "confirmed_hit"
        elif any(item.status == "pending" for item in siblings):
            screening.status = "in_review"
        else:
            screening.status = "no_hit"
        screening.updated_at = utcnow()
        if whitelist and status == "no_hit":
            counterparty = ""
            if screening.customer_id:
                customer = db.get(Customer, screening.customer_id)
                counterparty = (customer.external_ref if customer and customer.external_ref else screening.customer_id)
            db.add(WhitelistEntry(tenant_id=tenant.id, counterparty=counterparty or match.entity_id, entity_id=match.entity_id))
        if screening.customer_id:
            customer = db.get(Customer, screening.customer_id)
            if customer:
                assess_customer(db, tenant, customer, source="ruleset")
                if not _customer_has_pending_screening(db, customer.id):
                    workflow = db.scalar(
                        select(WorkflowInstance).where(
                            WorkflowInstance.customer_id == customer.id,
                            WorkflowInstance.status == "active",
                            WorkflowInstance.state == "screen",
                        )
                    )
                    if workflow:
                        customer.canonical_status = "in_review"
    audit(db, tenant.id, actor_label(principal), "screening.disposition", "screening_match", match.id, {"status": status})
    return match


def case_payload(db: Session, case: CaseRecord) -> dict:
    inbox = db.get(Inbox, case.inbox_id)
    assignee = db.get(User, case.assignee_id) if case.assignee_id else None
    tags = []
    for link in db.scalars(select(CaseTag).where(CaseTag.case_id == case.id)).all():
        tag = db.get(Tag, link.tag_id)
        if tag:
            tags.append({"id": tag.id, "name": tag.name})
    contributor_ids = {comment.user_id for comment in db.scalars(select(CaseComment).where(CaseComment.case_id == case.id)).all()}
    if case.assignee_id:
        contributor_ids.add(case.assignee_id)
    contributors = []
    for user_id in contributor_ids:
        if not user_id:
            continue
        user = db.get(User, user_id)
        if user:
            contributors.append({"id": user.id, "name": user.full_name})
    return {
        "id": case.id,
        "inbox": {"id": inbox.id, "name": inbox.name} if inbox else {"id": case.inbox_id},
        "name": case.name,
        "assignee": {"id": assignee.id, "name": assignee.full_name} if assignee else None,
        "status": case.status,
        "outcome": case.outcome,
        "contributors": contributors,
        "tags": tags,
        "snoozed_until": iso(case.snoozed_until),
        "review_level": case.review_level,
        "created_at": iso(case.created_at),
    }


def store_bytes(root: Path, tenant_id: str, filename: str, content: bytes, db: Session | None = None) -> tuple[str, str]:
    from app.storage import store_bytes as persist

    return persist(root, tenant_id, filename, content, db=db)


def workflow_list_item(workflow: WorkflowInstance, customer: Customer | None) -> dict:
    given = ""
    family = ""
    if customer and customer.individual:
        given = customer.individual.given_name
        family = customer.individual.family_name
    elif customer and customer.business:
        given = customer.business.legal_name
    return {
        "id": workflow.ballerine_runtime_id,
        "firstName": given,
        "lastName": family,
        "workflowDefinitionName": workflow.definition_name,
        "workflowDefinitionId": workflow.definition_key,
        "workflowDefinition": {"id": workflow.definition_key, "name": workflow.definition_name},
        "status": workflow.status,
        "context": workflow.context,
        "config": workflow.config,
        "state": workflow.state,
        "createdBy": workflow.created_by,
        "createdAt": iso(workflow.created_at),
        "resolvedAt": iso(workflow.resolved_at),
        "updatedAt": iso(workflow.updated_at),
        "businessId": customer.id if customer and customer.kind == "business" else None,
        "endUserId": customer.id if customer and customer.kind == "individual" else None,
    }


def document_record(document: IdentityDocument) -> dict:
    return {
        "id": document.id,
        "category": document.category,
        "type": document.doc_type,
        "issuingVersion": document.issuing_version,
        "issuingCountry": document.issuing_country,
        "version": document.version,
        "status": document.status,
        "decision": document.decision,
        "decisionReason": document.decision_reason,
        "comment": document.comment,
        "properties": document.properties,
        "workflowRuntimeDataId": document.workflow_id,
        "projectId": document.tenant_id,
    }


def customer_view(db: Session, customer: Customer) -> dict:
    screenings = db.scalars(select(ScreeningCheck).where(ScreeningCheck.customer_id == customer.id)).all()
    matches = []
    for screening in screenings:
        matches.extend(db.scalars(select(ScreeningMatch).where(ScreeningMatch.screening_id == screening.id)).all())
    risk = db.scalar(
        select(RiskAssessment).where(
            RiskAssessment.customer_id == customer.id,
            RiskAssessment.current.is_(True),
        )
    )
    documents = db.scalars(select(IdentityDocument).where(IdentityDocument.customer_id == customer.id)).all()
    parties = db.scalars(select(Party).where(Party.business_customer_id == customer.id)).all()
    checks = db.scalars(select(VerificationCheck).where(VerificationCheck.customer_id == customer.id)).all()
    refresh_kyc_cycles(db, customer.tenant_id)
    cycles = db.scalars(select(KycCycle).where(KycCycle.customer_id == customer.id).order_by(KycCycle.created_at.desc())).all()
    workflows = {
        item.id: item
        for item in db.scalars(select(WorkflowInstance).where(WorkflowInstance.customer_id == customer.id)).all()
    }
    grouped: dict[str, list] = {}
    claimed = {row.workflow_id for row in cycles if row.workflow_id}
    unclaimed: list = []
    for document in documents:
        piece = {
            "id": document.id,
            "title": str((document.properties or {}).get("title") or document.doc_type or "Pièce"),
            "type": document.doc_type,
            "decision": document.decision,
            "url": f"/api/clarity/v1/desks/documents/{document.id}/content",
        }
        if document.workflow_id and document.workflow_id in claimed:
            grouped.setdefault(document.workflow_id, []).append(piece)
        else:
            unclaimed.append(piece)
    fallback = None
    if workflows:
        fallback = max(workflows.values(), key=lambda item: item.created_at or item.id).ballerine_runtime_id
    cycle_views = []
    for index, row in enumerate(cycles):
        owned = list(grouped.get(row.workflow_id or "", []))
        if index == 0:
            owned.extend(unclaimed)
        runtime = workflows[row.workflow_id].ballerine_runtime_id if row.workflow_id and row.workflow_id in workflows else None
        if runtime is None and index == 0:
            runtime = fallback
        cycle_views.append(kyc_cycle_view(row, runtime, owned))
    return {
        "id": customer.id,
        "kind": customer.kind,
        "display_name": customer.display_name,
        "external_ref": customer.external_ref,
        "status": customer.canonical_status,
        "risk_tier": customer.risk_tier,
        "purpose": customer.purpose,
        "expected_activity": customer.expected_activity,
        "source_of_funds": customer.source_of_funds,
        "synthetic": customer.synthetic,
        "next_review_at": iso(customer.next_review_at),
        "individual": None
        if not customer.individual
        else {
            "legal_name": customer.individual.legal_name,
            "date_of_birth": customer.individual.date_of_birth,
            "nationality": customer.individual.nationality,
            "residence_country": customer.individual.residence_country,
            "address": customer.individual.address,
            "email": customer.individual.email,
            "phone": customer.individual.phone,
        },
        "business": None
        if not customer.business
        else {
            "legal_name": customer.business.legal_name,
            "trading_name": customer.business.trading_name,
            "registration_number": customer.business.registration_number,
            "jurisdiction": customer.business.jurisdiction,
            "legal_form": customer.business.legal_form,
            "registered_address": customer.business.registered_address,
            "industry": customer.business.industry,
            "activities": customer.business.activities,
        },
        "parties": [
            {
                "id": party.id,
                "role": party.role,
                "full_name": party.full_name,
                "ownership_pct": float(party.ownership_pct) if party.ownership_pct is not None else None,
                "nationality": party.nationality,
            }
            for party in parties
        ],
        "documents": [document_record(document) for document in documents],
        "kyc_cycles": cycle_views,
        "verification_checks": [
            {
                "id": check.id,
                "provider": check.provider,
                "kind": check.kind,
                "status": check.status,
                "production": check.production,
                "detail": check.detail,
            }
            for check in checks
        ],
        "screening": [
            {
                "id": item.id,
                "status": item.status,
                "match_count": item.match_count,
                "production": item.production,
                "provider": item.provider,
                "matches": [
                    {
                        "id": match.id,
                        "status": match.status,
                        "score": match.score,
                        "explanation": match.explanation,
                        "entity_id": match.entity_id,
                        "name": str((match.payload or {}).get("caption") or (match.payload or {}).get("name") or match.entity_id),
                        "synthetic": (match.payload or {}).get("synthetic", False),
                    }
                    for match in matches
                    if match.screening_id == item.id
                ],
            }
            for item in screenings
        ],
        "risk": None
        if not risk
        else {
            "id": risk.id,
            "methodology_version": risk.methodology_version,
            "source": risk.source,
            "score": risk.score,
            "tier": risk.tier,
            "risk_level": risk.risk_level,
            "factors": risk.factors,
            "rationale": risk.rationale,
        },
    }
