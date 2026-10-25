import hashlib
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
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
    return status


def kyc_cycle_view(row: KycCycle) -> dict:
    return {
        "id": row.id,
        "customer_id": row.customer_id,
        "workflow_id": row.workflow_id,
        "status": row.status,
        "label": kyc_label(row.status),
        "pending": row.status in {"pending", "overdue"},
        "created_at": iso(row.created_at),
        "validated_at": iso(row.validated_at),
        "expires_at": iso(row.expires_at),
        "reminder": row.reminder,
        "reminded_at": iso(row.reminded_at),
        "production": row.production,
    }


def latest_kyc_cycle(db: Session, customer_id: str) -> KycCycle | None:
    return db.scalar(select(KycCycle).where(KycCycle.customer_id == customer_id).order_by(KycCycle.created_at.desc()))


def open_kyc_cycle(db: Session, customer: Customer, workflow_id: str | None = None) -> KycCycle:
    current = latest_kyc_cycle(db, customer.id)
    if current and current.status == "pending":
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
                notify(db, tenant_id, "Relance KYC", f"{name} : la vérification est expirée. Relance automatique enregistrée.")
        if row.status == "expired" and expires + timedelta(days=KYC_OVERDUE_DAYS) <= now:
            row.status = "overdue"


def remind_kyc_cycle(db: Session, customer: Customer, cycle_id: str) -> KycCycle:
    refresh_kyc_cycles(db, customer.tenant_id)
    row = db.get(KycCycle, cycle_id)
    if not row or row.customer_id != customer.id:
        raise clarity_error(404, "not_found", "Vérification introuvable.")
    if row.status == "valid":
        raise clarity_error(422, "not_due", "La relance intervient à l'expiration de la vérification.")
    row.reminder = "manual"
    row.reminded_at = utcnow()
    notify(db, customer.tenant_id, "Relance KYC manuelle", f"{customer.display_name} : relance manuelle enregistrée.")
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
    return created


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
        terminal = variables.get("action")
        if terminal == "approve":
            terminal = "approved"
        elif terminal == "reject":
            terminal = "rejected"
        elif terminal == "close":
            terminal = "closed"
        else:
            terminal = "completed"
        workflow.status = "completed"
        workflow.state = terminal if terminal in TERMINAL or terminal == "completed" else terminal
        workflow.resolved_at = utcnow()
        _sync_task(db, workflow, None)
        if workflow.state == "completed":
            workflow.state = "closed"
        return
    _sync_task(db, workflow, nxt[0])


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
    definition = definition_key or ("clarityKycIndividual" if kind == "individual" else "clarityKybBusiness")
    if definition not in PROCESS_CATALOG:
        raise clarity_error(400, "unknown_workflow", "Cette définition de workflow n'a pas de processus exécutable.")
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
        if kind == "individual":
            profile = _individual(customer.id, data)
            customer.individual = profile
            db.add(profile)
        else:
            profile = _business(customer.id, data)
            customer.business = profile
            db.add(profile)
            db.flush()
            _parties(db, tenant.id, customer.id, data)
    else:
        customer.updated_at = utcnow()
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
    _advance_if_ready(db, engine, workflow)
    db.add(
        VerificationCheck(
            tenant_id=tenant.id,
            customer_id=customer.id,
            provider="clarity-development-identity",
            kind="identity_document",
            status="not_a_production_verification",
            production=False,
            detail={
                "notice": "Aucun prestataire de vérification d'identité, de document ou de vivacité n'est connecté. Ce contrôle ne doit pas être lu comme une vérification réelle."
            },
        )
    )
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
    if task.task_key == "analystReview":
        context["maker_id"] = identity
        workflow.context = context
    if action in {"approve", "reject"} and task.task_key == "checkerReview":
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
    _complete_active(db, engine, workflow, {"action": action, "rationale": rationale})
    customer = db.get(Customer, workflow.customer_id) if workflow.customer_id else None
    if customer and workflow.state == "approved":
        customer.canonical_status = "approved"
        years = {"high": 1, "medium": 3, "low": 5}.get(customer.risk_tier or "medium", 3)
        customer.next_review_at = utcnow() + timedelta(days=365 * years)
        validate_kyc_cycle(db, customer, workflow.id, customer.next_review_at)
    elif customer and workflow.state == "rejected":
        customer.canonical_status = "rejected"
    elif customer and action == "remediate":
        customer.canonical_status = "remediation"
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


def store_bytes(root: Path, tenant_id: str, filename: str, content: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(content).hexdigest()
    safe = Path(filename).name or "evidence.bin"
    folder = root / tenant_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest[:16]}-{safe}"
    path.write_bytes(content)
    return str(path), digest


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
        "kyc_cycles": [kyc_cycle_view(row) for row in cycles],
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
                        "synthetic": match.payload.get("synthetic", False),
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
