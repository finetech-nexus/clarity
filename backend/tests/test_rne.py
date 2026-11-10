from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.engine import PROCESS_CATALOG, definition_chart
from app.ids import ANALYST_EMAIL, DEMO_PASSWORD
from app.main import create_app
from app.rne import check_company, normalize_identifiant, prepare_business


def _login(client: TestClient, email: str) -> str:
    response = client.post("/api/clarity/v1/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]

RNE_FICHE = {
    "identifiant_UNIQUE": "1616343A",
    "denomination_FR": "ATELIER DU LAC",
    "nom_COMMERCIAL_FR": "ATELIER",
    "STATUS": "NORMAL",
    "type_ENTITE": "M",
    "forme_JURIDIQUE_FR": "SUARL",
    "rue_SIEGE_FR": "Rue du Lac",
    "ville_SIEGE_FR": "Tunis",
    "code_POSTAL_SIEGE": "1002",
    "gouvernorat_SIEGE_FR": "Tunis",
    "declaration_BENEFICIAIRES_EFFECTIFS": "oui",
    "capital_TND": 10000,
    "activites": {"primaryActivity": {"codeActivite": "47", "detailActiviteLatin": "Commerce de detail"}},
    "direction": [
        {
            "NOM_PRENOM_GESTION_FR": "AMINA BEN SALAH",
            "QALITE_GESTION_FR": "Gerant",
            "NATIONALITE_GESTION_FR": "Tunisienne",
            "DATE_NAISSANCE_GESTION": "1988-01-02",
            "pouvoirs": [{"LIBELLE_POUVOIR_FR": "Représentant légal"}],
        }
    ],
}

UBO_DECLARATION = [
    {
        "NP_PERSONNE_FR": "SAMI BEN SALAH",
        "NATIONALITE": "Tunisienne",
        "NID_PERSONNE": "01234567",
        "TYPE_BENFICAIRE": "Détention du Capital",
        "RESIDENT": "Résident",
    }
]


def _response(payload, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.text = ""
    response.json.return_value = payload
    return response


def test_identifiant_keeps_the_rne_shape():
    assert normalize_identifiant(" 1616343a ") == "1616343A"
    assert normalize_identifiant("1616343A/M/000") == "1616343A"
    assert check_company("123456789")["status"] == "invalid"


def test_registry_maps_fiche_directors_and_beneficial_owners():
    settings = Settings(
        rne_token="secret",
        rne_kyc_subscription_id="4",
        rne_kyc_subscriber_id="21",
        rne_ubo_subscription_id="4",
        rne_ubo_subscriber_id="21",
        rne_ubo_motif="1",
    )

    def fake_get(url, params, headers, timeout):
        assert headers["Authorization"] == "Bearer secret"
        assert params["identifiantUnique"] == "1616343A"
        if url.endswith("/WS-BE/1"):
            return _response(UBO_DECLARATION)
        return _response(RNE_FICHE)

    with patch("app.rne.httpx.get", side_effect=fake_get):
        report = check_company("1616343A", settings)
    assert report["status"] == "found"
    assert report["production"] is True
    assert report["fiche"]["legal_name"] == "ATELIER DU LAC"
    assert report["fiche"]["active"] is True
    assert report["directors"][0]["role"] == "representative"
    assert report["directors"][0]["nationality"] == "TN"
    assert report["beneficial_owners"][0]["full_name"] == "SAMI BEN SALAH"
    assert report["beneficial_owners"][0]["identity_number"] == "01234567"


def test_missing_subscription_does_not_invent_a_company():
    report = check_company("1616343A", Settings())
    assert report["status"] == "unavailable"
    assert report["fiche"] is None
    assert report["production"] is False


def test_kyb_process_checks_the_register_before_screening():
    chart = definition_chart("clarityKybBusiness")
    assert chart["initial"] == "collect"
    assert chart["states"]["registry"]["on"]["NEXT"] == "ubo"
    assert chart["states"]["ubo"]["on"]["NEXT"] == "verify"
    assert "registry" in PROCESS_CATALOG["clarityKybBusiness"]["tasks"]
    assert "ubo" in PROCESS_CATALOG["clarityKybBusiness"]["tasks"]


def test_business_onboarding_records_the_rne_check(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        seed_demo=True,
        env="test",
        flowable_url="",
        upload_dir=str(tmp_path / "uploads"),
        rne_token="secret",
        rne_kyc_subscription_id="4",
        rne_kyc_subscriber_id="21",
        rne_ubo_subscription_id="4",
        rne_ubo_subscriber_id="21",
    )
    app = create_app(settings)

    def fake_get(url, params, headers, timeout):
        if str(url).endswith("/WS-BE/1"):
            return _response(UBO_DECLARATION)
        return _response(RNE_FICHE)

    with TestClient(app) as client, patch("app.rne.httpx.get", side_effect=fake_get):
        analyst = {"Authorization": f"Bearer {_login(client, ANALYST_EMAIL)}"}
        opened = client.post(
            "/api/clarity/v1/onboarding/businesses",
            headers=analyst,
            json={
                "legal_name": "Nom declare",
                "registration_number": "1616343A",
                "identifiant_unique": "1616343A",
                "jurisdiction": "TN",
                "legal_form": "A confirmer",
                "registered_address": "A confirmer",
                "industry": "Commerce",
                "activities": "Commerce",
                "purpose": "Compte professionnel",
                "expected_activity": "Encaissements",
                "source_of_funds": "Activite",
                "external_ref": "atelier-lac",
                "beneficial_owners": [{"full_name": "Lina Trabelsi", "ownership_pct": 30, "nationality": "TN"}],
            },
        )
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert body["registry"]["status"] == "found"
        assert body["registry"]["name_mismatch"] is True
        assert body["customer"]["display_name"] == "ATELIER DU LAC"
        assert body["customer"]["business"]["legal_form"] == "SUARL"
        assert body["customer"]["business"]["registration_number"] == "1616343A"
        roles = {row["role"]: row["full_name"] for row in body["customer"]["parties"]}
        assert roles["representative"] == "AMINA BEN SALAH"
        assert roles["beneficial_owner"] in {"SAMI BEN SALAH", "Lina Trabelsi"}
        names = {row["full_name"] for row in body["customer"]["parties"] if row["role"] == "beneficial_owner"}
        assert names == {"SAMI BEN SALAH", "Lina Trabelsi"}
        assert body["state"] == "analystReview"
        queue = client.get("/api/clarity/v1/desks/kyc", headers=analyst, params={"kind": "business"})
        row = next(item for item in queue.json() if item["name"] == "ATELIER DU LAC")
        detail = client.get(f"/api/clarity/v1/desks/kyc/{row['id']}", headers=analyst)
        assert detail.status_code == 200, detail.text
        assert detail.json()["registry"]["fiche"]["legal_name"] == "ATELIER DU LAC"
        assert any(step["key"] == "registry" and step["status"] == "done" for step in detail.json()["steps"])
        assert any(step["key"] == "ubo" and step["status"] == "done" for step in detail.json()["steps"])


def test_prepare_business_skips_a_foreign_registration_number():
    prepared = prepare_business(
        {
            "legal_name": "Atelier Nord",
            "registration_number": "123456789",
            "directors": [],
            "beneficial_owners": [],
        }
    )
    assert prepared["legal_name"] == "Atelier Nord"
    assert prepared["registry"]["status"] == "invalid"
    assert prepared["registry"]["production"] is False
