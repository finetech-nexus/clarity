import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree

import yaml
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.engine import PROCESS_CATALOG
from app.ids import (
    ADMIN_EMAIL,
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


def test_restart_restores_admin_login(tmp_path):
    with _client(tmp_path) as client:
        from sqlalchemy import select

        from app.ids import ADMIN_EMAIL
        from app.models import User
        from app.seed import seed_demo

        db = client.app.state.database.session_factory()
        admin = db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        admin.password_hash = "other$deadbeef"
        admin.active = False
        db.commit()
        db.close()
        refused = client.post("/api/clarity/v1/auth/login", json={"email": ADMIN_EMAIL, "password": DEMO_PASSWORD})
        assert refused.status_code == 401
        db = client.app.state.database.session_factory()
        seed_demo(db, client.app.state.engine, seed_clients=False)
        db.commit()
        db.close()
        restored = client.post("/api/clarity/v1/auth/login", json={"email": ADMIN_EMAIL, "password": DEMO_PASSWORD})
        assert restored.status_code == 200, restored.text
        assert restored.json()["user"]["role"] == "admin"


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
        assert profile.json()["verification_checks"] == []


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
        from app.models import IdentityDocument

        db = client.app.state.database.session_factory()
        customer = db.get(Customer, opened.json()["customer"]["id"])
        db.add(IdentityDocument(tenant_id=customer.tenant_id, customer_id=customer.id, workflow_id=workflow_id, doc_type="passport", decision="approved", properties={"title": "Passeport"}))
        db.commit()
        db.close()
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
        import httpx

        from app.config import current_settings

        sent = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            sent["url"] = url
            sent["body"] = json
            sent["signature"] = (headers or {}).get("x-hmac-signature")

            class Response:
                status_code = 200
                text = "ok"

            return Response()

        current_settings().core_banking_hook_url = "http://core-banking.test/api/kyc/onboarding"
        original = httpx.post
        httpx.post = fake_post
        try:
            approved = client.post(
                f"/api/clarity/v1/workflows/{workflow_id}/actions",
                headers={"Authorization": f"Bearer {checker}"},
                json={"action": "approve", "rationale": "Contrôle de second niveau favorable."},
            )
        finally:
            httpx.post = original
        assert approved.status_code == 200, approved.text
        assert approved.json()["state"] == "approved"
        from app.models import WorkflowInstance

        db = client.app.state.database.session_factory()
        stored = db.get(WorkflowInstance, workflow_id)
        hook = stored.context["account_hook"]
        db.close()
        assert hook["delivered"] is True
        assert hook["payload"]["eventName"] == "workflow.completed"
        assert hook["payload"]["workflowFinalState"] == "approved"
        assert hook["payload"]["data"]["entity"]["data"]["firstName"] == "Luc"
        assert hook["payload"]["data"]["documents"][0]["category"] == "proof_of_identity"
        assert sent["url"] == "http://core-banking.test/api/kyc/onboarding"
        assert sent["signature"]

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
        page = client.get("/api/clarity/v1/audit", headers={"Authorization": f"Bearer {investigator}"}, params={"limit": 2, "offset": 0, "action": "report"})
        assert page.status_code == 200, page.text
        assert page.json()["total"] >= 1
        assert len(page.json()["items"]) <= 2
        assert all("report" in item["action"] for item in page.json()["items"])


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
        identity = body["identity"]
        assert [field["key"] for field in identity].count("firstName") == 1
        assert [field["key"] for field in identity].count("lastName") == 1
        assert [field["key"] for field in identity].count("dateOfBirth") == 1
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


def test_agent_can_ask_for_a_document_and_the_customer_sends_it(tmp_path):
    with _client(tmp_path) as client:
        analyst = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        ballerine = {"Authorization": f"Bearer {BALLERINE_API_KEY}"}
        queue = client.get("/api/clarity/v1/desks/kyc", headers=analyst)
        assert queue.status_code == 200, queue.text
        amina = next(row for row in queue.json() if row["name"] == "Amina El Fassi")
        for _ in range(4):
            if amina["state"] == "analystReview":
                break
            stepped = client.post(f"/api/v1/external/workflows/{amina['id']}/event", headers=ballerine, json={"name": "USER_NEXT_STEP"})
            assert stepped.status_code == 200, stepped.text
            amina = client.get(f"/api/clarity/v1/desks/kyc/{amina['id']}", headers=analyst).json()["case"]
        assert amina["state"] == "analystReview"
        detail = client.get(f"/api/clarity/v1/desks/kyc/{amina['id']}", headers=analyst)
        passport = next(item for item in detail.json()["documents"] if item["type"] == "passport")
        definitions = client.get("/api/v1/workflow-definition", headers=ballerine)
        kyc = next(row for row in definitions.json() if row["name"] == "kyc")
        saved = client.put(
            f"/api/v1/workflow-definition/{kyc['id']}/documents-schema",
            headers=ballerine,
            json=[{
                "category": "proof_of_address",
                "type": "proof_of_address",
                "issuer": {"country": "FR"},
                "required": True,
                "i18n": {"label": {"fr": "Justificatif de domicile", "en": "Proof of address", "ar": "إثبات العنوان"}},
            }],
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["documentsSchema"][0]["i18n"]["label"]["en"] == "Proof of address"
        asked = client.post(
            f"/api/clarity/v1/desks/kyc/{amina['id']}/document-requests",
            headers=analyst,
            json={
                "reason": "Photo illisible",
                "items": [
                    {"document_id": passport["id"]},
                    {"type": "proof_of_address", "category": "proof_of_address", "country": "FR"},
                ],
            },
        )
        assert asked.status_code == 200, asked.text
        body = asked.json()
        assert body["case"]["state"] == "documentsRequested"
        labels = {item["type"]: item["label"] for item in body["requested_documents"]}
        assert labels["passport"]["ar"] == "جواز السفر"
        assert labels["proof_of_address"]["fr"] == "Justificatif de domicile"
        runtime = client.get(f"/api/v1/external/workflows/{amina['id']}", headers=ballerine)
        assert runtime.json()["state"] == "documentsRequested"
        for item in runtime.json()["context"]["documentsRequested"]:
            uploaded = client.post(
                f"/api/v1/external/documents/{amina['id']}/{item['id']}",
                headers=ballerine,
                files={"file": ("piece.jpg", b"\xff\xd8\xff piece", "image/jpeg")},
            )
            assert uploaded.status_code == 200, uploaded.text
        again = client.get(f"/api/v1/external/workflows/{amina['id']}", headers=ballerine)
        assert again.json()["state"] == "analystReview"
        assert again.json()["context"]["documentsRequested"] == []
        video = client.post(
            f"/api/v1/external/workflows/{amina['id']}/liveness",
            headers=ballerine,
            files={"file": ("liveness.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")},
        )
        assert video.status_code == 200, video.text
        assert video.json()["type"] == "liveness_video"
        unknown = client.post(
            f"/api/clarity/v1/desks/kyc/{amina['id']}/document-requests",
            headers=analyst,
            json={"items": [{"type": "passport", "category": "identity"}]},
        )
        assert unknown.status_code == 422, unknown.text


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
    line_two = "12AB345673FRA8804189F3001019<<<<<<<<<<<<<<08"
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
    assert card["parsedData"]["address"] == "12 rue de la Paix, 75002 Paris"
    joined = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg">
        <text>Adresse: 93110 RASNY-SOUS-B015, Carte valabie jusqu'au : 27 11. 2034, délivrée le: 28.11.2019</text>
        </svg>""".encode()
    )
    assert joined["parsedData"]["address"] == "93110 RASNY-SOUS-B015"
    assert joined["parsedData"]["expiry"] == "2034-11-27"
    split_card = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg">
        <text>Adresse</text><text>93110 RASNY-SOUS-B015</text>
        <text>Carte valabie jusqu'au : 27 11. 2034</text>
        <text>délivrée le: 28.11.2019</text>
        </svg>""".encode()
    )
    assert split_card["parsedData"]["address"] == "93110 RASNY-SOUS-B015"
    assert split_card["parsedData"]["expiry"] == "2034-11-27"
    bilingual = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg">
        <text>Nom / Surname</text><text>MILADI</text>
        <text>Prénoms / Given names</text><text>Mohamed Amine</text>
        <text>Nationalité / Nationality</text><text>Française</text>
        <text>Date de naissance / Date of birth</text><text>02 06 1983</text>
        </svg>""".encode()
    )
    assert bilingual["parsedData"]["firstName"] == "Mohamed Amine"
    assert bilingual["parsedData"]["lastName"] == "MILADI"
    assert bilingual["parsedData"]["dateOfBirth"] == "1983-06-02"
    assert bilingual["parsedData"]["nationality"] == "FRA"
    noise = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg"><text>Nationatite Francaie</text></svg>""".encode()
    )
    assert noise["parsedData"]["firstName"] == ""
    assert noise["parsedData"]["lastName"] == ""
    card_mrz = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg">
        <text>IDFRAX4RTBPFW46&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;</text>
        <text>8306029M3106017FRA&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;0</text>
        <text>MILADI&lt;&lt;MOHAMED&lt;AMINE&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;</text>
        </svg>""".encode()
    )
    assert card_mrz["parsedData"]["firstName"] == "Mohamed Amine"
    assert card_mrz["parsedData"]["lastName"] == "Miladi"
    assert card_mrz["parsedData"]["dateOfBirth"] == "1983-06-02"
    assert card_mrz["parsedData"]["nationality"] == "FRA"
    assert card_mrz["parsedData"]["documentNumber"] == "X4RTBPFW4"
    assert card_mrz["mrz"] is True
    garbage = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg">
        <text>Prénoms</text><text>PARA</text>
        <text>Nom</text><text>RM RARES Cd</text>
        <text>0002029M2501019FRA&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;</text>
        </svg>""".encode()
    )
    assert garbage["parsedData"]["firstName"] == ""
    assert garbage["parsedData"]["lastName"] == ""
    assert garbage["parsedData"]["dateOfBirth"] == ""
    assert garbage["mrz"] is False
    french = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg">
        <text>IDFRADUPONT&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;000000</text>
        <text>1234567890122MARIE&lt;LOUISE&lt;&lt;8501019F2</text>
        <text>Carte valable jusqu'au : 01.01.2030</text>
        </svg>""".encode()
    )
    assert french["mrz"] is True
    assert french["parsedData"]["firstName"] == "Marie Louise"
    assert french["parsedData"]["lastName"] == "Dupont"
    assert french["parsedData"]["dateOfBirth"] == "1985-01-01"
    assert french["parsedData"]["nationality"] == "FRA"
    assert french["parsedData"]["documentNumber"] == "123456789012"
    assert french["parsedData"]["expiry"] == "2030-01-01"
    assert french["parsedData"]["gender"] == "F"
    broken = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg">
        <text>IDFRADUPONT&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;000000</text>
        <text>1234567890120MARIE&lt;LOUISE&lt;&lt;0002029F2</text>
        </svg>""".encode()
    )
    assert broken["mrz"] is False
    assert broken["parsedData"]["firstName"] == ""
    assert broken["parsedData"]["lastName"] == ""
    assert broken["parsedData"]["dateOfBirth"] == ""
    repaired = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg">
        <text>IDFRADUPONT&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;&lt;000000</text>
        <text>1234567890122M0HAMED&lt;&lt;AMINE8306029M2</text>
        </svg>""".encode()
    )
    assert repaired["mrz"] is True
    assert repaired["parsedData"]["firstName"] == "Mohamed Amine"
    assert repaired["parsedData"]["lastName"] == "Dupont"
    assert repaired["parsedData"]["dateOfBirth"] == "1983-06-02"


def test_rapid_lines_accept_array_results():
    from app.ocr import _rapid_lines

    class Rows(list):
        def __bool__(self):
            raise ValueError("ambiguous")

    class Page:
        txts = Rows(["02 06 1983", "MILADI"])
        scores = Rows([0.9, 0.8])
        boxes = Rows([[[0, 40], [1, 40]], [[0, 10], [1, 10]]])

    assert _rapid_lines(Page()) == "MILADI\n02 06 1983"


def test_paddle_lines_follow_the_page_from_top_to_bottom():
    from app.ocr import _paddle_lines

    class Page:
        rec_texts = ["02 06 1983", "MILADI", "Mohamed Amine"]
        dt_polys = [[[0, 80], [10, 80]], [[0, 10], [10, 10]], [[0, 40], [10, 40]]]

    assert _paddle_lines([Page()]) == "MILADI\nMohamed Amine\n02 06 1983"


def test_paddleocr_text_is_parsed_and_noise_is_rejected(monkeypatch):
    from app import ocr

    photo = b"\xff\xd8\xff" + b"photo"

    def recognized(_content: bytes):
        return "Nom\nMILADI\nPrenoms\nMohamed Amine\nDate de naissance\n02 06 1983", {"nationality": "Française"}, True

    monkeypatch.setattr(ocr, "_paddle_read", recognized)
    read = ocr.read_identity(photo)
    assert read["engine"] == "paddleocr"
    assert read["production"] is False
    assert read["parsedData"]["firstName"] == "Mohamed Amine"
    assert read["parsedData"]["lastName"] == "MILADI"
    assert read["parsedData"]["dateOfBirth"] == "1983-06-02"
    assert read["parsedData"]["nationality"] == "FRA"

    def noise(_content: bytes):
        return "PARA RM RARES Cd", {"full_name": "PARA RM RARES Cd", "date_of_birth": "000202"}, True

    monkeypatch.setattr(ocr, "_paddle_read", noise)
    rejected = ocr.read_identity(photo)
    assert rejected["engine"] == "paddleocr"
    assert rejected["parsedData"]["firstName"] == ""
    assert rejected["parsedData"]["lastName"] == ""
    assert rejected["parsedData"]["dateOfBirth"] == ""


def test_name_fragments_are_skipped_for_the_real_identity():
    from app.ocr import read_identity

    card = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg">
        <text>Nom</text><text>rrret</text><text>MILADI</text>
        <text>Prenoms</text><text>Cite, e</text><text>Mohamed Amine</text>
        <text>Date de naissance</text><text>02 06 1983</text>
        </svg>""".encode()
    )
    assert card["parsedData"]["firstName"] == "Mohamed Amine"
    assert card["parsedData"]["lastName"] == "MILADI"
    assert card["parsedData"]["dateOfBirth"] == "1983-06-02"
    noise = read_identity(
        """<svg xmlns="http://www.w3.org/2000/svg"><text>Prenoms</text><text>Cite, e</text><text>Nom</text><text>rrret</text></svg>""".encode()
    )
    assert noise["parsedData"]["firstName"] == ""
    assert noise["parsedData"]["lastName"] == ""


def test_scenario_editor_and_document_types(tmp_path):
    with _client(tmp_path) as client:
        headers = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        scenarios = client.get("/scenarios", headers=headers)
        assert scenarios.status_code == 200, scenarios.text
        scenario_id = scenarios.json()[0]["id"]
        translated = client.patch(
            f"/scenarios/{scenario_id}",
            headers=headers,
            json={"i18n": {"name": {"fr": "Seuil", "en": "Threshold", "ar": "حد"}, "description": {"fr": "Surveillance", "en": "Watch", "ar": ""}}},
        )
        assert translated.status_code == 200, translated.text
        assert translated.json()["name"] == "Seuil"
        assert translated.json()["i18n"]["name"]["en"] == "Threshold"
        assert translated.json()["i18n"]["name"]["ar"] == "حد"
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
        refused = client.patch("/api/clarity/v1/integrations/sanctions", headers=headers, json={"provider": "complyadvantage", "api_key": secret})
        assert refused.status_code == 422, refused.text
        saved = client.patch("/api/clarity/v1/integrations/sanctions", headers=headers, json={"api_key": secret})
        assert saved.status_code == 200, saved.text
        assert saved.json()["has_secret"] is True
        assert saved.json()["provider"] == "opensanctions"
        assert secret not in saved.text
        listed = client.get("/api/clarity/v1/integrations", headers=headers)
        assert secret not in listed.text
        assert {row["kind"] for row in listed.json()} == {"sanctions", "identity"}
        providers = client.get("/api/clarity/v1/integrations/providers", headers=headers)
        assert {row["id"] for row in providers.json()} == {"opensanctions", "veriff"}
        with patch("app.integrations_store.httpx.get", return_value=Mock(status_code=200)):
            checked = client.post("/api/clarity/v1/integrations/sanctions/check", headers=headers)
        assert checked.status_code == 200, checked.text
        assert checked.json()["connection_status"] == "connected"
        assert secret not in checked.text
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
        assert review.json()["workflowRuntimeId"] == runtime_id
        desk = client.get("/api/clarity/v1/desks/kyc", headers=analyst)
        assert len([row for row in desk.json() if row["name"] == "Sara Lopez"]) == 1
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
        assert isinstance(case.json()["customer"]["risk"]["score"], int)
        profile = client.get(f"/api/clarity/v1/customers/{case.json()['customer']['id']}", headers=analyst)
        cycle = profile.json()["kyc_cycles"][0]
        assert cycle["workflow_runtime_id"] == runtime_id
        assert cycle["created_at"]
        assert "expires_at" in cycle
        assert cycle["documents"]
        assert cycle["documents"][0]["url"].endswith("/content")
        blocked = client.post(
            f"/api/clarity/v1/desks/kyc/{runtime_id}/decision",
            headers=analyst,
            json={"action": "approve", "rationale": "trop tôt"},
        )
        assert blocked.status_code == 422, blocked.text
        assert "pièces" in blocked.json()["message"]
        for document in case.json()["documents"]:
            decided = client.post(
                f"/api/clarity/v1/desks/documents/{document['id']}/decision",
                headers=analyst,
                json={"decision": "approve"},
            )
            assert decided.status_code == 200, decided.text
        from app.models import ScreeningCheck, ScreeningMatch

        db = client.app.state.database.session_factory()
        customer = db.get(Customer, case.json()["customer"]["id"])
        screening = ScreeningCheck(tenant_id=customer.tenant_id, customer_id=customer.id, status="in_review", query={}, match_count=1, partial=False, production=False)
        db.add(screening)
        db.flush()
        match = ScreeningMatch(
            tenant_id=customer.tenant_id,
            screening_id=screening.id,
            entity_id="candidate-1",
            status="pending",
            score=90,
            explanation=["candidate"],
            payload={"caption": "Candidate", "synthetic": True},
            query_ids=[],
        )
        db.add(match)
        db.commit()
        match_id = match.id
        db.close()
        waiting = client.post(
            f"/api/clarity/v1/desks/kyc/{runtime_id}/decision",
            headers=analyst,
            json={"action": "approve", "rationale": "criblage ouvert"},
        )
        assert waiting.status_code == 422, waiting.text
        assert "criblage" in waiting.json()["message"]
        cleared = client.post(
            f"/api/clarity/v1/screening/matches/{match_id}/disposition",
            headers=analyst,
            json={"status": "no_hit"},
        )
        assert cleared.status_code == 200, cleared.text
        moved = client.post(
            f"/api/clarity/v1/desks/kyc/{runtime_id}/decision",
            headers=analyst,
            json={"action": "approve", "rationale": "pièces et criblage vérifiés"},
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["case"]["state"] == "checkerReview"
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
        assert rows["cus-viktor"]["kyc_status"] == "pending"
        assert rows["cus-noah"]["kyc_status"] == "pending"
        pending = client.get("/api/clarity/v1/customers", headers=headers, params={"kind": "individual", "status": "pending", "limit": 50})
        pending_refs = {row["external_ref"] for row in pending.json()["items"]}
        assert "cus-amina" in pending_refs
        assert "cus-noah" in pending_refs
        assert "cus-claire" not in pending_refs
        detail = client.get(f"/api/clarity/v1/customers/{rows['cus-viktor']['id']}", headers=headers)
        cycles = detail.json()["kyc_cycles"]
        cycle = next(item for item in cycles if item["status"] == "expired")
        assert any(item["status"] == "pending" for item in cycles)
        assert cycle["created_at"] and cycle["expires_at"]
        assert cycle["reminder"] == "automatic"
        noah = client.get(f"/api/clarity/v1/customers/{rows['cus-noah']['id']}", headers=headers)
        assert any(item["status"] == "overdue" for item in noah.json()["kyc_cycles"])
        reminded = client.post(f"/api/clarity/v1/customers/{rows['cus-viktor']['id']}/verifications/{cycle['id']}/remind", headers=headers)
        assert reminded.status_code == 200, reminded.text
        assert reminded.json()["reminder"] == "manual"
        claire = client.get(f"/api/clarity/v1/customers/{rows['cus-claire']['id']}", headers=headers)
        too_early = client.post(
            f"/api/clarity/v1/customers/{rows['cus-claire']['id']}/verifications/{claire.json()['kyc_cycles'][0]['id']}/remind",
            headers=headers,
        )
        assert too_early.status_code == 422
        claire_cycle = claire.json()["kyc_cycles"][0]
        piece = claire_cycle["documents"][0]
        requested = client.post(
            f"/api/clarity/v1/customers/{rows['cus-claire']['id']}/verifications/{claire_cycle['id']}/remind",
            headers=headers,
            json={"document_ids": [piece["id"]]},
        )
        assert requested.status_code == 200, requested.text
        refreshed = client.get(f"/api/clarity/v1/customers/{rows['cus-claire']['id']}", headers=headers)
        stored = next(item for row in refreshed.json()["kyc_cycles"] for item in row["documents"] if item["id"] == piece["id"])
        assert stored["decision"] == "revisions"


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


def test_one_verification_per_client_until_expiry(tmp_path):
    from app.models import KycCycle

    entity = {
        "type": "individual",
        "id": "phone-client-1",
        "data": {"firstName": "Test", "lastName": "Sept", "dateOfBirth": "2000-02-02"},
    }
    with _client(tmp_path) as client:
        headers = {"Authorization": f"Bearer {BALLERINE_API_KEY}"}
        first = client.post(
            "/api/v1/external/workflows/run",
            headers=headers,
            json={"workflowId": "kyc_collection_flow", "context": {"entity": entity, "documents": []}},
        )
        assert first.status_code == 200, first.text
        updated = {
            **entity,
            "data": {**entity["data"], "firstName": "Mohamed", "lastName": "Amine"},
        }
        second = client.post(
            "/api/v1/external/workflows/run",
            headers=headers,
            json={"workflowId": "kyc_collection_flow", "context": {"entity": updated, "documents": []}},
        )
        assert second.status_code == 200, second.text
        assert second.json()["workflowRuntimeId"] == first.json()["workflowRuntimeId"]
        customer_id = first.json()["ballerineEntityId"]
        listed = client.get("/api/v1/external/workflows", headers=headers)
        same = [row for row in listed.json()["results"] if row.get("endUserId") == customer_id]
        assert len(same) == 1
        analyst = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        profile = client.get(f"/api/clarity/v1/customers/{customer_id}", headers=analyst)
        assert profile.json()["display_name"] == "Mohamed Amine"

        from app.models import WorkflowInstance

        session = client.app.state.database.session_factory()
        cycle = session.query(KycCycle).filter(KycCycle.customer_id == customer_id).one()
        cycle.status = "valid"
        cycle.validated_at = utcnow()
        cycle.expires_at = utcnow() + timedelta(days=300)
        workflow = session.query(WorkflowInstance).filter(WorkflowInstance.customer_id == customer_id).one()
        workflow.status = "completed"
        workflow.state = "approved"
        session.commit()
        session.close()
        blocked = client.post(
            "/api/v1/external/workflows/run",
            headers=headers,
            json={"workflowId": "kyc_collection_flow", "context": {"entity": updated, "documents": []}},
        )
        assert blocked.status_code == 409, blocked.text

        session = client.app.state.database.session_factory()
        cycle = session.query(KycCycle).filter(KycCycle.customer_id == customer_id, KycCycle.status == "valid").one()
        cycle.expires_at = utcnow() - timedelta(days=1)
        session.commit()
        session.close()
        renewed = client.post(
            "/api/v1/external/workflows/run",
            headers=headers,
            json={"workflowId": "kyc_collection_flow", "context": {"entity": updated, "documents": []}},
        )
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["workflowRuntimeId"] != first.json()["workflowRuntimeId"]
        assert renewed.json()["ballerineEntityId"] == customer_id
        listed = client.get("/api/v1/external/workflows", headers=headers)
        same = [row for row in listed.json()["results"] if row.get("endUserId") == customer_id]
        assert len(same) == 2


def test_kyc_validity_defaults_to_one_year(tmp_path):
    with _client(tmp_path) as client:
        admin = {"Authorization": f"Bearer {_login(client, ADMIN_EMAIL)}"}
        current = client.get("/api/clarity/v1/administration/kyc", headers=admin)
        assert current.status_code == 200, current.text
        assert current.json()["kyc_validity_years"] == 1
        saved = client.patch("/api/clarity/v1/administration/kyc", headers=admin, json={"kyc_validity_years": 2})
        assert saved.status_code == 200, saved.text
        assert saved.json()["kyc_validity_years"] == 2
        analyst = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        denied = client.patch("/api/clarity/v1/administration/kyc", headers=analyst, json={"kyc_validity_years": 3})
        assert denied.status_code == 403


def test_admin_can_purge_clients_and_verifications(tmp_path):
    with _client(tmp_path) as client:
        analyst = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        denied = client.post("/api/clarity/v1/administration/purge-clients", headers=analyst)
        assert denied.status_code == 403
        before = client.get("/api/clarity/v1/customers", headers=analyst)
        assert before.json()
        admin = {"Authorization": f"Bearer {_login(client, ADMIN_EMAIL)}"}
        purged = client.post("/api/clarity/v1/administration/purge-clients", headers=admin)
        assert purged.status_code == 200, purged.text
        assert purged.json()["customers"] > 0
        assert client.get("/api/clarity/v1/customers", headers=analyst).json() == []
        assert client.get("/api/clarity/v1/desks/kyc", headers=analyst).json() == []
        assert client.get("/api/clarity/v1/administration/users", headers=admin).json()


def test_oidc_user_stays_pending_until_an_admin_assigns_a_role(tmp_path):
    from datetime import datetime, timedelta, timezone
    from urllib.parse import parse_qs, urlparse

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    import app.oidc as oidc

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'oidc.db'}",
        seed_demo=True,
        env="test",
        flowable_url="",
        upload_dir=str(tmp_path / "uploads"),
        jwt_secret="test-secret-test-secret-test-secret",
        public_app_url="http://clarity.test",
        oidc_issuer="https://idp.test/realms/internal",
        oidc_client_id="clarity",
        admin_username="admin",
        admin_password="password",
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    holder: dict[str, str] = {}

    def fake_discover(issuer: str) -> dict:
        return {
            "issuer": issuer,
            "authorization_endpoint": "https://idp.test/auth",
            "token_endpoint": "https://idp.test/token",
            "jwks_uri": "https://idp.test/certs",
        }

    class FakeJwk:
        def __init__(self, uri: str, **_kwargs):
            self.uri = uri

        def get_signing_key_from_jwt(self, token: str):
            public = key.public_key()

            class Signing:
                pass

            Signing.key = public
            return Signing()

    def fake_post(url, data=None, timeout=None, **_kwargs):
        pending = jwt.decode(holder["state"], settings.jwt_secret, algorithms=["HS256"])
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "iss": settings.oidc_issuer,
                "aud": settings.oidc_client_id,
                "nonce": pending["nonce"],
                "email": "Nina.Morel@bank.test",
                "name": "Nina Morel",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            key,
            algorithm="RS256",
        )
        assert data["code_verifier"] == pending["verifier"]

        class Response:
            status_code = 200

            def json(self):
                return {"id_token": token}

        return Response()

    original = (oidc.discover, oidc.PyJWKClient, oidc.httpx.post)
    oidc.discover = fake_discover
    oidc.PyJWKClient = FakeJwk
    oidc.httpx.post = fake_post
    try:
        with TestClient(create_app(settings)) as client:
            assert client.get("/api/clarity/v1/auth/oidc").json()["enabled"] is True
            started = client.get("/api/clarity/v1/auth/oidc/start", follow_redirects=False)
            assert started.status_code == 302, started.text
            holder["state"] = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
            assert "code_challenge_method=S256" in started.headers["location"]
            callback = client.get(
                "/api/clarity/v1/auth/oidc/callback",
                params={"code": "one-time-code", "state": holder["state"]},
                follow_redirects=False,
            )
            assert callback.status_code == 302, callback.text
            session = callback.headers["location"].split("#session=", 1)[1]
            headers = {"Authorization": f"Bearer {session}"}
            me = client.get("/api/clarity/v1/me", headers=headers)
            assert me.status_code == 200, me.text
            assert me.json()["email"] == "nina.morel@bank.test"
            assert me.json()["role"] == "pending"
            assert client.get("/api/clarity/v1/customers", headers=headers).status_code == 403
            admin = client.post("/api/clarity/v1/auth/login", json={"email": "admin", "password": "password"})
            assert admin.status_code == 200, admin.text
            admin_headers = {"Authorization": f"Bearer {admin.json()['access_token']}"}
            listed = client.get("/api/clarity/v1/administration/users", headers=admin_headers)
            nina = next(row for row in listed.json() if row["email"] == "nina.morel@bank.test")
            bootstrap = next(row for row in listed.json() if row["email"] == "admin")
            refused = client.patch(
                f"/api/clarity/v1/administration/users/{bootstrap['id']}",
                headers=admin_headers,
                json={"role": "analyst"},
            )
            assert refused.status_code == 422
            assigned = client.patch(
                f"/api/clarity/v1/administration/users/{nina['id']}",
                headers=admin_headers,
                json={"role": "analyst"},
            )
            assert assigned.status_code == 200, assigned.text
            assert client.get("/api/clarity/v1/customers", headers=headers).status_code == 200
    finally:
        oidc.discover, oidc.PyJWKClient, oidc.httpx.post = original


def test_keycloak_mifos_is_admin_and_can_give_several_roles(tmp_path):
    from datetime import datetime, timedelta, timezone

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    import app.oidc as oidc

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'kc.db'}",
        seed_demo=True,
        env="test",
        flowable_url="",
        upload_dir=str(tmp_path / "uploads"),
        jwt_secret="test-secret-test-secret-test-secret",
        oidc_issuer="https://idp.test/realms/internal",
        oidc_client_id="web-app",
        oidc_admins="mifos",
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": settings.oidc_issuer,
            "azp": "web-app",
            "preferred_username": "mifos",
            "given_name": "Mi",
            "family_name": "Fos",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        key,
        algorithm="RS256",
    )

    class FakeJwk:
        def __init__(self, uri: str, **_kwargs):
            self.uri = uri

        def get_signing_key_from_jwt(self, raw: str):
            public = key.public_key()

            class Signing:
                pass

            Signing.key = public
            return Signing()

    original = oidc.PyJWKClient
    oidc.PyJWKClient = FakeJwk
    try:
        with TestClient(create_app(settings)) as client:
            headers = {"Authorization": f"Bearer {token}"}
            me = client.get("/api/clarity/v1/me", headers=headers)
            assert me.status_code == 200, me.text
            assert "admin" in me.json()["roles"]
            assert client.get("/api/v1/workflow-definition", headers=headers).status_code == 200
            assert client.get("/scenarios", headers=headers).status_code == 200
            assert client.get("/data-model", headers=headers).status_code == 200
            assert client.get("/api/clarity/v1/customers", headers=headers).status_code == 200
            created = client.post(
                "/api/clarity/v1/administration/users",
                headers=headers,
                json={"given_name": "Nora", "family_name": "Diallo", "email": "Nora.Diallo@bank.test", "roles": ["analyst", "checker"]},
            )
            assert created.status_code == 201, created.text
            assert created.json()["email"] == "nora.diallo@bank.test"
            assert set(created.json()["roles"]) == {"analyst", "checker"}
            listed = client.get("/api/clarity/v1/administration/users", headers=headers)
            mifos = next(row for row in listed.json() if row["email"] == "mifos")
            refused = client.patch(
                f"/api/clarity/v1/administration/users/{mifos['id']}",
                headers=headers,
                json={"roles": ["analyst"]},
            )
            assert refused.status_code == 422
    finally:
        oidc.PyJWKClient = original


def test_password_auth_mode_uses_the_env_admin(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/api/clarity/v1/auth/mode").json() == {"mode": "oidc"}
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'password-auth.db'}",
        seed_demo=True,
        env="test",
        flowable_url="",
        upload_dir=str(tmp_path / "uploads"),
        auth_mode="password",
        admin_username="desk-admin",
        admin_password="desk-secret",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/clarity/v1/auth/mode").json() == {"mode": "password"}
        refused = client.post("/api/clarity/v1/auth/login", json={"email": "desk-admin", "password": "wrong"})
        assert refused.status_code == 401
        logged = client.post("/api/clarity/v1/auth/login", json={"email": "desk-admin", "password": "desk-secret"})
        assert logged.status_code == 200, logged.text
        assert logged.json()["user"]["role"] == "admin"
        assert "desk-secret" not in logged.text
        headers = {"Authorization": f"Bearer {logged.json()['access_token']}"}
        created = client.post(
            "/api/clarity/v1/administration/users",
            headers=headers,
            json={"given_name": "Lea", "family_name": "Martin", "email": "lea.martin@bank.test", "roles": ["analyst"], "password": "analyst-secret"},
        )
        assert created.status_code == 201, created.text
        assert "analyst-secret" not in created.text
        member = client.post("/api/clarity/v1/auth/login", json={"email": "lea.martin@bank.test", "password": "analyst-secret"})
        assert member.status_code == 200, member.text
        assert member.json()["user"]["role"] == "analyst"
        missing = client.post(
            "/api/clarity/v1/administration/users",
            headers=headers,
            json={"given_name": "No", "family_name": "Password", "email": "no.password@bank.test", "roles": ["analyst"]},
        )
        assert missing.status_code == 422
        garbage = client.get("/api/clarity/v1/me", headers={"Authorization": "Bearer not-a-jwt"})
        assert garbage.status_code == 401
        assert "Keycloak" not in garbage.text


def test_table_columns_can_be_added_and_edited(tmp_path):
    with _client(tmp_path) as client:
        headers = {"X-API-KEY": MARBLE_API_KEY}
        created = client.post(
            "/data-model/tables",
            headers=headers,
            json={"name": "cards", "semantic_type": "transactions", "description": "Cartes"},
        )
        assert created.status_code == 200, created.text
        table = created.json()
        names = {field["name"] for field in table["fields"].values()}
        assert {"object_id", "updated_at"} <= names
        added = client.post(
            f"/data-model/tables/{table['id']}/fields",
            headers=headers,
            json={"name": "pan_country", "data_type": "String", "nullable": True, "description": "Pays"},
        )
        assert added.status_code == 200, added.text
        edited = client.patch(
            f"/data-model/fields/{added.json()['id']}",
            headers=headers,
            json={"name": "issuer_country", "data_type": "String", "nullable": False, "description": "Pays émetteur"},
        )
        assert edited.status_code == 200, edited.text
        model = client.get("/data-model", headers=headers)
        field = model.json()["tables"]["cards"]["fields"]["issuer_country"]
        assert field["data_type"] == "String"
        assert field["nullable"] is False
        assert field["description"] == "Pays émetteur"
        assert "pan_country" not in model.json()["tables"]["cards"]["fields"]
        duplicate = client.post(
            f"/data-model/tables/{table['id']}/fields",
            headers=headers,
            json={"name": "issuer_country", "data_type": "String", "nullable": True},
        )
        assert duplicate.status_code == 422


def test_face_match_compares_the_selfie_with_the_identity_photo(tmp_path):
    from app.config import current_settings

    jpeg = b"\xff\xd8\xff\xd9"
    with _client(tmp_path) as client:
        current_settings().face_api_url = "http://ai.test"
        analyst = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        ballerine = {"Authorization": f"Bearer {BALLERINE_API_KEY}"}
        queue = client.get("/api/clarity/v1/desks/kyc", headers=analyst)
        amina = next(row for row in queue.json() if row["name"] == "Amina El Fassi")
        for doc_type, name in (("passport", "id.jpg"), ("selfie", "selfie.jpg")):
            uploaded = client.post(
                "/api/v1/external/documents",
                headers=ballerine,
                data={
                    "metadata": "{}",
                    "properties": "{}",
                    "category": "proof_of_identity",
                    "type": doc_type,
                    "issuingVersion": "1",
                    "issuingCountry": "FR",
                    "version": "1",
                    "status": "provided",
                    "workflowRuntimeDataId": amina["id"],
                },
                files={"file": (name, jpeg, "image/jpeg")},
            )
            assert uploaded.status_code == 200, uploaded.text

        class Response:
            status_code = 200

            def json(self):
                return {"face_match_score": 0.82, "face_verification_details": {"verified": True, "confidence": 0.82}}

        with patch("app.face.httpx.post", return_value=Response()) as called:
            matched = client.post(f"/api/clarity/v1/desks/kyc/{amina['id']}/face-match", headers=analyst)
        assert matched.status_code == 200, matched.text
        face = matched.json()["face_match"]
        assert face["score"] == 0.82
        assert face["match"] is True
        assert face["source"] == "selfie"
        assert called.call_args.args[0] == "http://ai.test/api/v1/kyc/verify"
        again = client.get(f"/api/clarity/v1/desks/kyc/{amina['id']}", headers=analyst)
        assert again.json()["face_match"]["match"] is True


def normalize_template(path: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", "{}", path)
