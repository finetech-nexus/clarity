from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.ids import ANALYST_EMAIL, DEMO_PASSWORD
from app.main import create_app
from app.providers import match_opensanctions
from app.screening_sources import SCREENING_SOURCES, sources_for_hit


def _login(client: TestClient) -> dict[str, str]:
    response = client.post("/api/clarity/v1/auth/login", json={"email": ANALYST_EMAIL, "password": DEMO_PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_a_hit_is_kept_only_for_an_enabled_opensanctions_dataset():
    enabled = [row for row in SCREENING_SOURCES if row["id"] in {"tn_cnlct", "us_ofac_sdn", "ca_facfoa_tun"}]
    cnlct = {"id": "tn-1", "datasets": ["tn_cnlct", "sanctions"], "properties": {}}
    ofac = {"id": "us-1", "datasets": ["us_ofac_sdn"], "properties": {}}
    canada_tunisia = {"id": "ca-tn", "datasets": ["ca_facfoa"], "properties": {"programId": ["CA-FACFOA-TUN"]}}
    canada_other = {"id": "ca-other", "datasets": ["ca_facfoa"], "properties": {"programId": ["CA-FACFOA-UKR"]}}
    assert [row["id"] for row in sources_for_hit(cnlct, enabled)] == ["tn_cnlct"]
    assert [row["id"] for row in sources_for_hit(ofac, enabled)] == ["us_ofac_sdn"]
    assert [row["id"] for row in sources_for_hit(canada_tunisia, enabled)] == ["ca_facfoa_tun"]
    assert sources_for_hit(canada_other, enabled) == []
    assert sources_for_hit({"datasets": ["eu_fsf"], "properties": {}}, enabled) == []


def test_match_drops_hits_from_sources_that_are_off():
    body = {
        "responses": {
            "q": {
                "results": [
                    {"id": "keep", "caption": "Listed Person", "score": 0.91, "schema": "Person", "datasets": ["tn_cnlct"], "properties": {}},
                    {"id": "drop", "caption": "Other Person", "score": 0.95, "schema": "Person", "datasets": ["us_ofac_sdn"], "properties": {}},
                ]
            }
        }
    }
    settings = Settings(opensanctions_api_key="secret")
    sources = [row for row in SCREENING_SOURCES if row["id"] == "tn_cnlct"]
    with patch("app.providers._settings", return_value=settings), patch("app.providers.httpx.post", return_value=Mock(status_code=200, json=lambda: body, raise_for_status=lambda: None)):
        rows = match_opensanctions("Listed Person", "Person", None, "TN", sources)
    assert [row["entity_id"] for row in rows] == ["keep"]
    assert rows[0]["payload"]["sources"] == ["tn_cnlct"]


def test_third_party_tab_stores_each_source_flag(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        seed_demo=True,
        env="test",
        flowable_url="",
        upload_dir=str(tmp_path / "uploads"),
    )
    with TestClient(create_app(settings)) as client:
        headers = _login(client)
        current = client.get("/api/clarity/v1/integrations", headers=headers)
        sanctions = next(row for row in current.json() if row["kind"] == "sanctions")
        ids = {row["id"] for row in sanctions["sources"]}
        assert "opensanctions" in ids
        assert {"tn_arp", "bvmt", "ugtt", "gouvernorat_tunis", "agi", "tn_cnlct", "ca_facfoa_tun", "ctaf", "menafatf", "un_sc_sanctions", "us_ofac_sdn", "arab_league", "basel_aml_index", "fatf", "interpol_red_notices"} <= ids
        assert all(row["enabled"] is True for row in sanctions["sources"])
        saved = client.patch(
            "/api/clarity/v1/integrations/sanctions",
            headers=headers,
            json={"enabled": False, "sources": {"tn_cnlct": False, "us_ofac_sdn": True, "bvmt": False}},
        )
        assert saved.status_code == 200, saved.text
        flags = {row["id"]: row["enabled"] for row in saved.json()["sources"]}
        assert flags["opensanctions"] is False
        assert flags["tn_cnlct"] is False
        assert flags["us_ofac_sdn"] is True
        assert flags["bvmt"] is False
        assert flags["tn_arp"] is True
        assert "api_key" not in saved.text
