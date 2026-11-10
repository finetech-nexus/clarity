"""Adverse media: lexicon analysis, matching, collection and score. Fictional data only."""

import io
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.adverse_media import (
    NLP_ENGINE,
    AdverseMediaError,
    analyze_text,
    screening_contract,
    analyze_version,
    collect_source,
    content_hash,
    deep_search,
    http_get,
    run_batch,
    identity_assessment,
    ingest_article,
    name_similarity,
    process_queue,
    reanalyze,
    record_decision,
    retry_failed,
    screen_customer,
)
from app.ids import ADMIN_EMAIL, ANALYST_EMAIL, DEMO_PASSWORD, TENANT_ID
from app.models import (
    AdverseMediaAlert,
    AdverseMediaArticle,
    AdverseMediaArticleVersion,
    AdverseMediaSource,
    BusinessProfile,
    Customer,
    IndividualProfile,
    Party,
    Tenant,
)
from app.services import RISK_METHODOLOGY, assess_customer
from app.timeutil import utcnow
from tests.test_platform import _client

RSS = b"""<?xml version="1.0"?>
<rss><channel>
<item>
  <title>Zoubeida Mentest fait l'objet de poursuites pour fraude</title>
  <link>https://exemple.media/zoubeida-fraude</link>
  <description>Zoubeida Mentest fait l'objet de poursuites pour fraude fiscale a Tunis.</description>
</item>
</channel></rss>"""


def _session(client: TestClient):
    return client.app.state.database.session_factory()


def _person(db, name: str, dob: str | None = "1981-04-12") -> Customer:
    customer = Customer(tenant_id=TENANT_ID, kind="individual", display_name=name, external_ref=f"adv-{name}", canonical_status="approved")
    db.add(customer)
    db.flush()
    db.add(IndividualProfile(customer_id=customer.id, legal_name=name, given_name=name.split()[0], family_name=name.split()[-1], date_of_birth=dob, nationality="TN", residence_country="TN"))
    db.flush()
    return customer


def test_lexicon_distinguishes_stance_and_does_not_conclude_guilt():
    fraud = analyze_text("Zoubeida Mentest", "Zoubeida Mentest fait l'objet de poursuites pour fraude.")
    assert fraud["guilt_conclusion"] is False
    assert fraud["events"][0]["category"] == "fraud"
    assert fraud["events"][0]["stance"] == "prosecution"
    assert any(entity["name"] == "Zoubeida Mentest" for entity in fraud["entities"])

    english = analyze_text("Case", "Karim Haddad is under investigation for money laundering.")
    assert english["events"][0]["category"] == "money_laundering"
    assert english["events"][0]["stance"] == "investigation"

    arabic = analyze_text("ملف", "سامي بن صالح متابع في قضية رشوة.")
    assert any(event["category"] == "corruption" for event in arabic["events"])

    denied = analyze_text("Zoubeida Mentest", "Zoubeida Mentest n'est pas poursuivie pour fraude.")
    assert denied["events"][0]["negated"] is True

    cleared = analyze_text("Zoubeida Mentest", "Zoubeida Mentest a été condamnée pour fraude puis acquittée.")
    assert cleared["events"][0]["stance"] == "acquittal"
    assert cleared["events"][0]["subject_role"] == "acquitted"

    victim = analyze_text("Zoubeida Mentest", "Zoubeida Mentest est victime d'une fraude.")
    assert victim["subject_role"] == "victim"
    assert victim["events"][0]["stance"] == "mention"
    assert victim["guilt_conclusion"] is False

    bribery = analyze_text("Dossier", "The director is charged with bribery.")
    assert any(event["category"] == "bribery" and event["subject_role"] == "charged" for event in bribery["events"])

    quiet = analyze_text("Météo", "Le temps est beau à Tunis.")
    assert quiet["events"] == []
    assert quiet["stance"] == "mention"

    absent = screening_contract("Zoubeida Mentest", screened=True, failed=False, alerts=[], corroborated=False)
    assert absent["screening_status"] == "NoRelevantMediaFound"
    assert "ne signifie pas" in absent["screening_meaning"]
    assert absent["identity_confirmed_by_name_alone"] is False
    review = screening_contract("Zoubeida Mentest", screened=True, failed=False, alerts=[type("Alert", (), {"status": "new"})()], corroborated=False)
    assert review["screening_status"] == "ManualReviewRequired"
    confirmed = screening_contract("Zoubeida Mentest", screened=True, failed=False, alerts=[type("Alert", (), {"status": "confirmed_relevant"})()], corroborated=False)
    assert confirmed["screening_status"] == "ConfirmedMatch"


def test_identity_uses_transliteration_and_ignores_missing_fields():
    assert name_similarity("سامي بن صالح", "Sami Ben Salah") >= 0.92
    from app.adverse_media import Subject

    subject = Subject(customer_id="c", party_id=None, kind="person", names=["Sami Ben Salah"], dob="1979-01-02", nationality="TN", country="TN")
    same = identity_assessment("سامي بن صالح", {}, subject)
    assert same is not None
    assert same[0] >= 82
    assert any("aucun point" in note for note in same[1])

    conflict = identity_assessment("Sami Ben Salah", {"dob": "1990-01-01"}, subject)
    assert conflict is None

    missing_name = identity_assessment("", {}, subject)
    assert missing_name is None


def test_collection_dedup_retry_and_no_fabricated_article(tmp_path):
    with _client(tmp_path) as client:
        db = _session(client)
        source = AdverseMediaSource(
            tenant_id=TENANT_ID,
            name="RSS test",
            kind="rss",
            url="https://exemple.media/rss.xml",
            language="fr",
            frequency_minutes=60,
            active=True,
            config={"format": "rss", "timeout_seconds": 5, "quota_per_hour": 1},
        )
        db.add(source)
        db.commit()
        calls = {"n": 0}

        def fetch(url, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AdverseMediaError("delai depasse")
            assert url.startswith("https://exemple.media/rss.xml")
            assert timeout == 5
            return RSS

        first = collect_source(db, source, fetch=fetch)
        db.commit()
        assert first["status"] == "error"
        assert db.scalar(select(func.count()).select_from(AdverseMediaArticle).where(AdverseMediaArticle.tenant_id == TENANT_ID)) == 0
        assert source.availability == "down"

        second = collect_source(db, source, fetch=fetch)
        db.commit()
        assert second == {"status": "success", "fetched": 1, "stored": 1}
        third = collect_source(db, source, fetch=fetch)
        db.commit()
        assert third["status"] == "quota"
        assert calls["n"] == 2

        inactive = AdverseMediaSource(
            tenant_id=TENANT_ID,
            name="Source coupee",
            kind="rss",
            url="https://exemple.media/off.xml",
            language="fr",
            frequency_minutes=60,
            active=False,
            config={"format": "rss"},
        )
        db.add(inactive)
        db.commit()
        skipped = collect_source(db, inactive, fetch=lambda url, timeout: (_ for _ in ()).throw(AssertionError("reseau")))
        assert skipped["status"] == "skipped"


def test_pipeline_matches_customer_once_and_score_waits_for_a_human(tmp_path):
    with _client(tmp_path) as client:
        db = _session(client)
        customer = _person(db, "Zoubeida Mentest")
        business = Customer(tenant_id=TENANT_ID, kind="business", display_name="Atelier Fictif", external_ref="adv-atelier", canonical_status="approved", risk_tier="high")
        db.add(business)
        db.flush()
        db.add(BusinessProfile(customer_id=business.id, legal_name="Atelier Fictif", jurisdiction="TN"))
        db.add(Party(tenant_id=TENANT_ID, business_customer_id=business.id, role="shareholder", full_name="Zoubeida Mentest", nationality="TN"))
        source = AdverseMediaSource(tenant_id=TENANT_ID, name="Depot test", kind="rss", url="https://exemple.media/rss.xml", language="fr", active=False, config={})
        db.add(source)
        db.flush()
        text = "Zoubeida Mentest, actionnaire de Atelier Fictif, fait l'objet de poursuites pour fraude."
        assert ingest_article(db, source, url="https://exemple.media/zoubeida-fraude", title="Poursuites", body=text) == "stored"
        assert ingest_article(db, source, url="https://exemple.media/zoubeida-fraude", title="Poursuites", body=text) == "duplicate"
        db.commit()

        first = process_queue(db, TENANT_ID)
        second = process_queue(db, TENANT_ID)
        db.commit()
        assert first["analyzed"] == 1
        assert second["analyzed"] == 0
        version = db.scalar(select(AdverseMediaArticleVersion))
        assert version.nlp_engine_version == NLP_ENGINE
        assert version.analysis["guilt_conclusion"] is False
        alerts = db.scalars(select(AdverseMediaAlert).where(AdverseMediaAlert.customer_id == customer.id)).all()
        assert len(alerts) == 1
        assert alerts[0].status == "new"
        assert alerts[0].payload["guilt_conclusion"] is False

        tenant = db.get(Tenant, TENANT_ID)
        before = assess_customer(db, tenant, customer)
        assert all(factor["code"] != "media" for factor in before.factors)

        record_decision(db, alerts[0], "confirmed_relevant", "Le nom et le contexte correspondent au client. Ce n'est pas une declaration de culpabilite.", "Camille Morel")
        db.commit()
        after = db.scalar(select(type(before)).where(type(before).customer_id == customer.id, type(before).current.is_(True)))
        media = next(factor for factor in after.factors if factor["code"] == "media")
        assert media["points"] == 8
        assert after.methodology_version == RISK_METHODOLOGY == "clarity-risk-3.4"

        article = db.scalar(select(AdverseMediaArticle))
        assert reanalyze(db, article, force=False) == "unchanged"
        try:
            reanalyze(db, article, force=True)
            raise AssertionError("reanalyse autorisee apres confirmation")
        except AdverseMediaError:
            pass

        screened = screen_customer(db, TENANT_ID, customer.id, trigger="manual")
        assert screened["status"] == "completed"


def test_failed_analysis_can_be_retried_without_replaying_history(tmp_path, monkeypatch):
    with _client(tmp_path) as client:
        db = _session(client)
        source = AdverseMediaSource(tenant_id=TENANT_ID, name="Echec", kind="rss", url="https://exemple.media/rss.xml", language="fr", active=False, config={})
        db.add(source)
        db.flush()
        ingest_article(db, source, url="https://exemple.media/echec", title="Nour El Fictif", body="Nour El Fictif est soupçonné de corruption.")
        db.commit()
        version = db.scalar(select(AdverseMediaArticleVersion))

        def boom(*_args, **_kwargs):
            raise RuntimeError("moteur indisponible")

        monkeypatch.setattr("app.adverse_media.analyze_text", boom)
        assert analyze_version(db, version) == "failed"
        db.commit()
        monkeypatch.undo()
        assert retry_failed(db, version) == "analyzed"
        assert analyze_version(db, version) == "unchanged"


def test_desk_exposes_adverse_media_and_keeps_opensanctions_path(tmp_path):
    with _client(tmp_path) as client:
        token = client.post("/api/clarity/v1/auth/login", json={"email": ANALYST_EMAIL, "password": DEMO_PASSWORD}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        listing = client.get("/api/clarity/v1/desks/kyc?limit=1", headers=headers)
        assert listing.status_code == 200
        case_id = listing.json()["items"][0]["id"]
        detail = client.get(f"/api/clarity/v1/desks/kyc/{case_id}", headers=headers)
        assert detail.status_code == 200
        body = detail.json()
        assert body["adverse_media"]["guilt_conclusion"] is False
        name = body["customer"]["display_name"]
        db = _session(client)
        source = AdverseMediaSource(tenant_id=TENANT_ID, name="Fiche", kind="rss", url="https://exemple.media/rss.xml", language="fr", active=False, config={})
        db.add(source)
        db.flush()
        ingest_article(db, source, url="https://exemple.media/fiche", title=name, body=f"{name} fait l'objet de poursuites pour fraude.")
        db.commit()
        process_queue(db, TENANT_ID)
        db.commit()
        screened = client.post(f"/api/clarity/v1/desks/kyc/{case_id}/adverse-media", headers=headers)
        assert screened.status_code == 200, screened.text
        media = screened.json()["adverse_media"]
        assert media["last_screening"]["status"] == "completed"
        assert media["alerts"]
        assert media["alerts"][0]["identity_score"] >= 82
        assert media["alerts"][0]["media_score"] >= 40
        sources = client.get("/api/clarity/v1/adverse-media/sources", headers=headers)
        assert sources.status_code == 200
        catalog = [row for row in sources.json() if row["name"] != "Fiche"]
        assert any(row["name"] == "Business News" and row["url"] == "https://businessnews.com.tn/feed/" and row["active"] is True for row in catalog)
        assert any(row["name"] == "Réalités" and row["active"] is True for row in catalog)
        assert all(row["active"] is True and str(row["url"]).startswith("https://") for row in catalog)
        assert any(row["name"] == "Presse tunisienne" and row["url"] == "https://lapresse.tn/feed/" for row in catalog)
        assert any(row["name"] == "GDELT" and row["config"]["query"] == "Tunisia OR Tunisie" for row in catalog)
        admin = client.post("/api/clarity/v1/auth/login", json={"email": ADMIN_EMAIL, "password": DEMO_PASSWORD}).json()["access_token"]
        policy = client.put(
            "/api/clarity/v1/adverse-media/policy",
            headers={"Authorization": f"Bearer {admin}"},
            json={"frequencies_hours": {"high": 24, "medium": 168, "low": 720}},
        )
        assert policy.status_code == 200
        assert policy.json()["frequencies_hours"]["low"] == 720
        denied = client.patch(f"/api/clarity/v1/adverse-media/sources/{sources.json()[0]['id']}", headers=headers, json={"active": True})
        assert denied.status_code == 403


def test_gdelt_reads_the_article_page_and_shareholders_are_parties(tmp_path):
    listing = b'{"articles":[{"url":"https://exemple.media/gdelt-zoubeida","title":"Une depeche","language":"French"}]}'
    page = b"<html><body><p>Zoubeida Mentest fait l'objet de poursuites pour fraude.</p></body></html>"

    def fetch(url, timeout):
        assert timeout == 8
        if "mode=artlist" in url:
            return listing
        assert url == "https://exemple.media/gdelt-zoubeida"
        return page

    with _client(tmp_path) as client:
        db = _session(client)
        customer = _person(db, "Zoubeida Mentest")
        business = Customer(tenant_id=TENANT_ID, kind="business", display_name="Atelier Fictif", external_ref="adv-shareholder", canonical_status="approved")
        db.add(business)
        db.flush()
        from app.services import _parties

        _parties(db, TENANT_ID, business.id, {"shareholders": [{"full_name": "Zoubeida Mentest", "ownership_pct": 30}]})
        holder = db.scalar(select(Party).where(Party.business_customer_id == business.id, Party.role == "shareholder"))
        assert holder is not None
        assert holder.full_name == "Zoubeida Mentest"
        source = AdverseMediaSource(
            tenant_id=TENANT_ID,
            name="GDELT test",
            kind="gdelt",
            url="https://api.gdeltproject.org/api/v2/doc/doc",
            language="fr",
            active=True,
            config={"query": "Tunisie", "timeout_seconds": 8, "quota_per_hour": 5, "max_records": 5},
        )
        db.add(source)
        db.commit()
        result = collect_source(db, source, fetch=fetch)
        db.commit()
        assert result["stored"] == 1
        version = db.scalar(select(AdverseMediaArticleVersion))
        assert "poursuites" in version.body
        assert process_queue(db, TENANT_ID)["analyzed"] == 1
        db.commit()
        alerts = db.scalars(select(AdverseMediaAlert).where(AdverseMediaAlert.customer_id == customer.id)).all()
        assert alerts
        token = client.post("/api/clarity/v1/auth/login", json={"email": ANALYST_EMAIL, "password": DEMO_PASSWORD}).json()["access_token"]
        summary = client.get("/api/clarity/v1/adverse-media/summary", headers={"Authorization": f"Bearer {token}"})
        assert summary.status_code == 200
        assert summary.json()["articles"] >= 1
        assert summary.json()["guilt_conclusion"] is False
    assert content_hash("Titre", "Corps") == content_hash("Titre", "  Corps  ")
    old = utcnow() - timedelta(days=1)
    assert old < utcnow()


def test_deep_search_walks_gdelt_history_and_keeps_only_returned_articles(tmp_path, monkeypatch):
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        assert "startdatetime=" in url
        assert "corruption" in urllib.parse.unquote(url)
        if len(calls) == 1:
            articles = [{"url": f"https://exemple.media/bulk-{i}", "title": f"Lot {i}", "seendate": "20240115T120000Z"} for i in range(100)]
            return json.dumps({"articles": articles}).encode()
        stamp = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["startdatetime"][0]
        return json.dumps({"articles": [{"url": f"https://exemple.media/{stamp}", "title": "Fraude fictive ancienne", "seendate": stamp}]}).encode()

    with _client(tmp_path) as client:
        db = _session(client)
        source = AdverseMediaSource(
            tenant_id=TENANT_ID,
            name="GDELT historique",
            kind="gdelt",
            url="https://api.gdeltproject.org/api/v2/doc/doc",
            language="multi",
            active=True,
            config={"query": "Tunisia OR Tunisie", "timeout_seconds": 5},
        )
        db.add(source)
        db.commit()
        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 1, 20, tzinfo=UTC)
        result = deep_search(db, source, years=1, start=start, end=end, fetch=fetch, pause=0)
        assert result["status"] == "completed"
        assert result["calls"] == 3
        assert result["stored"] == 2
        assert db.scalar(select(func.count()).select_from(AdverseMediaArticle).where(AdverseMediaArticle.tenant_id == TENANT_ID)) == 2
        article = db.scalar(select(AdverseMediaArticle).where(AdverseMediaArticle.url.like("https://exemple.media/2024%")))
        assert article is not None and article.published_at is not None
        other = AdverseMediaSource(tenant_id=TENANT_ID, name="RSS loin", kind="rss", url="https://exemple.media/rss.xml", language="fr", active=True, config={})
        db.add(other)
        db.commit()
        try:
            deep_search(db, other, years=1, start=start, end=end, fetch=fetch, pause=0)
            raise AssertionError("recherche lointaine hors GDELT")
        except AdverseMediaError as exc:
            assert "GDELT" in exc.message
        monkeypatch.setattr("app.api.adverse.launch_deep_search", lambda factory, source_id, years: {"status": "started", "years": years})
        token = client.post("/api/clarity/v1/auth/login", json={"email": ANALYST_EMAIL, "password": DEMO_PASSWORD}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        denied = client.post(f"/api/clarity/v1/adverse-media/sources/{other.id}/deep-search", headers=headers, json={"years": 5})
        assert denied.status_code == 400
        started = client.post(f"/api/clarity/v1/adverse-media/sources/{source.id}/deep-search", headers=headers, json={"years": 3})
        assert started.status_code == 200
        assert started.json()["status"] == "started"
        assert started.json()["years"] == 3


def test_gdelt_rate_limit_waits_and_retries(monkeypatch):
    calls = {"n": 0}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b'{"articles":[]}'

    def urlopen(request, timeout):
        calls["n"] += 1
        assert timeout == 5
        if calls["n"] == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"Please limit requests to one every 5 seconds"))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("app.adverse_media.time.sleep", lambda *_args: None)
    body = http_get("https://api.gdeltproject.org/api/v2/doc/doc?query=Tunisie", 5)
    assert body == b'{"articles":[]}'
    assert calls["n"] == 2


def test_batch_progress_stores_only_what_the_source_returns(tmp_path):
    def fetch(url, _timeout):
        if "gdeltproject.org" in url:
            return b'{"articles":[]}'
        return b'<?xml version="1.0"?><rss><channel></channel></rss>'

    with _client(tmp_path) as client:
        db = _session(client)
        result = run_batch(db, TENANT_ID, fetch=fetch)
        assert result["status"] == "completed"
        assert result["progress"] == 100
        assert result["total"] >= 9
        assert result["stored"] == 0
        assert db.scalar(select(func.count()).select_from(AdverseMediaArticle).where(AdverseMediaArticle.tenant_id == TENANT_ID)) == 0
        from app.models import ProviderConnection

        connection = db.scalar(select(ProviderConnection).where(ProviderConnection.tenant_id == TENANT_ID, ProviderConnection.kind == "adverse_media"))
        if connection is None:
            connection = ProviderConnection(tenant_id=TENANT_ID, kind="adverse_media", name="Dow Jones", mode="not_configured", configured=False, detail={})
            db.add(connection)
        connection.detail = {"provider": "dow_jones"}
        db.commit()
        licensed = run_batch(db, TENANT_ID, fetch=fetch)
        assert licensed["stored"] == 0
        assert licensed["progress"] == 100
        assert "licence" in licensed["message"]
