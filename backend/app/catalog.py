"""Configuration documents that Marble and Ballerine clients read and that execution uses."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ast_eval import AstError, eval_ast, node_evaluation
from app.engine import chart_needs_repair, definition_chart
from app.ids import (
    CS_CONFIG_ID,
    LIST_COUNTRY_ID,
    RULE_AMOUNT_ID,
    RULE_COUNTRY_ID,
    RULE_SCREEN_ID,
    SCENARIO_ID,
    SCENARIO_ITERATION_ID,
    SCORESET_ID,
    TENANT_ID,
    UI_KYC_ID,
    WEBHOOK_ID,
    WF_ALERT_ID,
    WF_KYB_ID,
    WF_KYC_DEMO_ID,
    WF_KYC_ID,
)
from app.models import ConfigDocument, DataModelField, Inbox, Scenario, Tag, new_id
from app.timeutil import iso, utcnow

FIELD_TYPES = {
    "amount": "Float",
    "updated_at": "Timestamp",
    "birth_date": "Timestamp",
    "object_id": "String",
}


def table_id(tenant_id: str, name: str) -> str:
    return str(uuid.uuid5(uuid.UUID(TENANT_ID), f"{tenant_id}:table:{name}"))


def add_doc(
    db: Session,
    tenant_id: str,
    kind: str,
    name: str,
    payload: dict,
    *,
    parent_id: str | None = None,
    status: str = "active",
    doc_id: str | None = None,
) -> ConfigDocument:
    row = ConfigDocument(
        id=doc_id or new_id(),
        tenant_id=tenant_id,
        kind=kind,
        parent_id=parent_id,
        name=name,
        status=status,
        payload=payload,
    )
    db.add(row)
    db.flush()
    return row


def get_doc(db: Session, tenant_id: str, kind: str, doc_id: str) -> ConfigDocument | None:
    row = db.get(ConfigDocument, doc_id)
    if not row or row.tenant_id != tenant_id or row.kind != kind:
        return None
    return row


def list_docs(db: Session, tenant_id: str, kind: str, parent_id: str | None = None) -> list[ConfigDocument]:
    query = select(ConfigDocument).where(ConfigDocument.tenant_id == tenant_id, ConfigDocument.kind == kind)
    if parent_id:
        query = query.where(ConfigDocument.parent_id == parent_id)
    return list(db.scalars(query.order_by(ConfigDocument.created_at.asc())).all())


def scenario_meta(db: Session, scenario: Scenario) -> dict:
    row = get_doc(db, scenario.tenant_id, "scenario_meta", scenario.id)
    return row.payload if row else {}


def scenario_dto(db: Session, scenario: Scenario) -> dict:
    meta = scenario_meta(db, scenario)
    body = {
        "id": scenario.id,
        "archived": bool(meta.get("archived", not scenario.active)),
        "created_at": meta.get("created_at") or iso(utcnow()),
        "deduplicate_batch_objects": bool(meta.get("deduplicate_batch_objects", False)),
        "description": meta.get("description") or "",
        "name": scenario.name,
        "organization_id": scenario.tenant_id,
        "trigger_object_type": scenario.trigger_object_type,
    }
    if meta.get("live_version_id"):
        body["live_version_id"] = meta["live_version_id"]
    return body


def touch(row: ConfigDocument) -> None:
    row.updated_at = utcnow()


def iteration_dto(row: ConfigDocument, with_body: bool) -> dict:
    payload = row.payload
    body = {
        "id": row.id,
        "scenario_id": payload["scenario_id"],
        "version": payload.get("version"),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "archived": bool(payload.get("archived", False)),
    }
    if with_body:
        body["body"] = payload.get("body") or {"rules": []}
    return body


def rule_dto(row: ConfigDocument) -> dict:
    payload = row.payload
    return {
        "id": row.id,
        "scenario_iteration_id": payload["scenario_iteration_id"],
        "display_order": payload.get("display_order", 0),
        "name": row.name,
        "description": payload.get("description") or "",
        "ai_description": payload.get("ai_description") or "",
        "rule_group": payload.get("rule_group") or "",
        "score_modifier": int(payload.get("score_modifier") or 0),
        "created_at": iso(row.created_at),
        "formula_ast_expression": payload.get("formula_ast_expression"),
    }


def executable_from_formula(formula, score_modifier: int) -> dict:
    screening = isinstance(formula, dict) and (formula.get("name") == "Screen" or _mentions(formula, "Screen"))
    if screening:
        return {"kind": "screening", "score_modifier": score_modifier, "screening": True, "ast": formula}
    return {"kind": "ast", "score_modifier": score_modifier, "ast": formula, "screening": False}


def _mentions(node, name: str) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("name") == name:
        return True
    return any(_mentions(child, name) for child in node.get("children") or []) or any(
        _mentions(child, name) for child in (node.get("named_children") or {}).values()
    )


def commit_iteration(db: Session, scenario: Scenario, iteration: ConfigDocument) -> None:
    rules = list_docs(db, scenario.tenant_id, "scenario_rule", iteration.id)
    compiled = []
    for rule in rules:
        executable = dict(rule.payload.get("executable") or executable_from_formula(rule.payload.get("formula_ast_expression"), int(rule.payload.get("score_modifier") or 0)))
        executable["id"] = rule.id
        executable["name"] = rule.name
        executable["score_modifier"] = int(rule.payload.get("score_modifier") or 0)
        compiled.append(executable)
    body = iteration.payload.get("body") or {}
    scenario.rules = compiled
    scenario.review_threshold = int(body.get("score_review_threshold") or 0)
    scenario.block_threshold = int(body.get("score_block_and_review_threshold") or 0)
    scenario.decline_threshold = int(body.get("score_decline_threshold") or 0)
    scenario.iteration_id = iteration.id
    version = iteration.payload.get("version") or 1
    scenario.version = str(version)
    scenario.active = True
    for previous in list_docs(db, scenario.tenant_id, "scenario_iteration", scenario.id):
        if previous.id == iteration.id:
            continue
        if previous.status == "live":
            previous.status = "archived"
            previous.payload = {**previous.payload, "archived": True}
            touch(previous)
    iteration.status = "live"
    iteration.payload = {**iteration.payload, "version": version, "archived": False}
    touch(iteration)
    meta = get_doc(db, scenario.tenant_id, "scenario_meta", scenario.id)
    if meta:
        meta.payload = {**meta.payload, "live_version_id": iteration.id, "archived": False}
        touch(meta)
    publication = add_doc(
        db,
        scenario.tenant_id,
        "scenario_publication",
        scenario.name,
        {"scenario_id": scenario.id, "scenario_iteration_id": iteration.id, "status": "published"},
        parent_id=scenario.id,
        status="published",
    )
    return publication


def data_model_dto(db: Session, tenant_id: str) -> dict:
    fields = db.scalars(select(DataModelField).where(DataModelField.tenant_id == tenant_id)).all()
    tables: dict[str, dict] = {}
    for field in fields:
        bucket = tables.setdefault(field.object_type, {"fields": {}})
        bucket["fields"][field.name] = _field_dto(tenant_id, field)
    result = {}
    for name, bucket in tables.items():
        meta = get_doc(db, tenant_id, "table_meta", table_id(tenant_id, name))
        result[name] = {
            "id": table_id(tenant_id, name),
            "name": name,
            "description": (meta.payload.get("description") if meta else "") or "",
            "fields": bucket["fields"],
        }
    return {"tables": result}


def _field_dto(tenant_id: str, field: DataModelField) -> dict:
    return {
        "id": field.id,
        "data_type": FIELD_TYPES.get(field.name, "String"),
        "description": "",
        "is_enum": False,
        "name": field.name,
        "nullable": not field.required,
        "table_id": table_id(tenant_id, field.object_type),
        "unicity_constraint": "no_unicity_constraint",
    }


WORKFLOW_ALIASES = {
    "kyc_collection_flow": "kyc_collection_flow",
    "risk-score-improvement-dev": "risk-score-improvement",
}


def resolve_workflow_definition(db: Session, tenant_id: str, key: str) -> ConfigDocument | None:
    names = [key]
    alias = WORKFLOW_ALIASES.get(key)
    if alias and alias not in names:
        names.append(alias)
    for name in names:
        row = db.get(ConfigDocument, name)
        if row and row.tenant_id == tenant_id and row.kind == "workflow_definition":
            return row
        found = db.scalar(
            select(ConfigDocument).where(
                ConfigDocument.tenant_id == tenant_id,
                ConfigDocument.kind == "workflow_definition",
                ConfigDocument.name == name,
            )
        )
        if found:
            return found
    return None


def workflow_definition_dto(row: ConfigDocument) -> dict:
    payload = row.payload
    return {
        "id": row.id,
        "name": row.name,
        "displayName": payload.get("displayName") or row.name,
        "version": payload.get("version") or 1,
        "definitionType": payload.get("definitionType") or "statechart",
        "definition": payload.get("definition") or {},
        "config": payload.get("config") or {},
        "extensions": payload.get("extensions") or {},
        "contextSchema": payload.get("contextSchema") or {},
        "documentsSchema": payload.get("documentsSchema") or [],
        "submitStates": payload.get("submitStates") or [],
        "projectId": row.tenant_id,
        "processKey": payload.get("processKey"),
        "entityType": payload.get("entityType"),
        "synthetic": True,
    }


def validate_formula(formula, expected: str | None) -> dict:
    errors = []
    value = None
    if formula is None:
        errors.append({"error": "RULE_FORMULA_REQUIRED", "message": "formula_ast_expression is required"})
    else:
        try:
            value = eval_ast(formula, {}, {})
            if expected == "bool" and not isinstance(value, bool):
                errors.append({"error": "FORMULA_MUST_RETURN_BOOLEAN", "message": "formula must return a boolean on an empty payload"})
            if expected and expected not in {"bool", "string", "int", "float"}:
                errors.append({"error": "FORMULA_INCORRECT_RETURN_TYPE", "message": "unexpected return type"})
        except AstError as exc:
            if "unsupported" in str(exc):
                errors.append({"error": "FORMULA_INCORRECT_RETURN_TYPE", "message": str(exc)})
            else:
                errors.append({"error": "RULE_FORMULA_REQUIRED", "message": str(exc)})
            value = None
    return {"errors": errors, "evaluation": node_evaluation(value, [])}


def validation_error(code: str, message: str) -> dict:
    return {"error": code, "message": message}


def ensure_studio(db: Session) -> None:
    """Insert the demonstration configuration once. Safe to call on an existing tenant."""
    scenario = db.get(Scenario, SCENARIO_ID)
    if scenario and not get_doc(db, TENANT_ID, "scenario_meta", SCENARIO_ID):
        add_doc(
            db,
            TENANT_ID,
            "scenario_meta",
            scenario.name,
            {
                "description": "Scénario de démonstration. Une correspondance de criblage reste une piste, jamais un refus automatique.",
                "archived": False,
                "deduplicate_batch_objects": False,
                "live_version_id": SCENARIO_ITERATION_ID,
                "created_at": iso(utcnow()),
            },
            doc_id=SCENARIO_ID,
            status="live",
        )
    if scenario and not db.get(ConfigDocument, SCENARIO_ITERATION_ID):
        add_doc(
            db,
            TENANT_ID,
            "scenario_iteration",
            "version-1",
            {
                "scenario_id": SCENARIO_ID,
                "version": 1,
                "archived": False,
                "body": {
                    "score_review_threshold": scenario.review_threshold,
                    "score_block_and_review_threshold": scenario.block_threshold,
                    "score_decline_threshold": scenario.decline_threshold,
                    "rules": [RULE_AMOUNT_ID, RULE_COUNTRY_ID, RULE_SCREEN_ID],
                    "screening_configs": [],
                    "schedule": None,
                    "trigger_condition_ast_expression": None,
                },
            },
            parent_id=SCENARIO_ID,
            status="live",
            doc_id=SCENARIO_ITERATION_ID,
        )
        _seed_rule(
            db,
            RULE_AMOUNT_ID,
            "Montant unitaire élevé",
            1,
            40,
            {">=": ("amount", 10000)},
            {"kind": "threshold", "field": "amount", "op": "gte", "value": 10000, "score_modifier": 40},
        )
        _seed_rule(
            db,
            RULE_COUNTRY_ID,
            "Pays de la liste interne de démonstration",
            2,
            50,
            None,
            {"kind": "country", "field": "country", "values": ["KP", "IR", "SY"], "score_modifier": 50},
            formula={
                "name": "IsInList",
                "children": [
                    {"name": "Payload", "named_children": {"path": {"constant": "country"}}},
                ],
                "named_children": {"list": {"constant": "pays_eleves"}},
            },
        )
        _seed_rule(
            db,
            RULE_SCREEN_ID,
            "Criblage de la contrepartie",
            3,
            0,
            None,
            {"kind": "screening", "score_modifier": 0, "screening": True},
            formula={"name": "Screen", "children": []},
        )
    if not db.get(ConfigDocument, LIST_COUNTRY_ID):
        add_doc(
            db,
            TENANT_ID,
            "custom_list",
            "pays_eleves",
            {"description": "Liste interne de démonstration. Ce n'est pas une liste FATF."},
            doc_id=LIST_COUNTRY_ID,
        )
        for country in ("KP", "IR", "SY"):
            add_doc(db, TENANT_ID, "custom_list_value", country, {"value": country}, parent_id=LIST_COUNTRY_ID)
    if not db.get(ConfigDocument, SCORESET_ID):
        add_doc(
            db,
            TENANT_ID,
            "scoring_ruleset",
            "customers",
            {
                "record_type": "customers",
                "version": 1,
                "status": "live",
                "factors": [
                    {"name": "Pays élevé", "kind": "country", "field": "country", "values": ["KP", "IR", "SY"], "score": 25},
                    {"name": "Correspondance en attente", "kind": "screening_pending", "score": 15},
                ],
                "notice": "Le score explique un niveau. Il ne refuse pas le client.",
            },
            doc_id=SCORESET_ID,
            status="live",
        )
    if not db.get(ConfigDocument, CS_CONFIG_ID):
        add_doc(
            db,
            TENANT_ID,
            "continuous_screening",
            "contreparties",
            {
                "object_type": "transactions",
                "enabled": True,
                "production": False,
                "dataset": "clarity-synthetic-watchlist",
                "threshold": 86,
            },
            doc_id=CS_CONFIG_ID,
        )
    if not db.get(ConfigDocument, WEBHOOK_ID):
        add_doc(
            db,
            TENANT_ID,
            "webhook",
            "decisions",
            {"url": "", "events": ["decision.created"], "enabled": False, "delivery": "not_configured"},
            doc_id=WEBHOOK_ID,
            status="disabled",
        )
    for name, target in (("prioritaire", "case"), ("edd", "case")):
        exists = db.scalar(select(Tag).where(Tag.tenant_id == TENANT_ID, Tag.name == name))
        if not exists:
            db.add(Tag(tenant_id=TENANT_ID, name=name, target=target))
    inbox = db.get(Inbox, "22222222-2222-4222-8222-222222222222")
    if inbox and not get_doc(db, TENANT_ID, "inbox_meta", inbox.id):
        add_doc(db, TENANT_ID, "inbox_meta", inbox.name, {"escalation_inbox_id": None}, doc_id=inbox.id)
    _ensure_workflows(db)
    _ensure_document_schemas(db)
    _ensure_kyc_api_fields(db)


KYC_API_FIELDS = {
    "prospects": [
        "object_id", "updated_at", "full_name", "first_name", "last_name", "birth_date", "nationality", "country",
        "email", "phone", "pep_public_function", "pep_family_associate", "pep_financial_involvement",
        "screening_tags", "risk_score", "risk_level", "kyc_status", "kyc_expires_at", "source_system",
    ],
    "clients": [
        "object_id", "updated_at", "full_name", "first_name", "last_name", "legal_name", "birth_date", "nationality",
        "customer_reference", "customer_type", "country_of_residence", "registered_country", "customer_status",
        "customer_since", "created_at",
    ],
    "accounts": [
        "object_id", "updated_at", "currency", "customer_id", "iban", "status", "account_reference", "account_type",
        "opened_at", "account_status", "product_type", "channel", "country_code", "source_system", "created_at",
    ],
    "transactions": [
        "object_id", "updated_at", "amount", "currency", "direction", "country", "customer_id", "counterparty_name",
        "description", "client_id", "transaction_reference", "source_system", "transaction_type", "transaction_status",
        "booked_at", "occurred_at", "amount_base_currency", "base_currency", "channel", "country_origin",
        "country_destination", "payment_reference", "is_cash", "debit_account_id", "credit_account_id",
        "originator_customer_id", "beneficiary_customer_id",
    ],
    "counterparties": [
        "object_id", "updated_at", "name", "counterparty_type", "normalized_name", "country_code",
        "external_identifier", "source_system", "first_seen_at", "last_seen_at",
    ],
    "customer_relationships": [
        "object_id", "updated_at", "source_customer_id", "target_customer_id", "relationship_type",
        "source", "verification_status", "valid_from_relation",
    ],
}


def _ensure_kyc_api_fields(db: Session) -> None:
    """Accept the object fields kyc-api already sends to Marble."""
    for object_type, fields in KYC_API_FIELDS.items():
        present = {
            row.name
            for row in db.scalars(
                select(DataModelField).where(DataModelField.tenant_id == TENANT_ID, DataModelField.object_type == object_type)
            ).all()
        }
        for field in fields:
            if field in present:
                continue
            db.add(DataModelField(tenant_id=TENANT_ID, object_type=object_type, name=field, required=field in {"object_id", "updated_at"}))


def _seed_rule(db: Session, rule_id: str, name: str, order: int, score: int, comparison, executable: dict, formula=None) -> None:
    if formula is None and comparison:
        operator, (field, value) = next(iter(comparison.items()))
        formula = {
            "name": operator,
            "children": [
                {"name": "Payload", "named_children": {"path": {"constant": field}}},
                {"constant": value},
            ],
        }
    add_doc(
        db,
        TENANT_ID,
        "scenario_rule",
        name,
        {
            "scenario_iteration_id": SCENARIO_ITERATION_ID,
            "display_order": order,
            "description": name,
            "ai_description": "",
            "rule_group": "demo",
            "score_modifier": score,
            "formula_ast_expression": formula,
            "executable": executable,
        },
        parent_id=SCENARIO_ITERATION_ID,
        doc_id=rule_id,
    )


KYC_DOCUMENTS = [
    {"id": "doc-passport", "category": "identity", "type": "passport", "issuer": {"country": "FR"}, "version": 1, "propertiesSchema": {"type": "object", "properties": {"expiryDate": {"type": "string", "format": "date"}}}, "required": True},
    {"id": "doc-id-card", "category": "identity", "type": "id_card", "issuer": {"country": "FR"}, "version": 1, "propertiesSchema": {"type": "object", "properties": {}}, "required": False},
    {"id": "doc-address", "category": "proof_of_address", "type": "proof_of_address", "issuer": {"country": "FR"}, "version": 1, "propertiesSchema": {"type": "object", "properties": {}}, "required": True},
]
KYB_DOCUMENTS = [
    {"id": "doc-registration", "category": "proof_of_registration", "type": "certificate_of_registration", "issuer": {"country": "ZZ"}, "version": 1, "propertiesSchema": {"type": "object", "properties": {}}, "required": True},
    {"id": "doc-company-address", "category": "proof_of_address", "type": "general_document", "issuer": {"country": "ZZ"}, "version": 1, "propertiesSchema": {"type": "object", "properties": {}}, "required": True},
]


def _ensure_document_schemas(db: Session) -> None:
    mapping = {
        WF_KYC_ID: KYC_DOCUMENTS,
        WF_KYC_DEMO_ID: KYC_DOCUMENTS,
        WF_KYB_ID: KYB_DOCUMENTS,
    }
    for doc_id, schema in mapping.items():
        row = db.get(ConfigDocument, doc_id)
        if not row or row.payload.get("documentsSchema"):
            continue
        row.payload = {**row.payload, "documentsSchema": schema}
        touch(row)


def _ensure_workflows(db: Session) -> None:
    specs = [
        (WF_KYC_ID, "kyc", "Entrée en relation personne physique", "clarityKycIndividual", "individual"),
        (WF_KYC_DEMO_ID, "kyc-demo", "Démonstration KYC", "clarityKycIndividual", "individual"),
        (WF_KYB_ID, "kyb", "Entrée en relation personne morale", "clarityKybBusiness", "business"),
        (WF_ALERT_ID, "alert-investigation", "Investigation d'alerte", "clarityAlertInvestigation", "any"),
        (str(uuid.uuid5(uuid.UUID(TENANT_ID), "workflow:kyc_collection_flow")), "kyc_collection_flow", "Collecte KYC", "clarityKycIndividual", "individual"),
        (str(uuid.uuid5(uuid.UUID(TENANT_ID), "workflow:risk-score-improvement")), "risk-score-improvement", "Revue du score de risque", "risk-score-improvement", "individual"),
    ]
    for doc_id, name, display, process, entity in specs:
        named = db.scalar(
            select(ConfigDocument).where(
                ConfigDocument.tenant_id == TENANT_ID,
                ConfigDocument.kind == "workflow_definition",
                ConfigDocument.name == name,
            )
        )
        if named:
            payload = dict(named.payload)
            changed = False
            if payload.get("processKey") != process and name in {"kyc_collection_flow", "risk-score-improvement"}:
                payload = {**payload, "processKey": process, "entityType": entity}
                changed = True
            if chart_needs_repair(payload.get("definition"), process):
                payload = {**payload, "definition": definition_chart(process)}
                changed = True
            if name == "risk-score-improvement" and payload.get("displayName") in {None, "", "risk-score-improvement"}:
                payload = {**payload, "displayName": display}
                changed = True
            if changed:
                from sqlalchemy.orm.attributes import flag_modified

                named.payload = payload
                flag_modified(named, "payload")
                touch(named)
            continue
        if db.get(ConfigDocument, doc_id):
            continue
        add_doc(
            db,
            TENANT_ID,
            "workflow_definition",
            name,
            {
                "displayName": display,
                "version": 1,
                "definitionType": "bpmn",
                "processKey": process,
                "entityType": entity,
                "definition": definition_chart(process),
                "config": {"engine": "flowable-or-fallback", "synthetic": True},
                "contextSchema": {"type": "object"},
                "submitStates": ["approved", "rejected", "closed"],
            },
            doc_id=doc_id,
            status="published",
        )
    if not db.get(ConfigDocument, UI_KYC_ID):
        add_doc(
            db,
            TENANT_ID,
            "ui_definition",
            "kyc-collection",
            {
                "workflowDefinitionId": WF_KYC_ID,
                "uiSchema": {
                    "title": "Entrée en relation",
                    "elements": ["legalName", "dateOfBirth", "nationality", "address", "identityDocument"],
                },
                "locales": {"fr": {"title": "Entrée en relation"}},
            },
            parent_id=WF_KYC_ID,
            doc_id=UI_KYC_ID,
        )
    if not db.scalar(select(ConfigDocument).where(ConfigDocument.tenant_id == TENANT_ID, ConfigDocument.kind == "alert_definition")):
        add_doc(
            db,
            TENANT_ID,
            "alert_definition",
            "pattern-to-investigate",
            {
                "description": "Une alerte signale un motif à investiguer. Elle ne conclut pas à un blanchiment.",
                "rules": ["threshold", "velocity", "cross_border", "round_amount"],
            },
        )
    if not db.scalar(select(ConfigDocument).where(ConfigDocument.tenant_id == TENANT_ID, ConfigDocument.kind == "filter")):
        add_doc(
            db,
            TENANT_ID,
            "filter",
            "dossiers-ouverts",
            {"entity": "cases", "query": {"status": "pending"}},
        )


def studio_view(db: Session, tenant_id: str) -> dict:
    from app.models import MonitoringRule

    scenarios = []
    for scenario in db.scalars(select(Scenario).where(Scenario.tenant_id == tenant_id)).all():
        item = scenario_dto(db, scenario)
        item["thresholds"] = {
            "review": scenario.review_threshold,
            "block_and_review": scenario.block_threshold,
            "decline": scenario.decline_threshold,
        }
        item["rules"] = scenario.rules
        scenarios.append(item)
    definitions = [workflow_definition_dto(row) for row in list_docs(db, tenant_id, "workflow_definition")]
    monitoring = [
        {"id": row.id, "name": row.name, "scenario": row.scenario, "parameters": row.parameters, "enabled": row.enabled, "version": row.version}
        for row in db.scalars(select(MonitoringRule).where(MonitoringRule.tenant_id == tenant_id)).all()
    ]
    return {
        "notice": "Le paramétrage ci-dessous est celui qui est exécuté. Les listes et contrôles marqués synthétiques ne sont pas des résultats de production.",
        "marble": {
            "scenarios": scenarios,
            "data_model": data_model_dto(db, tenant_id),
            "custom_lists": [{"id": row.id, "name": row.name, "description": row.payload.get("description")} for row in list_docs(db, tenant_id, "custom_list")],
            "scoring_rulesets": [row.payload | {"id": row.id, "status": row.status} for row in list_docs(db, tenant_id, "scoring_ruleset")],
            "continuous_screening": [row.payload | {"id": row.id} for row in list_docs(db, tenant_id, "continuous_screening")],
            "webhooks": [row.payload | {"id": row.id, "name": row.name, "status": row.status} for row in list_docs(db, tenant_id, "webhook")],
        },
        "ballerine": {
            "workflow_definitions": definitions,
            "ui_definitions": [row.payload | {"id": row.id, "name": row.name} for row in list_docs(db, tenant_id, "ui_definition")],
            "alert_definitions": [row.payload | {"id": row.id, "name": row.name} for row in list_docs(db, tenant_id, "alert_definition")],
            "filters": [row.payload | {"id": row.id, "name": row.name} for row in list_docs(db, tenant_id, "filter")],
        },
        "monitoring_rules": monitoring,
    }
