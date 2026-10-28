"""Marble console configuration and execution surface.

Paths follow marblecore-api.yaml. Responses use the documented field names for the
operations implemented here. AI generation, OpenSanctions and organization administration
are not served.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import (
    _field_dto,
    add_doc,
    commit_iteration,
    data_model_dto,
    get_doc,
    iteration_dto,
    list_docs,
    rule_dto,
    scenario_dto,
    table_id,
    touch,
    validate_formula,
    validation_error,
)
from app.deps import db_session, marble_principal
from app.models import (
    BatchExecution,
    ConfigDocument,
    DataModelField,
    Decision,
    Inbox,
    IngestedObject,
    Scenario,
    Tag,
    Tenant,
    User,
    new_id,
)
from app.security import Principal, hash_token
from app.services import execute_scenario, marble_error, persist_screening, require_uuid
from app.timeutil import iso, parse_dt, utcnow

router = APIRouter(tags=["marble-console"])


def _tenant(db: Session, principal: Principal) -> Tenant:
    tenant = db.get(Tenant, principal.tenant_id)
    if not tenant:
        raise marble_error(404, "not_found", "tenant not found")
    return tenant


def _scenario(db: Session, principal: Principal, scenario_id: str) -> Scenario:
    require_uuid(scenario_id, "scenarioId")
    scenario = db.get(Scenario, scenario_id)
    if not scenario or scenario.tenant_id != principal.tenant_id:
        raise marble_error(404, "not_found", "scenario not found")
    return scenario


def _iteration(db: Session, principal: Principal, iteration_id: str) -> ConfigDocument:
    require_uuid(iteration_id, "scenarioIterationId")
    row = get_doc(db, principal.tenant_id, "scenario_iteration", iteration_id)
    if not row:
        raise marble_error(404, "not_found", "scenario iteration not found")
    return row


@router.get("/scenarios")
def list_scenarios(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Scenario).where(Scenario.tenant_id == principal.tenant_id)).all()
    return [scenario_dto(db, row) for row in rows]


@router.post("/scenarios")
def create_scenario(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    for field in ("name", "description", "trigger_object_type"):
        if not isinstance(body.get(field), str) or not body[field]:
            raise marble_error(422, "invalid_payload", f"{field} is required")
    known = {
        row.object_type
        for row in db.scalars(select(DataModelField).where(DataModelField.tenant_id == principal.tenant_id)).all()
    }
    if body["trigger_object_type"] not in known:
        raise marble_error(422, "invalid_payload", "trigger_object_type is not in the data model")
    scenario = Scenario(
        tenant_id=principal.tenant_id,
        name=body["name"],
        iteration_id=new_id(),
        version="0",
        trigger_object_type=body["trigger_object_type"],
        review_threshold=0,
        block_threshold=0,
        decline_threshold=0,
        active=True,
        rules=[],
    )
    db.add(scenario)
    db.flush()
    add_doc(
        db,
        principal.tenant_id,
        "scenario_meta",
        scenario.name,
        {
            "description": body["description"],
            "archived": False,
            "deduplicate_batch_objects": False,
            "created_at": iso(utcnow()),
        },
        doc_id=scenario.id,
    )
    add_doc(
        db,
        principal.tenant_id,
        "scenario_iteration",
        "draft",
        {
            "scenario_id": scenario.id,
            "version": None,
            "archived": False,
            "body": {
                "rules": [],
                "screening_configs": [],
                "score_review_threshold": None,
                "score_block_and_review_threshold": None,
                "score_decline_threshold": None,
                "trigger_condition_ast_expression": None,
                "schedule": None,
            },
        },
        parent_id=scenario.id,
        status="draft",
        doc_id=scenario.iteration_id,
    )
    return scenario_dto(db, scenario)


@router.get("/scenarios/catalog")
def rule_catalog(principal: Principal = Depends(marble_principal)):
    return {
        "rules": [
            {"name": "Payload", "description": {"fr": "Lit un champ du déclencheur.", "en": "Reads a trigger field."}},
            {"name": "IsInList", "description": {"fr": "Teste une liste personnalisée.", "en": "Tests a custom list."}},
            {"name": "Screen", "description": {"fr": "Lance un criblage candidat, sans confirmation.", "en": "Runs a candidate screening."}},
        ]
    }


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    return scenario_dto(db, _scenario(db, principal, scenario_id))


@router.patch("/scenarios/{scenario_id}")
def update_scenario(scenario_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    scenario = _scenario(db, principal, scenario_id)
    meta = get_doc(db, principal.tenant_id, "scenario_meta", scenario.id)
    payload = dict(meta.payload) if meta else {}
    if "name" in body:
        scenario.name = str(body["name"])
    for key in ("description", "archived", "deduplicate_batch_objects"):
        if key in body:
            payload[key] = body[key]
    if isinstance(body.get("i18n"), dict):
        clean = {}
        for field in ("name", "description"):
            raw = body["i18n"].get(field)
            if isinstance(raw, dict):
                clean[field] = {lang: str(raw.get(lang) or "") for lang in ("fr", "en", "ar")}
        payload["i18n"] = clean
        chosen = next((clean.get("name", {}).get(lang) for lang in ("fr", "en", "ar") if clean.get("name", {}).get(lang)), "")
        if chosen:
            scenario.name = chosen
        described = next((clean.get("description", {}).get(lang) for lang in ("fr", "en", "ar") if clean.get("description", {}).get(lang)), "")
        if described:
            payload["description"] = described
    if body.get("archived") is True:
        scenario.active = False
    if meta:
        meta.payload = payload
        touch(meta)
    elif payload:
        add_doc(db, principal.tenant_id, "scenario_meta", scenario.name, payload, doc_id=scenario.id)
    return scenario_dto(db, scenario)


@router.post("/scenarios/{scenario_id}/copy")
def copy_scenario(scenario_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    source = _scenario(db, principal, scenario_id)
    created = create_scenario(
        {
            "name": body.get("name") or f"{source.name} copie",
            "description": scenario_dto(db, source)["description"],
            "trigger_object_type": source.trigger_object_type,
        },
        principal,
        db,
    )
    return created


@router.post("/scenarios/{scenario_id}/validate-ast")
def validate_ast(scenario_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    _scenario(db, principal, scenario_id)
    checked = validate_formula(body.get("node"), body.get("expected_return_type"))
    return {"ast_validation": checked}


@router.get("/scenarios/{scenario_id}/rules/latest")
def latest_rules(scenario_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    scenario = _scenario(db, principal, scenario_id)
    live = get_doc(db, principal.tenant_id, "scenario_iteration", scenario.iteration_id)
    rules = list_docs(db, principal.tenant_id, "scenario_rule", live.id) if live else []
    return [{"type": "rule", "stable_id": row.id, "name": row.name, "latest_version": scenario.version} for row in rules]


@router.get("/scenario-iterations")
def list_iterations(scenario_id: str | None = None, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    rows = list_docs(db, principal.tenant_id, "scenario_iteration")
    if scenario_id:
        rows = [row for row in rows if row.payload.get("scenario_id") == scenario_id]
    return [iteration_dto(row, True) for row in rows]


@router.post("/scenario-iterations")
def create_iteration(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    scenario = _scenario(db, principal, str(body.get("scenario_id") or ""))
    supplied = body.get("body") if isinstance(body.get("body"), dict) else {}
    row = add_doc(
        db,
        principal.tenant_id,
        "scenario_iteration",
        "draft",
        {
            "scenario_id": scenario.id,
            "version": None,
            "archived": False,
            "body": {
                "rules": [],
                "screening_configs": supplied.get("screening_configs") or [],
                "score_review_threshold": supplied.get("score_review_threshold"),
                "score_block_and_review_threshold": supplied.get("score_block_and_review_threshold"),
                "score_decline_threshold": supplied.get("score_decline_threshold"),
                "trigger_condition_ast_expression": supplied.get("trigger_condition_ast_expression"),
                "schedule": supplied.get("schedule"),
            },
        },
        parent_id=scenario.id,
        status="draft",
    )
    return iteration_dto(row, True)


@router.get("/scenario-iterations/{iteration_id}")
def get_iteration(iteration_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    return iteration_dto(_iteration(db, principal, iteration_id), True)


@router.patch("/scenario-iterations/{iteration_id}")
def update_iteration(iteration_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    row = _iteration(db, principal, iteration_id)
    if row.status == "live":
        raise marble_error(422, "invalid_payload", "a live iteration cannot be edited; create a new draft")
    supplied = body.get("body") if isinstance(body.get("body"), dict) else {}
    current = dict(row.payload.get("body") or {})
    current.update({key: value for key, value in supplied.items() if key != "rules"})
    row.payload = {**row.payload, "body": current}
    touch(row)
    return iteration_dto(row, True)


@router.post("/scenario-iterations/{iteration_id}/validate")
def validate_iteration(iteration_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    row = _iteration(db, principal, iteration_id)
    scenario = _scenario(db, principal, row.payload["scenario_id"])
    errors = []
    known = {field.object_type for field in db.scalars(select(DataModelField).where(DataModelField.tenant_id == principal.tenant_id)).all()}
    if scenario.trigger_object_type not in known:
        errors.append(validation_error("TRIGGER_OBJECT_NOT_FOUND", "trigger object is not in the data model"))
    body = row.payload.get("body") or {}
    thresholds = [body.get("score_review_threshold"), body.get("score_block_and_review_threshold"), body.get("score_decline_threshold")]
    if any(item is None for item in thresholds):
        errors.append(validation_error("SCORE_THRESHOLD_MISSING", "review, block and decline thresholds are required"))
    elif not (thresholds[0] <= thresholds[1] <= thresholds[2]):
        errors.append(validation_error("SCORE_THRESHOLDS_MISMATCH", "thresholds must be review <= block_and_review <= decline"))
    rules = {}
    for rule in list_docs(db, principal.tenant_id, "scenario_rule", row.id):
        checked = validate_formula(rule.payload.get("formula_ast_expression"), "bool")
        rules[rule.id] = {"errors": checked["errors"], "rule_evaluation": checked["evaluation"]}
    return {
        "trigger": {"errors": errors, "trigger_evaluation": validate_formula(body.get("trigger_condition_ast_expression"), None)["evaluation"] if body.get("trigger_condition_ast_expression") else {"return_value": {"is_omitted": True}, "errors": []}},
        "rules": {"errors": [], "rules": rules},
        "screening_configs": [],
        "decision": {"errors": errors},
    }


@router.post("/scenario-iterations/{iteration_id}/commit")
def commit(iteration_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    row = _iteration(db, principal, iteration_id)
    report = validate_iteration(iteration_id, principal, db)
    if report["decision"]["errors"] or any(item["errors"] for item in report["rules"]["rules"].values()):
        raise marble_error(422, "invalid_payload", "iteration is not valid")
    scenario = _scenario(db, principal, row.payload["scenario_id"])
    if row.payload.get("version") is None:
        previous = [item for item in list_docs(db, principal.tenant_id, "scenario_iteration", scenario.id) if item.payload.get("version")]
        row.payload = {**row.payload, "version": (max(int(item.payload["version"]) for item in previous) + 1) if previous else 1}
    publication = commit_iteration(db, scenario, row)
    return {"scenario_iteration_id": row.id, "publication_id": publication.id, "status": "published"}


@router.post("/scenario-iterations/{iteration_id}/schedule-execution")
def schedule_execution(iteration_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    row = _iteration(db, principal, iteration_id)
    if row.status != "live":
        raise marble_error(422, "invalid_payload", "only a published iteration can be executed")
    scenario = _scenario(db, principal, row.payload["scenario_id"])
    tenant = _tenant(db, principal)
    objects = db.scalars(
        select(IngestedObject).where(IngestedObject.tenant_id == tenant.id, IngestedObject.object_type == scenario.trigger_object_type)
    ).all()
    batch = BatchExecution(tenant_id=tenant.id, scenario_id=scenario.id, manual=False, status="success", decisions_created=0)
    db.add(batch)
    db.flush()
    for item in objects:
        execute_scenario(db, tenant, scenario, item.payload, trigger_object_type=scenario.trigger_object_type, batch_execution_id=batch.id)
        batch.decisions_created += 1
    batch.finished_at = utcnow()
    return {"id": batch.id, "status": batch.status, "decisions_created": batch.decisions_created}


@router.get("/scenario-iteration-rules")
def list_rules(scenario_iteration_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    _iteration(db, principal, scenario_iteration_id)
    return [rule_dto(row) for row in list_docs(db, principal.tenant_id, "scenario_rule", scenario_iteration_id)]


@router.post("/scenario-iteration-rules")
def create_rule(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    required = ("scenario_iteration_id", "display_order", "name", "description", "rule_group", "formula_ast_expression", "score_modifier")
    missing = [field for field in required if field not in body]
    if missing:
        raise marble_error(422, "invalid_payload", "missing " + ", ".join(missing))
    iteration = _iteration(db, principal, str(body["scenario_iteration_id"]))
    if iteration.status == "live":
        raise marble_error(422, "invalid_payload", "rules of a live iteration cannot be changed")
    checked = validate_formula(body["formula_ast_expression"], "bool")
    if checked["errors"]:
        raise marble_error(422, "invalid_payload", checked["errors"][0]["message"])
    row = add_doc(
        db,
        principal.tenant_id,
        "scenario_rule",
        str(body["name"]),
        {
            "scenario_iteration_id": iteration.id,
            "display_order": body["display_order"],
            "description": body["description"],
            "ai_description": "",
            "rule_group": body["rule_group"],
            "score_modifier": body["score_modifier"],
            "formula_ast_expression": body["formula_ast_expression"],
            "executable": {
                **(
                    {"kind": "screening", "screening": True}
                    if isinstance(body["formula_ast_expression"], dict) and body["formula_ast_expression"].get("name") == "Screen"
                    else {"kind": "ast", "ast": body["formula_ast_expression"], "screening": False}
                ),
                "score_modifier": body["score_modifier"],
            },
        },
        parent_id=iteration.id,
    )
    rules = list(iteration.payload.get("body", {}).get("rules") or [])
    rules.append(row.id)
    body_payload = dict(iteration.payload.get("body") or {})
    body_payload["rules"] = rules
    iteration.payload = {**iteration.payload, "body": body_payload}
    touch(iteration)
    return rule_dto(row)


@router.patch("/scenario-iteration-rules/{rule_id}")
def update_rule(rule_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    require_uuid(rule_id, "ruleId")
    row = get_doc(db, principal.tenant_id, "scenario_rule", rule_id)
    if not row:
        raise marble_error(404, "not_found", "rule not found")
    iteration = _iteration(db, principal, row.parent_id or "")
    if iteration.status == "live":
        raise marble_error(422, "invalid_payload", "rules of a live iteration cannot be changed")
    payload = dict(row.payload)
    for key in ("display_order", "description", "rule_group", "score_modifier", "formula_ast_expression"):
        if key in body:
            payload[key] = body[key]
    if "name" in body:
        row.name = str(body["name"])
    if "formula_ast_expression" in body:
        payload["executable"] = {
            "kind": "ast",
            "ast": body["formula_ast_expression"],
            "score_modifier": payload.get("score_modifier") or 0,
            "screening": isinstance(body["formula_ast_expression"], dict) and body["formula_ast_expression"].get("name") == "Screen",
        }
    row.payload = payload
    touch(row)
    return rule_dto(row)


@router.delete("/scenario-iteration-rules/{rule_id}")
def delete_rule(rule_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    row = get_doc(db, principal.tenant_id, "scenario_rule", rule_id)
    if not row:
        raise marble_error(404, "not_found", "rule not found")
    iteration = _iteration(db, principal, row.parent_id or "")
    if iteration.status == "live":
        raise marble_error(422, "invalid_payload", "rules of a live iteration cannot be changed")
    db.delete(row)
    return {"id": rule_id}


@router.get("/data-model")
def get_data_model(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    return data_model_dto(db, principal.tenant_id)


@router.get("/data-model/tables")
def list_tables(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    return list(data_model_dto(db, principal.tenant_id)["tables"].values())


@router.post("/data-model/tables")
def create_table(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise marble_error(422, "invalid_payload", "name is required")
    if "semantic_type" not in body:
        raise marble_error(422, "invalid_payload", "semantic_type is required")
    exists = db.scalar(select(DataModelField).where(DataModelField.tenant_id == principal.tenant_id, DataModelField.object_type == name))
    if exists:
        raise marble_error(422, "invalid_payload", "table already exists")
    for field_name, required in (("object_id", True), ("updated_at", True)):
        db.add(DataModelField(tenant_id=principal.tenant_id, object_type=name, name=field_name, required=required))
    for field in body.get("fields") or []:
        if not isinstance(field, dict) or not field.get("name"):
            raise marble_error(422, "invalid_payload", "field name is required")
        db.add(DataModelField(tenant_id=principal.tenant_id, object_type=name, name=str(field["name"]), required=bool(field.get("required", False))))
    db.flush()
    add_doc(
        db,
        principal.tenant_id,
        "table_meta",
        name,
        {"description": body.get("description") or "", "semantic_type": body["semantic_type"]},
        doc_id=table_id(principal.tenant_id, name),
    )
    return data_model_dto(db, principal.tenant_id)["tables"][name]


@router.post("/data-model/tables/{table_id}/fields")
def add_field(table_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    name = _table_name(db, principal.tenant_id, table_id)
    if not body.get("name"):
        raise marble_error(422, "invalid_payload", "name is required")
    field_name = str(body["name"]).strip()
    taken = db.scalar(
        select(DataModelField).where(
            DataModelField.tenant_id == principal.tenant_id,
            DataModelField.object_type == name,
            DataModelField.name == field_name,
        )
    )
    if taken:
        raise marble_error(422, "invalid_payload", "Cette colonne existe déjà.")
    row = DataModelField(
        tenant_id=principal.tenant_id,
        object_type=name,
        name=field_name,
        required=not bool(body.get("nullable", True)),
        data_type=str(body.get("data_type") or body.get("type") or "String"),
        description=str(body.get("description") or ""),
    )
    db.add(row)
    db.flush()
    return _field_dto(principal.tenant_id, row)


def _table_name(db: Session, tenant_id: str, raw_id: str) -> str:
    tables = data_model_dto(db, tenant_id)["tables"]
    for table in tables.values():
        if table["id"] == raw_id:
            return table["name"]
    raise marble_error(404, "not_found", "table not found")


@router.get("/custom-lists")
def list_custom_lists(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    return [{"id": row.id, "name": row.name, "description": row.payload.get("description") or ""} for row in list_docs(db, principal.tenant_id, "custom_list")]


@router.post("/custom-lists")
def create_custom_list(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    if not body.get("name"):
        raise marble_error(422, "invalid_payload", "name is required")
    row = add_doc(db, principal.tenant_id, "custom_list", str(body["name"]), {"description": body.get("description") or ""})
    return {"id": row.id, "name": row.name, "description": row.payload["description"]}


@router.post("/custom-lists/{custom_list_id}/values")
def add_list_value(custom_list_id: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    parent = get_doc(db, principal.tenant_id, "custom_list", custom_list_id)
    if not parent:
        raise marble_error(404, "not_found", "custom list not found")
    if "value" not in body:
        raise marble_error(422, "invalid_payload", "value is required")
    row = add_doc(db, principal.tenant_id, "custom_list_value", str(body["value"]), {"value": body["value"]}, parent_id=parent.id)
    return {"id": row.id, "value": body["value"]}


@router.get("/scoring/rulesets")
def list_rulesets(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    return [row.payload | {"id": row.id, "status": row.status} for row in list_docs(db, principal.tenant_id, "scoring_ruleset")]


@router.post("/scoring/rulesets/{record_type}/prepare")
def prepare_ruleset(record_type: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    row = add_doc(
        db,
        principal.tenant_id,
        "scoring_ruleset",
        record_type,
        {"record_type": record_type, "version": None, "status": "draft", "factors": body.get("factors") or [], "notice": "Un score n'est pas une décision de refus."},
        status="draft",
    )
    return row.payload | {"id": row.id}


@router.post("/scoring/rulesets/{record_type}/commit")
def commit_ruleset(record_type: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    drafts = [row for row in list_docs(db, principal.tenant_id, "scoring_ruleset") if row.payload.get("record_type") == record_type and row.status == "draft"]
    if not drafts and not body.get("factors"):
        raise marble_error(422, "invalid_payload", "no draft ruleset to commit")
    target = drafts[-1] if drafts else add_doc(db, principal.tenant_id, "scoring_ruleset", record_type, {"record_type": record_type, "factors": body.get("factors") or []}, status="draft")
    for row in list_docs(db, principal.tenant_id, "scoring_ruleset"):
        if row.payload.get("record_type") == record_type and row.status == "live" and row.id != target.id:
            row.status = "archived"
    version = int(target.payload.get("version") or 0) + 1
    target.payload = {**target.payload, "version": version, "status": "live", "factors": body.get("factors") or target.payload.get("factors") or []}
    target.status = "live"
    touch(target)
    return target.payload | {"id": target.id, "status": "live"}


@router.post("/scoring/rulesets/{record_type}/dry-run")
def dry_run_ruleset(record_type: str, body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    live = [row for row in list_docs(db, principal.tenant_id, "scoring_ruleset") if row.payload.get("record_type") == record_type and row.status == "live"]
    if not live:
        raise marble_error(404, "not_found", "no live ruleset")
    payload = body.get("record") if isinstance(body.get("record"), dict) else body
    score = 0
    factors = []
    for factor in live[-1].payload.get("factors") or []:
        hit = False
        if factor.get("kind") == "country":
            hit = str(payload.get(factor.get("field") or "country") or "") in set(factor.get("values") or [])
        if hit:
            score += int(factor.get("score") or 0)
            factors.append({"name": factor.get("name"), "score": factor.get("score"), "hit": True})
    return {"record_type": record_type, "score": score, "factors": factors, "rejects_customer": False, "production": False}


@router.get("/continuous-screenings/configs")
def list_screening_configs(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    return [row.payload | {"id": row.id, "stable_id": row.id} for row in list_docs(db, principal.tenant_id, "continuous_screening")]


@router.post("/continuous-screenings/configs")
def create_screening_config(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    row = add_doc(
        db,
        principal.tenant_id,
        "continuous_screening",
        body.get("name") or "criblage",
        {"object_type": body.get("object_type") or "customers", "enabled": True, "production": False, "dataset": "clarity-synthetic-watchlist", "threshold": body.get("threshold") or 86},
    )
    return row.payload | {"id": row.id}


@router.post("/continuous-screenings")
def run_continuous_screening(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    name = body.get("name") or body.get("counterparty_name")
    if not name:
        raise marble_error(422, "invalid_payload", "name is required")
    tenant = _tenant(db, principal)
    screening = persist_screening(db, tenant, {"Person": {"name": name}}, counterparty=str(body.get("object_id") or name))
    return {"id": screening.id, "status": screening.status, "production": False, "dataset": "clarity-synthetic-watchlist", "match_count": screening.match_count}


@router.get("/webhooks")
def list_webhooks(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    return [row.payload | {"id": row.id, "name": row.name, "status": row.status} for row in list_docs(db, principal.tenant_id, "webhook")]


@router.post("/webhooks")
def create_webhook(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    if not body.get("url"):
        raise marble_error(422, "invalid_payload", "url is required")
    row = add_doc(db, principal.tenant_id, "webhook", body.get("name") or "webhook", {"url": body["url"], "events": body.get("events") or ["decision.created"], "enabled": True, "delivery": "configured"}, status="active")
    return row.payload | {"id": row.id}


@router.get("/inboxes")
def list_inboxes(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Inbox).where(Inbox.tenant_id == principal.tenant_id)).all()
    return [{"id": row.id, "name": row.name} for row in rows]


@router.post("/inboxes")
def create_inbox(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    if not body.get("name"):
        raise marble_error(422, "invalid_payload", "name is required")
    row = Inbox(tenant_id=principal.tenant_id, name=str(body["name"]))
    db.add(row)
    db.flush()
    return {"id": row.id, "name": row.name}


@router.get("/tags")
def list_console_tags(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Tag).where(Tag.tenant_id == principal.tenant_id)).all()
    return [{"id": row.id, "name": row.name, "target": row.target} for row in rows]


@router.post("/tags")
def create_tag(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    if body.get("target") not in {"case", "object"} or not body.get("name"):
        raise marble_error(422, "invalid_payload", "name and target case|object are required")
    row = Tag(tenant_id=principal.tenant_id, name=str(body["name"]), target=body["target"])
    db.add(row)
    db.flush()
    return {"id": row.id, "name": row.name, "target": row.target}


@router.get("/users")
def list_users(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(User).where(User.tenant_id == principal.tenant_id)).all()
    return [{"id": row.id, "email": row.email, "name": row.full_name, "role": row.role, "active": row.active} for row in rows]


@router.get("/apikeys")
def list_api_keys(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    from app.models import ApiCredential

    rows = db.scalars(select(ApiCredential).where(ApiCredential.tenant_id == principal.tenant_id)).all()
    return [{"id": row.id, "name": row.name, "surface": row.surface, "created_at": iso(row.created_at)} for row in rows]


@router.post("/apikeys")
def create_api_key(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    from app.models import ApiCredential

    surface = body.get("surface")
    if surface not in {"marble", "ballerine"} or not body.get("name"):
        raise marble_error(422, "invalid_payload", "name and surface marble|ballerine are required")
    token = "clry_" + uuid.uuid4().hex
    row = ApiCredential(tenant_id=principal.tenant_id, surface=surface, name=str(body["name"]), token_hash=hash_token(token))
    db.add(row)
    db.flush()
    return {"id": row.id, "name": row.name, "surface": surface, "token": token}


@router.get("/decisions")
def list_decisions(limit: int = 50, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Decision).where(Decision.tenant_id == principal.tenant_id).order_by(Decision.created_at.desc()).limit(min(limit, 100))).all()
    items = []
    for row in rows:
        scenario = db.get(Scenario, row.scenario_id)
        items.append(
            {
                "id": row.id,
                "created_at": iso(row.created_at),
                "outcome": row.outcome,
                "pivot_values": [str(row.trigger_object.get("object_id") or "")],
                "review_status": row.review_status,
                "scenario": {
                    "id": row.scenario_id,
                    "name": scenario.name if scenario else "",
                    "scenario_iteration_id": row.iteration_id,
                    "version": int(row.scenario_version) if str(row.scenario_version).isdigit() else 0,
                },
                "score": row.score,
                "trigger_object": row.trigger_object,
                "trigger_object_type": row.trigger_object_type,
            }
        )
    return {"items": items, "has_next_page": False}


@router.post("/decisions")
def create_console_decision(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    if "scenario_id" not in body or "trigger_object" not in body:
        raise marble_error(400, "invalid_payload", "scenario_id and trigger_object are required")
    scenario = _scenario(db, principal, str(body["scenario_id"]))
    if not isinstance(body["trigger_object"], dict):
        raise marble_error(400, "invalid_payload", "trigger_object must be an object")
    tenant = _tenant(db, principal)
    decision = execute_scenario(
        db,
        tenant,
        scenario,
        body["trigger_object"],
        trigger_object_type=str(body.get("object_type") or scenario.trigger_object_type),
    )
    return {"id": decision.id, "outcome": decision.outcome, "score": decision.score, "review_status": decision.review_status}


@router.get("/scheduled-executions")
def list_scheduled(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(BatchExecution).where(BatchExecution.tenant_id == principal.tenant_id, BatchExecution.manual.is_(False))).all()
    return [{"id": row.id, "scenario_id": row.scenario_id, "status": row.status, "decisions_created": row.decisions_created, "created_at": iso(row.created_at)} for row in rows]


@router.get("/scenario-testrun")
def list_test_runs(scenario_id: str, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    _scenario(db, principal, scenario_id)
    rows = [row for row in list_docs(db, principal.tenant_id, "scenario_test_run") if row.payload.get("scenario_id") == scenario_id]
    return {"scenario_test_runs": [_test_run_dto(row) for row in rows]}


@router.post("/scenario-testrun", status_code=201)
def create_test_run(body: dict, principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    for field in ("scenario_id", "test_iteration_id", "end_date"):
        if field not in body:
            raise marble_error(422, "invalid_payload", f"{field} is required")
    scenario = _scenario(db, principal, str(body["scenario_id"]))
    iteration = _iteration(db, principal, str(body["test_iteration_id"]))
    try:
        end = parse_dt(str(body["end_date"]))
    except ValueError as exc:
        raise marble_error(422, "invalid_payload", "end_date must be a date-time") from exc
    tenant = _tenant(db, principal)
    created = 0
    for item in db.scalars(select(IngestedObject).where(IngestedObject.tenant_id == tenant.id, IngestedObject.object_type == scenario.trigger_object_type)).all():
        if item.updated_at <= end:
            created += 1
    row = add_doc(
        db,
        principal.tenant_id,
        "scenario_test_run",
        scenario.name,
        {
            "scenario_id": scenario.id,
            "ref_iteration_id": scenario.iteration_id,
            "test_iteration_id": iteration.id,
            "start_date": body.get("start_date") or iso(end),
            "end_date": iso(end),
            "creator_id": principal.credential_id or principal.tenant_id,
            "status": "completed",
            "decisions_counted": created,
            "note": "Comptage sur les objets ingérés. Aucune décision de production n'est réécrite.",
        },
        status="completed",
    )
    return {"scenario_test_run": _test_run_dto(row)}


def _test_run_dto(row: ConfigDocument) -> dict:
    payload = row.payload
    return {
        "id": row.id,
        "scenario_id": payload["scenario_id"],
        "ref_iteration_id": payload["ref_iteration_id"],
        "test_iteration_id": payload["test_iteration_id"],
        "start_date": payload["start_date"],
        "end_date": payload["end_date"],
        "creator_id": payload["creator_id"],
        "status": payload["status"],
    }


@router.get("/scenario-publications")
def list_publications(principal: Principal = Depends(marble_principal), db: Session = Depends(db_session)):
    return [
        {"id": row.id, "scenario_id": row.payload.get("scenario_id"), "scenario_iteration_id": row.payload.get("scenario_iteration_id"), "status": row.status, "created_at": iso(row.created_at)}
        for row in list_docs(db, principal.tenant_id, "scenario_publication")
    ]
