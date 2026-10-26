import json
from datetime import timedelta
from pathlib import Path
from xml.etree import ElementTree

import yaml
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.engine import PROCESS_CATALOG
from app.ids import (
    ANALYST_EMAIL,
    BALLERINE_API_KEY,
    CHECKER_EMAIL,
    DEMO_PASSWORD,
    INBOX_ID,
    MARBLE_API_KEY,
    SCENARIO_ID,
    WF_KYC_DEMO_ID,
)
from app.main import create_app
from app.models import Customer
from app.security import sign_payload
from app.timeutil import iso, utcnow

NS = {"bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL"}


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        seed_demo=True,
        env="test",
        flowable_url="",
        upload_dir=str(tmp_path / "uploads"),
    )
    app = create_app(settings)
    return TestClient(app)


def _login(client: TestClient, email: str) -> str:
    response = client.post("/api/clarity/v1/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def test_health_reports_fallback_engine(tmp_path):
    with _client(tmp_path) as client:
        body = client.get("/api/clarity/v1/health").json()
        assert body["workflow_engine"] == "in-process-fallback"
        assert body["production_screening"] is False
        assert body["production_identity_verification"] is False


def test_bpmn_tasks_match_engine_catalog():
    for path in (ROOT / "flowable" / "processes").glob("*.bpmn20.xml"):
        tree = ElementTree.parse(path)
        for process in tree.findall("bpmn:process", NS):
            key = process.attrib["id"]
            task_ids = {node.attrib["id"] for node in process.findall("bpmn:userTask", NS)}
            assert task_ids == set(PROCESS_CATALOG[key]["tasks"])


def test_marble_decision_contract_and_screening_does_not_decline(tmp_path):
    with _client(tmp_path) as client:
        unauthenticated = client.post("/v1/decisions", json={})
        assert unauthenticated.status_code == 401
        assert unauthenticated.content == b""

        denied = client.post(
            "/v1/decisions",
            headers={"X-API-KEY": BALLERINE_API_KEY},
            json={"scenario_id": SCENARIO_ID, "trigger_object": {"object_id": "obj-1", "updated_at": iso(utcnow())}},
        )
        assert denied.status_code == 401

        created = client.post(
            "/v1/decisions?include_screening_matches=true",
            headers={"X-API-KEY": MARBLE_API_KEY},
            json={
                "scenario_id": SCENARIO_ID,
                "trigger_object": {
                    "object_id": "cp-viktor",
                    "updated_at": iso(utcnow()),
                    "amount": 15000,
                    "country": "FR",
                    "counterparty_name": "Viktor Petrov",
                    "birth_date": "1974-03-12",
                },
            },
        )
        assert created.status_code == 200, created.text
        payload = created.json()
        decision = payload["data"][0]
        for field in ("id", "outcome", "review_status", "scenario", "trigger_object", "score", "created_at", "rules", "screenings"):
            assert field in decision
        assert decision["outcome"] in {"approve", "review", "block_and_review", "decline"}
        assert decision["outcome"] != "decline"
        assert decision["outcome"] == "block_and_review"
        assert decision["review_status"] == "pending"
        matches = decision["screenings"][0]["matches"]
        assert matches
        assert all(match["status"] == "pending" for match in matches)
        assert matches[0]["payload"]["synthetic"] is True
        assert "SYNTHETIC FIXTURE" in matches[0]["payload"]["properties"]["notes"][0]

        fetched = client.get(f"/v1/decisions/{decision['id']}", headers={"X-API-KEY": MARBLE_API_KEY})
        assert fetched.status_code == 200
        assert fetched.json()["data"]["id"] == decision["id"]

        too_wide = client.get(
            "/v1/decisions",
            headers={"X-API-KEY": MARBLE_API_KEY},
            params={"start": "2020-01-01T00:00:00Z", "end": "2020-06-01T00:00:00Z"},
        )
        assert too_wide.status_code == 400
        assert too_wide.json()["code"] == "invalid_payload"


def test_marble_case_ingestion_and_whitelist(tmp_path):
    headers = {"X-API-KEY": MARBLE_API_KEY}
    with _client(tmp_path) as client:
        created = client.post(
            "/v1beta/cases",
            headers=headers,
            json={"inbox_id": INBOX_ID, "name": "Dossier démonstration"},
        )
        assert created.status_code == 200, created.text
        case = created.json()["data"]
        assert case["status"] == "pending"
        assert case["outcome"] == "unset"
        assert case["review_level"] is None

        comment = client.post(
            f"/v1beta/cases/{case['id']}/comments",
            headers=headers,
            json={"comment": "Première note d'investigation."},
        )
        assert comment.status_code == 201
        assert comment.json()["data"]["comment"].startswith("Première")

        ingested = client.post(
            "/v1/ingest/customers",
            headers=headers,
            json={"object_id": "ext-1", "updated_at": "2026-10-25T00:00:00Z", "name": "Claire Martin", "type": "individual", "country": "FR"},
        )
        assert ingested.status_code == 201
        again = client.post(
            "/v1/ingest/customers",
            headers=headers,
            json={"object_id": "ext-1", "updated_at": "2026-10-25T00:00:00Z", "name": "Claire Martin", "type": "individual", "country": "FR"},
        )
        assert again.status_code == 200
        unknown = client.post(
            "/v1/ingest/customers",
            headers=headers,
            json={"object_id": "ext-2", "updated_at": "2026-10-25T00:00:00Z", "passport": "secret"},
        )
        assert unknown.status_code == 400

        listed = client.post("/screening/whitelists", headers=headers, json={"counterparty": "cp", "entity_id": "syn-san-001"})
        assert listed.status_code == 404
        added = client.post("/v1/screening/whitelists", headers=headers, json={"counterparty": "cp", "entity_id": "syn-san-001"})
        assert added.status_code == 201
        found = client.post("/v1/screening/whitelists/search", headers=headers, json={"entity_id": "syn-san-001"})
        assert found.status_code == 200
        assert found.json()["data"][0]["counterparty"] == "cp"


def test_ballerine_and_clarity_share_customer(tmp_path):
    with _client(tmp_path) as client:
        denied = client.post(
            "/api/v1/external/workflows/run",
            headers={"Authorization": f"Bearer {MARBLE_API_KEY}"},
            json={"workflowId": "kyc", "context": {"entity": {"type": "individual", "id": "x", "data": {}}}},
        )
        assert denied.status_code == 401
        run = client.post(
            "/api/v1/external/workflows/run",
            headers={"Authorization": f"Bearer {BALLERINE_API_KEY}"},
            json={
                "workflowId": "kyc-demo",
                "context": {
                    "entity": {
                        "type": "individual",
                        "id": "bank-customer-42",
                        "data": {
                            "firstName": "Ines",
                            "lastName": "Moreau",
                            "dateOfBirth": "1992-02-02",
                            "nationality": "FR",
                            "country": "FR",
                            "email": "ines.moreau@example.test",
                        },
                    },
                    "documents": [],
                },
            },
        )
        assert run.status_code == 200, run.text
        body = run.json()
        assert body["entity"] == {"type": "individual", "id": "bank-customer-42"}
        assert body["workflowRuntimeId"] != body["ballerineEntityId"]
        listed = client.get("/api/v1/external/workflows", headers={"Authorization": f"Bearer {BALLERINE_API_KEY}"})
        assert listed.status_code == 200
        assert listed.json()["meta"]["total"] >= 1
        assert listed.json()["results"][0]["status"] in {"active", "completed", "failed"}

        token = _login(client, ANALYST_EMAIL)
        customers = client.get("/api/clarity/v1/customers", headers={"Authorization": f"Bearer {token}"})
        assert customers.status_code == 200
        match = [row for row in customers.json() if row["external_ref"] == "bank-customer-42"]
        assert match
        profile = client.get(f"/api/clarity/v1/customers/{match[0]['id']}", headers={"Authorization": f"Bearer {token}"})
        assert profile.status_code == 200
        assert profile.json()["verification_checks"][0]["production"] is False
        assert "pas" in profile.json()["verification_checks"][0]["detail"]["notice"] or "n'est pas" in profile.json()["verification_checks"][0]["detail"]["notice"] or "ne doit pas" in profile.json()["verification_checks"][0]["detail"]["notice"]


def test_maker_checker_and_tenant_isolation(tmp_path):
    with _client(tmp_path) as client:
        analyst = _login(client, ANALYST_EMAIL)
        checker = _login(client, CHECKER_EMAIL)
        opened = client.post(
            "/api/clarity/v1/onboarding/individuals",
            headers={"Authorization": f"Bearer {analyst}"},
            json={
                "given_name": "Luc",
                "family_name": "Renard",
                "date_of_birth": "1985-05-05",
                "nationality": "FR",
                "residence_country": "FR",
                "address": "1 place de la Bourse, Paris",
                "email": "luc.renard@example.test",
                "phone": "+33102030405",
                "purpose": "Compte courant",
                "expected_activity": "Salaire",
                "source_of_funds": "Salaire",
                "external_ref": "luc-renard",
            },
        )
        assert opened.status_code == 200, opened.text
        workflow_id = opened.json()["workflow_id"]
        assert opened.json()["state"] == "analystReview"
        assert opened.json()["engine"] == "in-process-fallback"
        same = client.post(
            f"/api/clarity/v1/workflows/{workflow_id}/actions",
            headers={"Authorization": f"Bearer {analyst}"},
            json={"action": "approve", "rationale": "Dossier complet, CDD standard."},
        )
        assert same.status_code == 200, same.text
        forbidden = client.post(
            f"/api/clarity/v1/workflows/{workflow_id}/actions",
            headers={"Authorization": f"Bearer {analyst}"},
            json={"action": "approve", "rationale": "Je ne peux pas me contrôler."},
        )
        assert forbidden.status_code == 403
        approved = client.post(
            f"/api/clarity/v1/workflows/{workflow_id}/actions",
            headers={"Authorization": f"Bearer {checker}"},
            json={"action": "approve", "rationale": "Contrôle de second niveau favorable."},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["state"] == "approved"

        session = client.app.state.database.session_factory()
        other = Customer(tenant_id="99999999-9999-4999-8999-999999999999", kind="individual", display_name="Autre banque", canonical_status="approved")
        session.add(other)
        try:
            session.commit()
        except Exception:
            session.rollback()
            from app.models import Tenant

            session.add(Tenant(id="99999999-9999-4999-8999-999999999999", name="Autre", slug="autre"))
            session.flush()
            session.add(Customer(tenant_id="99999999-9999-4999-8999-999999999999", kind="individual", display_name="Autre banque", canonical_status="approved"))
            session.commit()
            other_id = session.query(Customer).filter(Customer.display_name == "Autre banque").one().id
        else:
            other_id = other.id
        session.close()
        visible = client.get("/api/clarity/v1/customers", headers={"Authorization": f"Bearer {analyst}"})
        assert all(row["id"] != other_id for row in visible.json())
        hidden = client.get(f"/api/clarity/v1/customers/{other_id}", headers={"Authorization": f"Bearer {analyst}"})
        assert hidden.status_code == 404


def test_monitoring_alert_backtest_and_report(tmp_path):
    with _client(tmp_path) as client:
        investigator = _login(client, "lea.martin@clarity.bank")
        alerts = client.get("/api/clarity/v1/alerts", headers={"Authorization": f"Bearer {investigator}"})
        assert alerts.status_code == 200
        assert alerts.json()
        assert all("blanchiment" in item["rationale"] or "investiguer" in item["title"].lower() or "Scénario" in item["title"] for item in alerts.json())
        rules = client.get("/api/clarity/v1/rules", headers={"Authorization": f"Bearer {investigator}"}).json()
        rule_id = rules[0]["id"]
        replay = client.post(
            "/api/clarity/v1/monitoring/backtests",
            headers={"Authorization": f"Bearer {investigator}"},
            json={"rule_id": rule_id, "start": iso(utcnow() - timedelta(days=7)), "end": iso(utcnow() + timedelta(days=1))},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["alerts_created"] == 0
        case_id = alerts.json()[0]["case_id"]
        report = client.post(
            f"/api/clarity/v1/cases/{case_id}/reports",
            headers={"Authorization": f"Bearer {investigator}"},
            json={"narrative": "Préparation interne. Aucun dépôt."},
        )
        assert report.status_code == 200, report.text
        assert report.json()["submission_channel"] == "not_connected"
        audit = client.get("/api/clarity/v1/audit", headers={"Authorization": f"Bearer {investigator}"})
        assert any(event["action"] == "report.draft" for event in audit.json())


def test_ballerine_kyc_vendor_is_not_faked(tmp_path):
    with _client(tmp_path) as client:
        run = client.post(
            "/api/v1/external/workflows/run",
            headers={"Authorization": f"Bearer {BALLERINE_API_KEY}"},
            json={"workflowId": "kyc", "context": {"entity": {"type": "individual", "id": "vendor-user", "data": {"firstName": "Noa", "lastName": "Blanc", "country": "FR"}}}},
        )
        runtime_id = run.json()["workflowRuntimeId"]
        response = client.post(
            "/api/v1/external/kyc",
            headers={"Authorization": f"Bearer {BALLERINE_API_KEY}"},
            json={"endUserId": "vendor-user", "workflowRuntimeDataId": runtime_id, "vendor": "veriff", "language": "fr"},
        )
        assert response.status_code == 501
        assert "No verification session" in response.json()["message"]


def test_webhook_signature(tmp_path):
    with _client(tmp_path) as client:
        run = client.post(
            "/api/v1/external/workflows/run",
            headers={"Authorization": f"Bearer {BALLERINE_API_KEY}"},
            json={"workflowId": "kyc", "context": {"entity": {"type": "individual", "id": "hook-user", "data": {"firstName": "Lea", "lastName": "Faure", "country": "FR"}}}},
        )
        runtime_id = run.json()["workflowRuntimeId"]
        payload = {"event": "workflow.context.changed"}
        bad = client.post(f"/api/v1/external/workflows/{runtime_id}/hook/workflow.context.changed", json=payload)
        assert bad.status_code == 401
        signature = sign_payload("clarity-dev-webhook-secret", payload)
        good = client.post(
            f"/api/v1/external/workflows/{runtime_id}/hook/workflow.context.changed",
            json=payload,
            headers={"x-hmac-signature": signature},
        )
        assert good.status_code == 200, good.text


def test_official_marble_spec_is_unmodified_and_scoped(tmp_path):
    spec_path = ROOT / "docs" / "api-compatibility" / "marble" / "original" / "v1.yml"
    upstream = ROOT.parent / "marble" / "api" / "pubapi" / "openapi" / "v1.yml"
    assert spec_path.read_bytes() == upstream.read_bytes()
    document = yaml.safe_load(spec_path.read_text())
    operation_ids = set()
    for item in document["paths"].values():
        for method, operation in item.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                operation_ids.add(operation["operationId"])
    with _client(tmp_path) as client:
        paths = client.app.openapi()["paths"]
    implemented = set()
    for path, item in paths.items():
        if not path.startswith("/v1/") and path != "/v1":
            continue
        if path.startswith("/v1beta"):
            continue
        for method, operation in item.items():
            if method in {"get", "post", "put", "patch", "delete"} and operation.get("operationId"):
                implemented.add(operation["operationId"])
    # FastAPI operationIds are generated and are not the upstream ids.
    # The compatibility claim is the path set below, checked directly.
    expected = {
        "/decisions",
        "/decisions/all",
        "/decisions/{decision_id}",
        "/decisions/{decision_id}/snooze",
        "/decisions/{decision_id}/screenings",
        "/screening/entities/{entity_id}",
        "/screening/search",
        "/screening/{screening_id}/refine",
        "/screening/{screening_id}/search",
        "/screening/matches/{match_id}",
        "/screening/whitelists",
        "/screening/whitelists/search",
        "/batch-executions",
        "/ingest/{object_type}",
        "/ingest/{object_type}/batch",
    }
    served = {path[len("/v1") :] for path in paths if path.startswith("/v1/") and not path.startswith("/v1beta")}
    assert expected <= served
    assert "createDecision" in operation_ids
    assert json.loads((ROOT / "docs" / "api-compatibility" / "unified" / "compatibility-report.json").read_text())["fully_compatible_ratio"] < 1


def test_configuration_is_executable(tmp_path):
    marble = {"X-API-KEY": MARBLE_API_KEY}
    ballerine = {"Authorization": f"Bearer {BALLERINE_API_KEY}"}
    with _client(tmp_path) as client:
        scenarios = client.get("/scenarios", headers=marble)
        assert scenarios.status_code == 200, scenarios.text
        assert any(row["id"] == SCENARIO_ID for row in scenarios.json())
        model = client.get("/data-model", headers=marble)
        assert "transactions" in model.json()["tables"]
        created = client.post(
            "/scenarios",
            headers=marble,
            json={"name": "Seuil de test", "description": "Règle publiée par le test", "trigger_object_type": "transactions"},
        )
        assert created.status_code == 200, created.text
        scenario_id = created.json()["id"]
        iterations = client.get("/scenario-iterations", headers=marble, params={"scenario_id": scenario_id})
        iteration_id = iterations.json()[0]["id"]
        updated = client.patch(
            f"/scenario-iterations/{iteration_id}",
            headers=marble,
            json={"body": {"score_review_threshold": 10, "score_block_and_review_threshold": 50, "score_decline_threshold": 200}},
        )
        assert updated.status_code == 200, updated.text
        rule = client.post(
            "/scenario-iteration-rules",
            headers=marble,
            json={
                "scenario_iteration_id": iteration_id,
                "display_order": 1,
                "name": "Montant présent",
                "description": "Score si le montant est renseigné et supérieur ou égal à 1",
                "rule_group": "test",
                "score_modifier": 80,
                "formula_ast_expression": {
                    "name": ">=",
                    "children": [
                        {"name": "Payload", "named_children": {"path": {"constant": "amount"}}},
                        {"constant": 1},
                    ],
                },
            },
        )
        assert rule.status_code == 200, rule.text
        committed = client.post(f"/scenario-iterations/{iteration_id}/commit", headers=marble)
        assert committed.status_code == 200, committed.text
        decision = client.post(
            "/v1/decisions",
            headers=marble,
            json={"scenario_id": scenario_id, "trigger_object": {"object_id": "cfg-1", "updated_at": iso(utcnow()), "amount": 5}},
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["data"][0]["outcome"] == "block_and_review"
        assert decision.json()["data"][0]["score"] == 80

        definitions = client.get("/api/v1/workflow-definition", headers=ballerine)
        assert definitions.status_code == 200, definitions.text
        assert any(row["id"] == WF_KYC_DEMO_ID and row["name"] == "kyc-demo" for row in definitions.json())
        run = client.post(
            "/api/v1/external/workflows/run",
            headers=ballerine,
            json={"workflowId": "kyc-demo", "context": {"entity": {"type": "individual", "id": "cfg-person", "data": {"firstName": "Nora", "lastName": "Blanc"}}}},
        )
        assert run.status_code == 200, run.text
        assert run.json()["workflowDefinitionId"] == WF_KYC_DEMO_ID
        created_definition = client.post(
            "/api/v1/workflow-definition",
            headers=ballerine,
            json={"name": "kyc-agence", "displayName": "Agence", "processKey": "clarityKycIndividual", "entityType": "individual"},
        )
        assert created_definition.status_code == 201, created_definition.text
        token = _login(client, ANALYST_EMAIL)
        studio = client.get("/api/clarity/v1/studio", headers={"Authorization": f"Bearer {token}"})
        assert studio.status_code == 200, studio.text
        assert studio.json()["marble"]["scenarios"]
        assert studio.json()["ballerine"]["workflow_definitions"]


def test_console_routes_token_and_case_review(tmp_path):
    from app.api.console_coverage import console_operations

    marble = {"X-API-KEY": MARBLE_API_KEY}
    ballerine = {"Authorization": f"Bearer {BALLERINE_API_KEY}"}
    with _client(tmp_path) as client:
        served = set()
        for path, item in client.app.openapi()["paths"].items():
            for method in item:
                if method in {"get", "post", "put", "patch", "delete"}:
                    served.add((method.upper(), normalize_template(path)))
        missing = [f"{method} {path}" for method, path, _operation in console_operations() if (method, normalize_template(path)) not in served]
        assert not missing, missing[:12]
        token = client.post("/token", headers=marble)
        assert token.status_code == 200, token.text
        assert token.json()["token_type"] == "Bearer"
        cases = client.get("/cases", headers=marble)
        assert cases.status_code == 200, cases.text
        case_id = cases.json()["items"][0]["id"]
        review = client.post(f"/cases/{case_id}/review/enqueue", headers=marble, json={})
        assert review.status_code == 200, review.text
        assert review.json()["production"] is False
        assert review.json()["provider"] == "clarity-local-analyst"
        datasets = client.get("/screenings/datasets", headers=marble)
        assert datasets.status_code == 200, datasets.text
        assert datasets.json()["provider"] == "opensanctions"
        run = client.post(
            "/api/v1/external/workflows/run",
            headers=ballerine,
            json={"workflowId": "kyc-demo", "context": {"entity": {"type": "individual", "id": "journey-user", "data": {"firstName": "Noa", "lastName": "Blanc"}}}},
        )
        runtime_id = run.json()["workflowRuntimeId"]
        issued = client.post("/api/v1/external/workflows/create-token", headers=ballerine, json={"workflowRuntimeDataId": runtime_id, "endUserId": "journey-user"})
        assert issued.status_code == 200, issued.text
        link = client.post("/api/v1/external/workflows/create-collection-flow-url", headers=ballerine, json={"workflowRuntimeDataId": runtime_id})
        assert link.status_code == 200, link.text
        assert "parcours?workflowId=" in link.json()["collectionFlowUrl"]
        context = client.get(f"/api/v1/collection-flow/context?workflowRuntimeId={runtime_id}", headers={"Authorization": f"Bearer {issued.json()['token']}"})
        assert context.status_code == 200, context.text
        assert context.json()["auth"] == "workflow-token"


def test_kyc_and_case_desks_show_documents(tmp_path):
    with _client(tmp_path) as client:
        headers = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        queue = client.get("/api/clarity/v1/desks/kyc", headers=headers)
        assert queue.status_code == 200, queue.text
        amina = next(row for row in queue.json() if row["name"] == "Amina El Fassi")
        detail = client.get(f"/api/clarity/v1/desks/kyc/{amina['id']}", headers=headers)
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert "Les pièces affichées" not in detail.text
        assert any(field["key"] == "firstName" for field in body["submitted"])
        passport = next(item for item in body["documents"] if item["type"] == "passport")
        read = {field["key"]: field for field in passport["extracted"]}
        assert read["firstName"]["value"] == ""
        assert read["lastName"]["value"] == ""
        assert read["dateOfBirth"]["value"] == ""
        assert read["firstName"]["match"] is False
        assert body["alert"] is True
        assert "correspondence" in body["alert_reasons"]
        documents = body["documents"]
        assert len(documents) >= 2
        assert documents[0]["extracted"]
        edited = client.patch(
            f"/api/clarity/v1/desks/kyc/{amina['id']}/documents/{documents[0]['id']}",
            headers=headers,
            json={"fields": {"expiry": "2030-01-01", "documentNumber": "SPEC-1"}},
        )
        assert edited.status_code == 200, edited.text
        saved = {field["key"]: field["value"] for field in edited.json()["extracted"]}
        assert saved["expiry"] == "2030-01-01"
        assert saved["documentNumber"] == "SPEC-1"
        content = client.get(documents[0]["url"], headers=headers)
        assert content.status_code == 200, content.text
        assert b"SPECIMEN" in content.content
        revised = client.post(
            f"/api/clarity/v1/desks/documents/{documents[0]['id']}/decision",
            headers=headers,
            json={"decision": "revision"},
        )
        assert revised.status_code == 200, revised.text
        assert revised.json()["decision"] == "revisions"
        ocr = client.post(f"/api/clarity/v1/desks/kyc/{amina['id']}/documents/{passport['id']}/ocr", headers=headers)
        assert ocr.status_code == 200, ocr.text
        parsed = ocr.json()["parsedData"]
        assert parsed["firstName"] == "Amina"
        assert parsed["lastName"] == "El Fassi"
        assert parsed["dateOfBirth"] == ""
        assert ocr.json()["production"] is False
        assert ocr.json()["engine"] == "svg-text"
        reread = client.get(f"/api/clarity/v1/desks/kyc/{amina['id']}", headers=headers)
        fields = {field["key"]: field for field in next(item for item in reread.json()["documents"] if item["type"] == "passport")["extracted"]}
        assert fields["firstName"]["match"] is True
        assert fields["dateOfBirth"]["match"] is False
        assert reread.json()["alert"] is True
        ballerine = {"Authorization": f"Bearer {BALLERINE_API_KEY}"}
        again = client.get(f"/api/v1/internal/workflows/{amina['id']}/documents/{passport['id']}/run-ocr", headers=ballerine)
        assert again.status_code == 200, again.text
        assert again.json()["parsedData"]["firstName"] == "Amina"
        cases = client.get("/api/clarity/v1/desks/cases", headers=headers)
        assert cases.status_code == 200 and cases.json()
        manager = client.get(f"/api/clarity/v1/desks/cases/{cases.json()[0]['id']}", headers=headers)
        assert manager.status_code == 200, manager.text
        assert manager.json()["files"]
        assert "events" in manager.json()


def test_verification_file_shows_aml_answers_from_the_journey(tmp_path):
    with _client(tmp_path) as client:
        ballerine = {"Authorization": f"Bearer {BALLERINE_API_KEY}"}
        headers = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        entity = {"type": "individual", "id": "aml-user", "data": {"firstName": "Amine", "lastName": "Miladi", "dateOfBirth": "1983-06-02"}}
        collection = client.post(
            "/api/v1/external/workflows/run",
            headers=ballerine,
            json={"workflowId": "kyc_collection_flow", "context": {"entity": entity, "documents": []}},
        )
        assert collection.status_code == 200, collection.text
        runtime = collection.json()["workflowRuntimeId"]
        patched = client.patch(
            f"/api/v1/external/workflows/{runtime}",
            headers=ballerine,
            json={
                "context": {
                    "marbleAml": {
                        "provided": {
                            "pepPublicFunction": False,
                            "pepFamilyAssociate": True,
                            "pepFinancialInvolvement": False,
                        },
                        "system": {"score": 12, "outcome": "review"},
                    }
                }
            },
        )
        assert patched.status_code == 200, patched.text
        review = client.post(
            "/api/v1/external/workflows/run",
            headers=ballerine,
            json={"workflowId": "risk-score-improvement-dev", "context": {"entity": entity}},
        )
        assert review.status_code == 200, review.text
        detail = client.get(f"/api/clarity/v1/desks/kyc/{review.json()['workflowRuntimeId']}", headers=headers)
        assert detail.status_code == 200, detail.text
        fields = {field["key"]: field for field in detail.json()["submitted"]}
        assert fields["firstName"]["value"] == "Amine"
        assert fields["pepPublicFunction"]["value"] == "Non"
        assert fields["pepFamilyAssociate"]["value"] == "Oui"
        assert fields["pepFinancialInvolvement"]["value"] == "Non"
        assert "fonction publique" in fields["pepPublicFunction"]["label"]
        assert fields["amlScore"]["value"] == "12"
        assert fields["amlOutcome"]["value"] == "review"


def test_document_ocr_reads_labels_and_mrz():
    from app.ocr import read_identity

    labeled = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg"><text>Prénom: Claire</text><text>Nom: Dumont</text><text>Date de naissance: 18/04/1988</text></svg>""".encode()
    )
    assert labeled["parsedData"]["firstName"] == "Claire"
    assert labeled["parsedData"]["lastName"] == "Dumont"
    assert labeled["parsedData"]["dateOfBirth"] == "1988-04-18"
    assert labeled["production"] is False
    line_one = "P<FRADUMONT<<CLAIRE" + "<" * 25
    line_two = "12AB345678FRA8804186F3001012" + "<" * 15 + "4"
    assert len(line_one) == 44 and len(line_two) == 44
    from xml.sax.saxutils import escape

    mrz = read_identity(
        f'<svg xmlns="http://www.w3.org/2000/svg"><text>{escape(line_one)}</text><text>{escape(line_two)}</text></svg>'.encode()
    )
    assert mrz["parsedData"]["firstName"] == "Claire"
    assert mrz["parsedData"]["lastName"] == "Dumont"
    assert mrz["parsedData"]["dateOfBirth"] == "1988-04-18"
    assert mrz["parsedData"]["nationality"] == "FRA"
    assert mrz["parsedData"]["documentNumber"] == "12AB34567"
    assert mrz["parsedData"]["expiry"] == "2030-01-01"
    card = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg">
        <text>Nom</text><text>MILADI</text>
        <text>Prenoms</text><text>Mohamed Amine</text>
        <text>Nationatite Francaie</text>
        <text>Date de naissance</text><text>02061983</text>
        <text>Adresse</text><text>12 rue de la Paix</text><text>75002 Paris</text>
        </svg>""".encode()
    )
    assert card["parsedData"]["firstName"] == "Mohamed Amine"
    assert card["parsedData"]["lastName"] == "MILADI"
    assert card["parsedData"]["dateOfBirth"] == "1983-06-02"
    assert card["parsedData"]["nationality"] == "FRA"
    assert "rue de la Paix" in card["parsedData"]["address"]
    noise = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg"><text>Nationatite Francaie</text></svg>""".encode()
    )
    assert noise["parsedData"]["firstName"] == ""
    assert noise["parsedData"]["lastName"] == ""


def test_scenario_editor_and_document_types(tmp_path):
    with _client(tmp_path) as client:
        headers = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        scenarios = client.get("/scenarios", headers=headers)
        scenario_id = scenarios.json()[0]["id"]
        draft = client.post(
            "/scenario-iterations",
            headers=headers,
            json={
                "scenario_id": scenario_id,
                "body": {"score_review_threshold": 10, "score_block_and_review_threshold": 20, "score_decline_threshold": 40},
            },
        )
        assert draft.status_code == 200, draft.text
        rule = client.post(
            "/scenario-iteration-rules",
            headers=headers,
            json={
                "scenario_iteration_id": draft.json()["id"],
                "display_order": 1,
                "name": "Montant atelier",
                "description": "Seuil d'atelier",
                "rule_group": "atelier",
                "score_modifier": 15,
                "formula_ast_expression": {
                    "name": ">=",
                    "children": [
                        {"name": "Payload", "named_children": {"path": {"constant": "amount"}}},
                        {"constant": 1000},
                    ],
                },
            },
        )
        assert rule.status_code == 200, rule.text
        published = client.post(f"/scenario-iterations/{draft.json()['id']}/commit", headers=headers, json={})
        assert published.status_code == 200, published.text
        definitions = client.get("/api/v1/workflow-definition", headers={"Authorization": f"Bearer {BALLERINE_API_KEY}"})
        kyc = next(row for row in definitions.json() if row["name"] == "kyc")
        assert any(item["type"] == "passport" for item in kyc["documentsSchema"])
        replaced = client.put(
            f"/api/v1/workflow-definition/{kyc['id']}/documents-schema",
            headers={"Authorization": f"Bearer {BALLERINE_API_KEY}"},
            json=[{"category": "identity", "type": "residence_permit", "issuer": {"country": "FR"}, "required": True}],
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["documentsSchema"][0]["type"] == "residence_permit"
        queue = client.get("/api/clarity/v1/desks/kyc", headers=headers)
        amina = next(row for row in queue.json() if row["name"] == "Amina El Fassi")
        detail = client.get(f"/api/clarity/v1/desks/kyc/{amina['id']}", headers=headers)
        assert any(item["type"] == "passport" and item["status"] != "missing" for item in detail.json()["tracker"])


def test_import_integrations_and_replacement(tmp_path):
    from app.config import get_settings
    from app.providers import veriff_configured

    with _client(tmp_path) as client:
        headers = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        report = client.get("/api/clarity/v1/compatibility", headers=headers)
        assert report.status_code == 200, report.text
        body = report.json()
        assert body["marble_public"]["replaces"] is True, body["marble_public"]["missing"]
        assert body["marble_console"]["replaces"] is True, body["marble_console"]["missing"]
        assert body["ballerine"]["replaces"] is True, body["ballerine"]["missing"]
        assert body["schema_identical"] is False
        assert "magic-link" in " ".join(body["not_replaced"])

        imported = client.post(
            "/api/clarity/v1/imports",
            headers=headers,
            json={
                "source": "marble",
                "payload": {
                    "name": "Scénario importé",
                    "description": "Import de configuration Marble",
                    "trigger_object_type": "transactions",
                    "iteration": {
                        "score_review_threshold": 5,
                        "score_block_and_review_threshold": 15,
                        "score_decline_threshold": 30,
                        "rules": [
                            {
                                "name": "Montant importé",
                                "score_modifier": 10,
                                "formula_ast_expression": {
                                    "name": ">=",
                                    "children": [
                                        {"name": "Payload", "named_children": {"path": {"constant": "amount"}}},
                                        {"constant": 1000},
                                    ],
                                },
                            }
                        ],
                    },
                    "lists": [{"name": "pays_importes", "values": ["ZZ"]}],
                },
            },
        )
        assert imported.status_code == 200, imported.text
        assert any(item["kind"] == "scenario" and item["rules"] == 1 for item in imported.json()["imported"])
        scenarios = client.get("/scenarios", headers=headers)
        assert any(row["name"] == "Scénario importé" for row in scenarios.json())

        workflow = client.post(
            "/api/clarity/v1/imports",
            headers=headers,
            json={
                "source": "ballerine",
                "payload": {
                    "name": "imported-kyc",
                    "displayName": "KYC importé",
                    "processKey": "ballerine-kyc-original",
                    "entityType": "individual",
                    "documentsSchema": [{"category": "identity", "type": "passport", "issuer": {"country": "FR"}}],
                },
            },
        )
        assert workflow.status_code == 200, workflow.text
        assert workflow.json()["warnings"]
        definitions = client.get("/api/v1/workflow-definition", headers={"Authorization": f"Bearer {BALLERINE_API_KEY}"})
        stored = next(row for row in definitions.json() if row["name"] == "imported-kyc")
        assert stored["processKey"] == "clarityKycIndividual"
        assert stored["documentsSchema"][0]["type"] == "passport"

        secret = "clry_test_opensanctions"
        saved = client.patch("/api/clarity/v1/integrations/sanctions", headers=headers, json={"api_key": secret})
        assert saved.status_code == 200, saved.text
        assert saved.json()["has_secret"] is True
        assert secret not in saved.text
        listed = client.get("/api/clarity/v1/integrations", headers=headers)
        assert secret not in listed.text
        assert get_settings().opensanctions_api_key == secret

        veriff = client.patch(
            "/api/clarity/v1/integrations/identity",
            headers=headers,
            json={"api_key": "veriff-key", "shared_secret": "veriff-secret"},
        )
        assert veriff.status_code == 200, veriff.text
        assert veriff_configured() is True
        assert "veriff-secret" not in veriff.text

        upload = client.put("/v1beta/ingest/transactions/uploads", headers={"X-API-KEY": MARBLE_API_KEY})
        assert upload.status_code == 202, upload.text
        target = upload.json()["data"]["upload_url"]
        path = target.split(":8095")[-1] if ":8095" in target else target[target.find("/v1beta") :]
        received = client.put(path, content="object_id,updated_at\nimp-1,2026-01-01T00:00:00Z\n", headers={"content-type": "text/csv"})
        assert received.status_code == 200, received.text
        assert received.json()["rows_ingested"] == 1
        assert client.get("/api/v1/_health/live").status_code == 204
        registry = client.get("/api/v1/collection-flow/business/business-information", headers={"Authorization": f"Bearer {BALLERINE_API_KEY}"}, params={"registrationNumber": "1", "countryCode": "FR"})
        assert registry.status_code == 501


def test_kyc_api_can_collect_files_and_ingest(tmp_path):
    ballerine = {"Authorization": f"Bearer {BALLERINE_API_KEY}"}
    marble = {"X-API-Key": MARBLE_API_KEY}
    entity = {"type": "individual", "id": "kyc-api-user", "data": {"firstName": "Sara", "lastName": "Lopez", "country": "FR"}}
    with _client(tmp_path) as client:
        started = client.post(
            "/api/v1/external/workflows/run",
            headers=ballerine,
            json={"workflowId": "kyc_collection_flow", "context": {"entity": entity, "documents": []}},
        )
        assert started.status_code == 200, started.text
        runtime_id = started.json()["workflowRuntimeId"]
        current = client.get(f"/api/v1/external/workflows/{runtime_id}", headers=ballerine)
        assert current.json()["state"] == "collect"
        issued = client.post(
            "/api/v1/external/workflows/create-token",
            headers={**ballerine, "X-API-KEY": "secret"},
            json={"workflowRuntimeDataId": runtime_id, "endUserId": started.json()["ballerineEntityId"]},
        )
        assert issued.status_code == 200, issued.text
        token = {"Authorization": f"Bearer {issued.json()['token']}"}
        uploaded = client.post(
            "/api/v1/collection-flow/files",
            headers=token,
            files={"file": ("passport.jpeg", b"\xff\xd8\xff passport-bytes", "image/jpeg")},
        )
        assert uploaded.status_code == 200, uploaded.text
        file_id = uploaded.json()["ballerineFileId"]
        assert uploaded.json()["id"] == file_id
        stored = client.get(f"/api/v1/collection-flow/files/{file_id}", headers=token)
        assert stored.status_code == 200, stored.text
        synced = client.patch(
            "/api/v1/collection-flow/sync/context",
            headers=token,
            json={"context": {"documents": [{"type": "passport", "category": "proof_of_identity", "pages": [{"ballerineFileId": file_id, "metadata": {"side": "front"}}]}]}},
        )
        assert synced.status_code == 200, synced.text
        page = client.post(
            f"/api/v1/external/workflows/{runtime_id}",
            headers=ballerine,
            json={"state": "check-selfie", "tags": ["check-selfie"]},
        )
        assert page.status_code == 200, page.text
        assert page.json()["context"]["collectionState"] == "check-selfie"
        stepped = client.post(f"/api/v1/external/workflows/{runtime_id}/event", headers=ballerine, json={"name": "USER_NEXT_STEP"})
        assert stepped.status_code == 200, stepped.text
        analyst = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        content = client.get(f"/api/clarity/v1/desks/documents/{file_id}/content", headers=analyst)
        assert content.status_code == 200, content.text
        assert content.content == b"\xff\xd8\xff passport-bytes"
        review = client.post(
            "/api/v1/external/workflows/run",
            headers=ballerine,
            json={"workflowId": "risk-score-improvement-dev", "context": {"entity": entity, "documents": []}},
        )
        assert review.status_code == 200, review.text
        opened = client.get(f"/api/v1/external/workflows/{review.json()['workflowRuntimeId']}", headers=ballerine)
        assert opened.json()["state"] == "analystReview"
        assert opened.json()["workflowDefinitionName"] == "risk-score-improvement"
        definitions = client.get("/api/v1/workflow-definition", headers=ballerine)
        risk = next(row for row in definitions.json() if row["name"] == "risk-score-improvement")
        linked = risk["definition"]["states"]
        assert risk["definition"]["initial"] == "screen"
        assert linked["screen"]["on"]["NEXT"] == "score"
        assert linked["score"]["on"]["NEXT"] == "analystReview"
        assert linked["analystReview"]["on"]["approve"] == "checkerReview"
        assert linked["checkerReview"]["on"]["approve"] == "approved"
        assert linked["checkerReview"]["on"]["return"] == "analystReview"
        assert linked["remediate"]["on"]["NEXT"] == "screen"
        assert "criblage" not in linked
        assert "manual_review" not in linked
        case = client.get(f"/api/clarity/v1/desks/kyc/{review.json()['workflowRuntimeId']}", headers=analyst)
        assert case.status_code == 200, case.text
        steps = case.json()["steps"]
        assert [step["key"] for step in steps] == ["screen", "score", "analystReview", "checkerReview", "approved"]
        assert next(step for step in steps if step["status"] == "current")["key"] == "analystReview"
        assert steps[0]["status"] == "done"
        assert steps[1]["status"] == "done"
        prospect = {
            "object_id": "prospect:kyc-api-user",
            "updated_at": "2026-10-25T21:00:00Z",
            "full_name": "Sara Lopez",
            "first_name": "Sara",
            "last_name": "Lopez",
            "kyc_status": "invalid",
            "pep_public_function": False,
            "source_system": "kyc",
        }
        ingested = client.post("/v1/ingest/prospects", headers=marble, json=prospect)
        assert ingested.status_code == 201, ingested.text
        client_row = client.post(
            "/v1/ingest/clients",
            headers=marble,
            json={"object_id": "client:kyc-api-user", "updated_at": "2026-10-25T21:00:00Z", "full_name": "Sara Lopez", "customer_type": "person", "customer_status": "active"},
        )
        assert client_row.status_code == 201, client_row.text
        counterparty = client.post(
            "/v1/ingest/counterparties",
            headers=marble,
            json={"object_id": "beneficiary:1", "updated_at": "2026-10-25T21:00:00Z", "name": "Sara Lopez"},
        )
        assert counterparty.status_code == 201, counterparty.text


def test_kyc_cycles_expire_and_can_be_reminded(tmp_path):
    with _client(tmp_path) as client:
        headers = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        listed = client.get("/api/clarity/v1/customers", headers=headers, params={"kind": "individual", "limit": 50})
        assert listed.status_code == 200, listed.text
        rows = {row["external_ref"]: row for row in listed.json()["items"]}
        assert rows["cus-claire"]["kyc_status"] == "valid"
        assert rows["cus-claire"]["kyc_validated_at"]
        assert rows["cus-viktor"]["kyc_status"] == "expired"
        assert rows["cus-viktor"]["reminder"] == "automatic"
        assert rows["cus-noah"]["kyc_label"] == "pending · overdue"
        pending = client.get("/api/clarity/v1/customers", headers=headers, params={"kind": "individual", "status": "pending", "limit": 50})
        pending_refs = {row["external_ref"] for row in pending.json()["items"]}
        assert "cus-amina" in pending_refs
        assert "cus-noah" in pending_refs
        assert "cus-claire" not in pending_refs
        detail = client.get(f"/api/clarity/v1/customers/{rows['cus-viktor']['id']}", headers=headers)
        cycle = detail.json()["kyc_cycles"][0]
        assert cycle["created_at"] and cycle["expires_at"]
        reminded = client.post(f"/api/clarity/v1/customers/{rows['cus-viktor']['id']}/verifications/{cycle['id']}/remind", headers=headers)
        assert reminded.status_code == 200, reminded.text
        assert reminded.json()["reminder"] == "manual"
        claire = client.get(f"/api/clarity/v1/customers/{rows['cus-claire']['id']}", headers=headers)
        too_early = client.post(
            f"/api/clarity/v1/customers/{rows['cus-claire']['id']}/verifications/{claire.json()['kyc_cycles'][0]['id']}/remind",
            headers=headers,
        )
        assert too_early.status_code == 422


def test_model_service_routes_stay_off_clarity(tmp_path: Path):
    """kyc-api keeps photo, OCR, and video calls on its model service."""
    model_paths = {
        "/api/v1/kyc/verify",
        "/api/v1/kyc/ocr",
        "/api/v1/liveness/challenge",
        "/api/v1/liveness/verify",
        "/api/v1/liveness/detect",
        "/api/v1/health",
    }
    with _client(tmp_path) as client:
        served = set(client.app.openapi()["paths"])
        assert model_paths.isdisjoint(served)
        assert "/api/v1/external/workflows/run" in served
        assert "/api/v1/collection-flow/files" in served
        assert "/api/v1/collection-flow/sync/context" in served
        assert "/v1/ingest/{object_type}" in served
        assert "/v1/decisions/all" in served


def normalize_template(path: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", "{}", path)
