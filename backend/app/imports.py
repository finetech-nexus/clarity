"""Import Marble and Ballerine configuration documents into the executable store."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import add_doc, executable_from_formula, touch, validate_formula, workflow_definition_dto
from app.engine import PROCESS_CATALOG
from app.models import DataModelField, Scenario, new_id
from app.security import Principal
from app.services import clarity_error
from app.timeutil import iso, utcnow


def import_bundle(db: Session, principal: Principal, source: str, payload: dict) -> dict:
    if source == "marble":
        return _marble(db, principal, payload)
    if source == "ballerine":
        return _ballerine(db, principal, payload)
    raise clarity_error(400, "invalid", "source must be marble or ballerine")


def _marble(db: Session, principal: Principal, payload: dict) -> dict:
    imported = []
    warnings = []
    tables = payload.get("tables") if isinstance(payload.get("tables"), dict) else {}
    for name, table in tables.items():
        fields = table.get("fields") if isinstance(table, dict) else None
        if isinstance(fields, dict):
            names = list(fields)
        elif isinstance(table, dict) and isinstance(table.get("fields"), list):
            names = [str(field) for field in table["fields"]]
        else:
            names = []
        for field_name in names:
            exists = db.scalar(
                select(DataModelField).where(
                    DataModelField.tenant_id == principal.tenant_id,
                    DataModelField.object_type == name,
                    DataModelField.name == field_name,
                )
            )
            if exists:
                continue
            db.add(DataModelField(tenant_id=principal.tenant_id, object_type=str(name), name=field_name, required=field_name in {"object_id", "updated_at"}))
        if names:
            imported.append({"kind": "data_model", "name": name, "fields": len(names)})
    scenarios = payload.get("scenarios") if isinstance(payload.get("scenarios"), list) else []
    if payload.get("trigger_object_type"):
        scenarios = [payload, *scenarios]
    for item in scenarios:
        imported.append(_scenario(db, principal, item, warnings))
    lists = payload.get("lists") if isinstance(payload.get("lists"), list) else []
    for item in lists:
        if not item.get("name"):
            warnings.append("A custom list without a name was skipped.")
            continue
        row = add_doc(db, principal.tenant_id, "custom_list", str(item["name"]), {"description": item.get("description") or "", "values": item.get("values") or []})
        imported.append({"kind": "custom_list", "id": row.id, "name": row.name})
    if not imported:
        raise clarity_error(400, "invalid", "No Marble scenario, table or list was found in the document.")
    return {"source": "marble", "imported": imported, "warnings": warnings}


def _scenario(db: Session, principal: Principal, item: dict, warnings: list[str]) -> dict:
    name = item.get("name")
    trigger = item.get("trigger_object_type") or item.get("triggerObjectType")
    if not name or not trigger:
        raise clarity_error(400, "invalid", "A Marble scenario needs name and trigger_object_type.")
    known = {row.object_type for row in db.scalars(select(DataModelField).where(DataModelField.tenant_id == principal.tenant_id)).all()}
    if trigger not in known:
        for field_name in ("object_id", "updated_at"):
            db.add(DataModelField(tenant_id=principal.tenant_id, object_type=str(trigger), name=field_name, required=True))
        db.flush()
        warnings.append(f"Table {trigger} was added so the scenario can be stored.")
    iteration_body = item.get("iteration") if isinstance(item.get("iteration"), dict) else item.get("body") if isinstance(item.get("body"), dict) else {}
    scenario = Scenario(
        tenant_id=principal.tenant_id,
        name=str(name),
        iteration_id=new_id(),
        version="0",
        trigger_object_type=str(trigger),
        review_threshold=int(iteration_body.get("score_review_threshold") or 0),
        block_threshold=int(iteration_body.get("score_block_and_review_threshold") or 0),
        decline_threshold=int(iteration_body.get("score_decline_threshold") or 0),
        rules=[],
    )
    db.add(scenario)
    db.flush()
    add_doc(
        db,
        principal.tenant_id,
        "scenario_meta",
        scenario.name,
        {"description": item.get("description") or "Imported from a Marble configuration.", "archived": False, "created_at": iso(utcnow())},
        doc_id=scenario.id,
    )
    iteration = add_doc(
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
                "screening_configs": iteration_body.get("screening_configs") or [],
                "score_review_threshold": iteration_body.get("score_review_threshold"),
                "score_block_and_review_threshold": iteration_body.get("score_block_and_review_threshold"),
                "score_decline_threshold": iteration_body.get("score_decline_threshold"),
                "trigger_condition_ast_expression": iteration_body.get("trigger_condition_ast_expression"),
                "schedule": iteration_body.get("schedule"),
            },
        },
        parent_id=scenario.id,
        status="draft",
        doc_id=scenario.iteration_id,
    )
    rule_ids = []
    for index, rule in enumerate(iteration_body.get("rules") or item.get("rules") or []):
        formula = rule.get("formula_ast_expression") or rule.get("formula")
        checked = validate_formula(formula, "bool")
        if checked["errors"]:
            warnings.append(f"Rule {rule.get('name') or index + 1} was skipped: {checked['errors'][0]['message']}")
            continue
        score = int(rule.get("score_modifier") or 0)
        stored = add_doc(
            db,
            principal.tenant_id,
            "scenario_rule",
            str(rule.get("name") or f"rule-{index + 1}"),
            {
                "scenario_iteration_id": iteration.id,
                "display_order": rule.get("display_order") or index + 1,
                "description": rule.get("description") or "",
                "rule_group": rule.get("rule_group") or "imported",
                "score_modifier": score,
                "formula_ast_expression": formula,
                "executable": executable_from_formula(formula, score),
            },
            parent_id=iteration.id,
        )
        rule_ids.append(stored.id)
    body = dict(iteration.payload["body"])
    body["rules"] = rule_ids
    iteration.payload = {**iteration.payload, "body": body}
    touch(iteration)
    return {"kind": "scenario", "id": scenario.id, "name": scenario.name, "rules": len(rule_ids), "status": "draft"}


def _ballerine(db: Session, principal: Principal, payload: dict) -> dict:
    imported = []
    warnings = []
    workflows = payload.get("workflows") if isinstance(payload.get("workflows"), list) else []
    if payload.get("name") and (payload.get("definition") is not None or payload.get("documentsSchema") is not None or payload.get("processKey")):
        workflows = [payload, *workflows]
    for item in workflows:
        imported.append(_workflow(db, principal, item, warnings))
    if not imported:
        raise clarity_error(400, "invalid", "No Ballerine workflow definition was found in the document.")
    return {"source": "ballerine", "imported": imported, "warnings": warnings}


def _workflow(db: Session, principal: Principal, item: dict, warnings: list[str]) -> dict:
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise clarity_error(400, "invalid", "A Ballerine workflow needs a name.")
    original = item.get("processKey") or (item.get("definition") or {}).get("processKey")
    entity = item.get("entityType") or "individual"
    mapped = original if original in PROCESS_CATALOG else ("clarityKybBusiness" if entity == "business" else "clarityKycIndividual")
    if original and original not in PROCESS_CATALOG:
        warnings.append(f"{name}: processKey {original} is kept in extensions and executed as {mapped}.")
    row = add_doc(
        db,
        principal.tenant_id,
        "workflow_definition",
        name,
        {
            "displayName": item.get("displayName") or name,
            "version": item.get("version") or 1,
            "definitionType": item.get("definitionType") or "statechart",
            "processKey": mapped,
            "entityType": entity if entity in {"individual", "business", "any"} else "individual",
            "definition": item.get("definition") or {},
            "config": item.get("config") or {},
            "extensions": {**(item.get("extensions") or {}), "originalProcessKey": original},
            "contextSchema": item.get("contextSchema") or {},
            "documentsSchema": item.get("documentsSchema") or [],
            "submitStates": item.get("submitStates") or [],
        },
        status="published",
    )
    if item.get("uiSchema"):
        add_doc(db, principal.tenant_id, "ui_definition", f"{name}-ui", {"workflowDefinitionId": row.id, "uiSchema": item["uiSchema"], "locales": item.get("locales") or {}}, parent_id=row.id)
    return {"kind": "workflow_definition", "id": row.id, "name": row.name, "processKey": mapped, "documents": len(row.payload.get("documentsSchema") or []), "definition": workflow_definition_dto(row)["name"]}
