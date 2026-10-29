from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT
from app.ids import (
    ADMIN_EMAIL,
    ANALYST_EMAIL,
    BALLERINE_API_KEY,
    CHECKER_EMAIL,
    DEMO_PASSWORD,
    INBOX_ID,
    INVESTIGATOR_EMAIL,
    MARBLE_API_KEY,
    RULE_AMOUNT_ID,
    RULE_COUNTRY_ID,
    RULE_SCREEN_ID,
    SCENARIO_ID,
    SCENARIO_ITERATION_ID,
    TENANT_ID,
)
from app.models import (
    ApiCredential,
    CaseRecord,
    Customer,
    DataModelField,
    Evidence,
    IdentityDocument,
    Inbox,
    IndividualProfile,
    MonitoringRule,
    ProviderConnection,
    Scenario,
    Tag,
    Tenant,
    Transaction,
    User,
    WatchlistEntry,
    WorkflowInstance,
)
from app.security import Principal, hash_password, hash_token
from app.services import DATA_MODELS, assess_customer, evaluate_transaction, persist_screening
from app.timeutil import utcnow


def ensure_demo_logins(db: Session) -> None:
    """Keep the four console accounts on the password shown by the login form."""
    accounts = (
        (ADMIN_EMAIL, "Aline Bernard", "admin"),
        (ANALYST_EMAIL, "Camille Morel", "analyst"),
        (CHECKER_EMAIL, "Hugo Bernard", "checker"),
        (INVESTIGATOR_EMAIL, "Léa Martin", "investigator"),
    )
    digest = hash_password(DEMO_PASSWORD)
    for email, name, role in accounts:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            db.add(User(tenant_id=TENANT_ID, email=email, full_name=name, password_hash=digest, role=role, mfa_enabled=True, active=True))
            continue
        user.password_hash = digest
        user.active = True
        user.role = role
    db.flush()


def ensure_env_admin(db: Session) -> None:
    """Keep the local administrator on the username and password from the environment."""
    from app.config import current_settings

    if db.get(Tenant, TENANT_ID) is None:
        return
    settings = current_settings()
    username = settings.admin_username.strip()
    if not username or not settings.admin_password:
        ensure_oidc_admins(db)
        return
    digest = hash_password(settings.admin_password)
    user = db.scalar(select(User).where(User.tenant_id == TENANT_ID, User.email == username))
    if user is None:
        db.add(User(tenant_id=TENANT_ID, email=username, full_name="Administrateur", password_hash=digest, role="admin", active=True, mfa_enabled=False))
    else:
        user.password_hash = digest
        user.role = "admin"
        user.active = True
    db.flush()
    ensure_oidc_admins(db)


def ensure_oidc_admins(db: Session) -> None:
    """The Keycloak account named in the environment is an administrator."""
    import secrets

    from app.config import current_settings
    from app.security import parse_roles, serialize_roles

    if db.get(Tenant, TENANT_ID) is None:
        return
    for name in sorted(current_settings().oidc_admin_names):
        user = db.scalar(select(User).where(User.tenant_id == TENANT_ID, User.email == name))
        if user is None:
            db.add(
                User(
                    tenant_id=TENANT_ID,
                    email=name,
                    full_name="mifos" if name == "mifos" else name,
                    password_hash=hash_password(secrets.token_urlsafe(24)),
                    role="admin",
                    active=True,
                    mfa_enabled=False,
                )
            )
            continue
        roles = parse_roles(user.role)
        if "admin" not in roles:
            user.role = serialize_roles([*roles, "admin"])
        user.active = True
    db.flush()


DEMO_SEED_VERSION = "tn-100-v1"


def _demo_seed_version(db: Session) -> str | None:
    from app.models import ConfigDocument

    row = db.scalar(
        select(ConfigDocument).where(
            ConfigDocument.tenant_id == TENANT_ID,
            ConfigDocument.kind == "demo_seed",
            ConfigDocument.name == "version",
        )
    )
    if not row or not isinstance(row.payload, dict):
        return None
    return str(row.payload.get("version") or "") or None


def _set_demo_seed_version(db: Session, version: str) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import ConfigDocument

    row = db.scalar(
        select(ConfigDocument).where(
            ConfigDocument.tenant_id == TENANT_ID,
            ConfigDocument.kind == "demo_seed",
            ConfigDocument.name == "version",
        )
    )
    if row:
        row.payload = {"version": version}
        flag_modified(row, "payload")
        return
    db.add(
        ConfigDocument(
            tenant_id=TENANT_ID,
            kind="demo_seed",
            name="version",
            status="active",
            payload={"version": version},
        )
    )


def seed_demo(db: Session, engine=None, seed_clients: bool = True) -> None:
    if db.get(Tenant, TENANT_ID):
        from app.catalog import ensure_studio
        from app.services import purge_clients

        ensure_studio(db)
        ensure_demo_logins(db)
        ensure_env_admin(db)
        if seed_clients and _demo_seed_version(db) != DEMO_SEED_VERSION:
            purge_clients(db, TENANT_ID)
            tenant = db.get(Tenant, TENANT_ID)
            user_ids = {row.email: row.id for row in db.scalars(select(User).where(User.tenant_id == TENANT_ID)).all()}
            if tenant:
                seed_tunisian_cases(db, engine, tenant, user_ids)
            _set_demo_seed_version(db, DEMO_SEED_VERSION)
        ensure_risk_score_case(db, engine, seed_clients=seed_clients)
        if seed_clients:
            ensure_kyc_cycles(db)
        return
    tenant = Tenant(
        id=TENANT_ID,
        name="Banque Démonstration Clarity",
        slug="clarity-demo",
        screening_threshold=86,
        synthetic=True,
    )
    db.add(tenant)
    db.add(Inbox(id=INBOX_ID, tenant_id=TENANT_ID, name="Conformité opérations"))
    users = [
        (ADMIN_EMAIL, "Aline Bernard", "admin", True),
        (ANALYST_EMAIL, "Camille Morel", "analyst", True),
        (CHECKER_EMAIL, "Hugo Bernard", "checker", True),
        (INVESTIGATOR_EMAIL, "Léa Martin", "investigator", True),
    ]
    user_ids = {}
    for email, name, role, mfa in users:
        user = User(
            tenant_id=TENANT_ID,
            email=email,
            full_name=name,
            password_hash=hash_password(DEMO_PASSWORD),
            role=role,
            mfa_enabled=mfa,
        )
        db.add(user)
        db.flush()
        user_ids[email] = user.id
    ensure_env_admin(db)
    for surface, token, name in (
        ("marble", MARBLE_API_KEY, "Démonstration Marble"),
        ("ballerine", BALLERINE_API_KEY, "Démonstration Ballerine"),
    ):
        db.add(ApiCredential(tenant_id=TENANT_ID, surface=surface, name=name, token_hash=hash_token(token)))
    for name, target in (("prioritaire", "case"), ("edd", "case"), ("synthétique", "object")):
        db.add(Tag(tenant_id=TENANT_ID, name=name, target=target))
    for object_type, fields in DATA_MODELS.items():
        for field in fields:
            db.add(
                DataModelField(
                    tenant_id=TENANT_ID,
                    object_type=object_type,
                    name=field,
                    required=field in {"object_id", "updated_at"},
                )
            )
    db.add(
        Scenario(
            id=SCENARIO_ID,
            tenant_id=TENANT_ID,
            name="Surveillance des paiements",
            iteration_id=SCENARIO_ITERATION_ID,
            version="1",
            trigger_object_type="transactions",
            review_threshold=30,
            block_threshold=70,
            decline_threshold=120,
            rules=[
                {
                    "id": RULE_AMOUNT_ID,
                    "name": "Montant unitaire élevé",
                    "kind": "threshold",
                    "field": "amount",
                    "op": "gte",
                    "value": 10000,
                    "score_modifier": 40,
                },
                {
                    "id": RULE_COUNTRY_ID,
                    "name": "Pays de la liste interne de démonstration",
                    "kind": "country",
                    "field": "country",
                    "values": ["KP", "IR", "SY"],
                    "score_modifier": 50,
                },
                {
                    "id": RULE_SCREEN_ID,
                    "name": "Criblage de la contrepartie",
                    "kind": "screening",
                    "score_modifier": 0,
                },
            ],
        )
    )
    fixtures = [
        ("syn-san-001", "sanctions", "Person", ["Viktor Petrov", "Виктор Петров"], ["RU"], ["1974-03-12"], ["sanction"], ["synthetic_sanctions"]),
        (
            "syn-putin-001",
            "sanctions",
            "Person",
            ["Vladimir Putin", "Vladimir Vladimirovich Putin", "Владимир Путин"],
            ["RU"],
            ["1952-10-07"],
            ["sanction", "export.control", "wanted", "debarment", "disqualified", "role.pep", "poi", "politician"],
            ["synthetic_sanctions"],
        ),
        ("syn-pep-001", "pep", "Person", ["Amira El Fassi", "Amira Elfassi", "Amina El Fassi"], ["MA"], ["1982-11-02"], ["role.pep"], ["synthetic_pep"]),
        ("syn-adv-001", "adverse_media", "Person", ["Noah Keller"], ["DE"], ["1990-01-19"], ["adverse_media"], ["synthetic_adverse_media"]),
        ("syn-org-001", "sanctions", "Organization", ["Northwind Trading Ltd", "Northwind Trading Limited"], ["CY"], [], ["sanction"], ["synthetic_sanctions"]),
        ("syn-int-001", "internal", "Person", ["Jonas Keller"], ["FR"], [], ["internal_watchlist"], ["synthetic_internal"]),
    ]
    for entry_id, list_type, schema, names, countries, dobs, topics, datasets in fixtures:
        db.add(
            WatchlistEntry(
                id=entry_id,
                tenant_id=None,
                list_type=list_type,
                schema_name=schema,
                names=names,
                countries=countries,
                birth_dates=dobs,
                topics=topics,
                datasets=datasets,
                synthetic=True,
                properties={"list_type": list_type},
            )
        )
    rules = [
        ("Seuil unitaire", "threshold", {"amount": 10000, "priority": "high"}, "Montant unitaire élevé. Schéma à investiguer."),
        ("Transfrontalier", "cross_border", {"home_countries": ["FR"], "amount": 1000, "priority": "medium"}, "Paiement hors pays d'origine du tenant de démonstration."),
        ("Vélocité", "velocity", {"window_hours": 24, "count": 5, "priority": "high"}, "Nombre d'opérations sur 24 heures."),
        ("Montants ronds", "round_amount", {"multiple": 1000, "minimum": 3000, "window_hours": 48, "count": 3, "priority": "medium"}, "Série de montants ronds."),
    ]
    for name, scenario, params, description in rules:
        db.add(
            MonitoringRule(
                tenant_id=TENANT_ID,
                name=name,
                scenario=scenario,
                parameters=params,
                description=description + " Ceci n'est pas une conclusion de blanchiment.",
            )
        )
    for kind, name, mode, configured in (
        ("identity", "Vérification d'identité", "not_configured", False),
        ("sanctions", "Listes sanctions et PEP", "development_fixture", False),
        ("registry", "Registre des sociétés", "not_configured", False),
        ("storage", "Stockage privé local", "development_filesystem", True),
        ("notification", "Notifications internes", "in_app", True),
    ):
        db.add(
            ProviderConnection(
                tenant_id=TENANT_ID,
                kind=kind,
                name=name,
                mode=mode,
                configured=configured,
                detail={"production": False, "synthetic": True},
            )
        )
    if seed_clients:
        seed_tunisian_cases(db, engine, tenant, user_ids)
        _set_demo_seed_version(db, DEMO_SEED_VERSION)
    from app.catalog import ensure_studio

    ensure_studio(db)
    ensure_risk_score_case(db, engine, seed_clients=seed_clients)
    if seed_clients:
        ensure_kyc_cycles(db)


def ensure_watchlist_entries(db: Session) -> None:
    fixtures = [
        ("syn-san-001", "sanctions", "Person", ["Viktor Petrov", "Виктор Петров"], ["RU"], ["1974-03-12"], ["sanction"], ["synthetic_sanctions"]),
        (
            "syn-putin-001",
            "sanctions",
            "Person",
            ["Vladimir Putin", "Vladimir Vladimirovich Putin", "Владимир Путин"],
            ["RU"],
            ["1952-10-07"],
            ["sanction", "export.control", "wanted", "debarment", "disqualified", "role.pep", "poi", "politician"],
            ["synthetic_sanctions"],
        ),
        ("syn-pep-001", "pep", "Person", ["Amira El Fassi", "Amira Elfassi", "Amina El Fassi"], ["MA"], ["1982-11-02"], ["role.pep"], ["synthetic_pep"]),
        ("syn-adv-001", "adverse_media", "Person", ["Noah Keller"], ["DE"], ["1990-01-19"], ["adverse_media"], ["synthetic_adverse_media"]),
        ("syn-org-001", "sanctions", "Organization", ["Northwind Trading Ltd", "Northwind Trading Limited"], ["CY"], [], ["sanction"], ["synthetic_sanctions"]),
        ("syn-int-001", "internal", "Person", ["Jonas Keller"], ["FR"], [], ["internal_watchlist"], ["synthetic_internal"]),
    ]
    for entry_id, list_type, schema, names, countries, dobs, topics, datasets in fixtures:
        row = db.get(WatchlistEntry, entry_id)
        if row:
            row.names = names
            row.topics = topics
            row.datasets = datasets
            row.birth_dates = dobs
            row.countries = countries
            continue
        db.add(
            WatchlistEntry(
                id=entry_id,
                tenant_id=None,
                list_type=list_type,
                schema_name=schema,
                names=names,
                countries=countries,
                birth_dates=dobs,
                topics=topics,
                datasets=datasets,
                synthetic=True,
                properties={"list_type": list_type},
            )
        )
    db.flush()


_TN_GIVEN = [
    "Amine", "Youssef", "Sami", "Karim", "Mehdi", "Anis", "Hichem", "Nabil", "Rami", "Oussama",
    "Fatma", "Sarra", "Ines", "Mariem", "Nour", "Aya", "Salma", "Yasmine", "Chiraz", "Emna",
    "Skander", "Bilel", "Walid", "Tarek", "Imed", "Hana", "Lina", "Rania", "Olfa", "Sonia",
]
_TN_FAMILY = [
    "Ben Ali", "Trabelsi", "Gharbi", "Jebali", "Mansouri", "Khelifi", "Bouazizi", "Hammami",
    "Chaabane", "Dridi", "Sassi", "Mejri", "Ayari", "Riahi", "Lahmar", "Jouini", "Bouzid",
    "Feki", "Messaoudi", "Karray", "Ben Romdhane", "Belhaj", "Cherif", "Slimani", "Othmani",
]

_TN_SCENARIOS = (
    "clean",
    "face_weak",
    "mrz_bad",
    "pep",
    "sanction",
    "multi_topic",
    "ops",
    "face_fail",
)


def reseed_demo_clients(db: Session, engine=None) -> dict:
    """Reload 100 Tunisian verification cases with distinct risk scenarios."""
    from app.services import purge_clients

    tenant = db.get(Tenant, TENANT_ID)
    if not tenant:
        return {"seeded_customers": 0, "scenarios": 0}
    ensure_watchlist_entries(db)
    purge_clients(db, TENANT_ID)
    tenant = db.get(Tenant, TENANT_ID)
    user_ids = {row.email: row.id for row in db.scalars(select(User).where(User.tenant_id == TENANT_ID)).all()}
    seeded = seed_tunisian_cases(db, engine, tenant, user_ids) if tenant else {"customers": 0, "scenarios": 0}
    _set_demo_seed_version(db, DEMO_SEED_VERSION)
    ensure_risk_score_case(db, engine, seed_clients=True)
    ensure_kyc_cycles(db)
    return {"seeded_customers": seeded.get("customers", 0), "scenarios": seeded.get("scenarios", 0)}


def seed_tunisian_cases(db: Session, engine, tenant: Tenant, user_ids: dict | None = None) -> dict:
    """Create 100 Tunisian-named clients with verification dossiers and varied scores."""
    import uuid

    from app.models import Alert, ScreeningCheck, ScreeningMatch, WorkflowTask

    if db.scalar(select(Customer).where(Customer.tenant_id == TENANT_ID, Customer.external_ref.like("tn-%"))):
        return {"customers": 0, "scenarios": 0}
    analyst = db.scalar(select(User).where(User.tenant_id == TENANT_ID, User.email == ANALYST_EMAIL))
    if not analyst:
        return {"customers": 0, "scenarios": 0}
    created = 0
    for index in range(100):
        scenario = _TN_SCENARIOS[index % len(_TN_SCENARIOS)]
        given = _TN_GIVEN[index % len(_TN_GIVEN)]
        family = _TN_FAMILY[(index * 3) % len(_TN_FAMILY)]
        if index >= len(_TN_GIVEN):
            given = f"{given}"
            family = f"{family} {index // len(_TN_GIVEN) + 1}"
        name = f"{given} {family}"
        ref = f"tn-{index + 1:03d}"
        year = 1970 + (index % 30)
        month = 1 + (index % 12)
        day = 1 + (index % 27)
        dob = f"{year:04d}-{month:02d}-{day:02d}"
        customer = Customer(
            tenant_id=TENANT_ID,
            kind="individual",
            display_name=name,
            external_ref=ref,
            canonical_status="in_review" if scenario != "clean" else "approved",
            purpose="Compte de dépôt",
            expected_activity="Virements locaux et régionaux",
            source_of_funds="Salaire",
            synthetic=True,
        )
        db.add(customer)
        db.flush()
        db.add(
            IndividualProfile(
                customer_id=customer.id,
                legal_name=name,
                given_name=given,
                family_name=family,
                date_of_birth=dob,
                nationality="TN",
                residence_country="TN",
                address=f"{10 + (index % 80)} avenue Habib Bourguiba, Tunis",
                email=f"{given.lower().replace(' ', '')}.{family.lower().replace(' ', '')}.{index + 1}@example.test",
                phone=f"+2162{index:07d}"[:12],
            )
        )
        instance_id = str(uuid.uuid4())
        workflow = WorkflowInstance(
            tenant_id=TENANT_ID,
            customer_id=customer.id,
            definition_key="clarityKycIndividual",
            definition_name="KYC individuel",
            engine="in-process-fallback",
            engine_instance_id=instance_id,
            ballerine_runtime_id=str(uuid.uuid4()),
            state="analystReview",
            status="active",
            context={
                "review_pack": True,
                "scenario": scenario,
                "entity": {"type": "individual", "data": {"firstName": given, "lastName": family, "dateOfBirth": dob, "nationality": "TN"}},
                "faceMatch": _tn_face(scenario),
            },
            config={},
            created_by=analyst.email,
        )
        db.add(workflow)
        db.flush()
        db.add(
            WorkflowTask(
                tenant_id=TENANT_ID,
                workflow_id=workflow.id,
                task_key="analystReview",
                name="Revue analyste",
                status="open",
            )
        )
        mrz_ok = scenario in {"clean", "pep", "ops"}
        mrz_text = "" if mrz_ok else f"P<TUN{family.upper().replace(' ', '')[:8]}<<{given.upper()[:8]}<<<<<<<<<<<<"
        title = "Carte d'identité"
        path, digest = _store_seed_file(
            TENANT_ID,
            f"{ref}-id.svg",
            _specimen(title, [name, "Nationalite tunisienne", f"Scenario {scenario}"]),
        )
        document = IdentityDocument(
            tenant_id=TENANT_ID,
            customer_id=customer.id,
            workflow_id=workflow.id,
            category="identity",
            doc_type="id_card",
            issuing_country="TN",
            issuing_version="clarity-tn-pack",
            status="provided",
            properties={
                "fileName": f"{title}.svg",
                "title": "id_card front",
                "mime": "image/svg+xml",
                "synthetic": True,
                "ocr": {
                    "engine": "seed",
                    "confidence": 0.9 if mrz_ok else 0.35,
                    "production": False,
                    "mrz": mrz_ok,
                    "extracted_text": "MRZ OK" if mrz_ok else mrz_text,
                },
            },
            storage_path=path,
            sha256=digest,
        )
        db.add(document)
        screening = ScreeningCheck(
            tenant_id=TENANT_ID,
            customer_id=customer.id,
            status="no_hit",
            query={"queries": {"seed": {"Person": {"name": name}}}},
            match_count=0,
            production=False,
            provider="clarity-synthetic-watchlist",
        )
        db.add(screening)
        db.flush()
        topics = _tn_topics(scenario)
        if topics:
            screening.status = "confirmed_hit" if scenario in {"sanction", "multi_topic", "face_fail"} else "in_review"
            screening.match_count = 1
            db.add(
                ScreeningMatch(
                    tenant_id=TENANT_ID,
                    screening_id=screening.id,
                    entity_id=f"syn-tn-{index + 1:03d}",
                    status="confirmed_hit" if screening.status == "confirmed_hit" else "pending",
                    score=88 + (index % 10),
                    explanation=[f"Correspondance de démonstration ({scenario})."],
                    payload={
                        "caption": name,
                        "topics": topics,
                        "properties": {"topics": topics, "name": [name]},
                        "provider": "clarity-synthetic-watchlist",
                        "synthetic": True,
                    },
                )
            )
        if scenario == "ops":
            case = CaseRecord(
                tenant_id=TENANT_ID,
                inbox_id=INBOX_ID,
                customer_id=customer.id,
                name=f"Opération à risque — {name}",
                status="closed",
                outcome="confirmed_risk",
            )
            db.add(case)
            db.flush()
            db.add(
                Alert(
                    tenant_id=TENANT_ID,
                    customer_id=customer.id,
                    case_id=case.id,
                    title=case.name,
                    priority="high",
                    status="closed",
                    disposition="valuable_alert",
                    dedup_key=f"seed-ops-{customer.id}",
                    rationale="Scénario démo : risque confirmé par un gestionnaire.",
                )
            )
            if user_ids and user_ids.get(INVESTIGATOR_EMAIL):
                case.assignee_id = user_ids[INVESTIGATOR_EMAIL]
        assess_customer(db, tenant, customer, source="ruleset")
        created += 1
        if created % 20 == 0:
            db.flush()
    db.flush()
    return {"customers": created, "scenarios": created}


def _tn_face(scenario: str) -> dict:
    if scenario == "face_fail":
        return {"score": 0.18, "match": False, "threshold": 40, "source": "selfie", "detail": "Échec facial (scénario).", "engine": "seed"}
    if scenario == "face_weak":
        return {"score": 0.46, "match": True, "threshold": 40, "source": "selfie", "detail": "Visage juste au-dessus du seuil.", "engine": "seed"}
    if scenario in {"sanction", "multi_topic"}:
        return {"score": 0.55, "match": True, "threshold": 40, "source": "selfie", "detail": "Correspondance moyenne.", "engine": "seed"}
    return {"score": 0.86, "match": True, "threshold": 40, "source": "selfie", "detail": "Correspondance OK.", "engine": "seed"}


def _tn_topics(scenario: str) -> list[str]:
    if scenario == "pep":
        return ["role.pep"]
    if scenario == "sanction":
        return ["sanction"]
    if scenario == "multi_topic":
        return ["sanction", "export.control", "wanted", "debarment", "disqualified", "role.pep", "poi", "politician"]
    if scenario == "face_fail":
        return ["wanted"]
    return []


def _store_seed_file(tenant_id: str, filename: str, content: bytes) -> tuple[str, str]:
    """Keep demo specimens on local disk so MinIO SSL issues never block startup seed."""
    import hashlib

    folder = Path(ROOT / "data" / "uploads" / tenant_id)
    # Prefer the runtime upload dir when Clarity runs in the cluster.
    from app.config import current_settings

    try:
        upload = Path(current_settings().upload_dir) / tenant_id
    except Exception:
        upload = folder
    upload.mkdir(parents=True, exist_ok=True)
    path = upload / filename
    path.write_bytes(content)
    return str(path), hashlib.sha256(content).hexdigest()


def _specimen(title: str, lines: list[str]) -> bytes:
    rows = "".join(f'<text x="36" y="{120 + index * 28}" font-size="16" fill="#1b1408">{line}</text>' for index, line in enumerate(lines))
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400">
<rect width="640" height="400" fill="#f4efe4"/>
<rect x="18" y="18" width="604" height="364" rx="16" fill="#fffaf2" stroke="#b08948" stroke-width="3"/>
<text x="36" y="58" font-size="13" letter-spacing="2" fill="#8a6230">SPECIMEN SYNTHETIQUE</text>
<text x="36" y="88" font-size="24" fill="#1b1408">{title}</text>
{rows}
<text x="36" y="348" font-size="12" fill="#8a6230">Document de demonstration. Aucune piece d'identite reelle.</text>
</svg>'''
    return svg.encode()


def ensure_review_pack(db: Session, engine=None) -> None:
    """Attach a reviewable identity file set so the case desks are not empty."""
    if db.scalar(select(IdentityDocument).where(IdentityDocument.issuing_version == "clarity-review-pack")):
        return
    tenant = db.get(Tenant, TENANT_ID)
    if not tenant or engine is None:
        return
    from app.services import start_customer_workflow, store_bytes

    analyst = db.scalar(select(User).where(User.tenant_id == TENANT_ID, User.email == ANALYST_EMAIL))
    if not analyst:
        return
    principal = Principal(TENANT_ID, "clarity", analyst.id, analyst.email, analyst.role)
    subjects = (
        ("cus-amina", "Amina", "El Fassi", "Passeport", "Justificatif de domicile"),
        ("cus-claire", "Claire", "Dumont", "Carte d'identite", "Justificatif de domicile"),
        ("cus-viktor", "Viktor", "Petrov", "Passeport", "Justificatif de domicile"),
        ("cus-putin", "Vladimir", "Putin", "Passeport", "Justificatif de domicile"),
        ("cus-noah", "Noah", "Keller", "Carte d'identite", "Justificatif de domicile"),
        ("cus-sofia", "Sofia", "Martin", "Carte d'identite", "Justificatif de domicile"),
    )
    for ref, given, family, identity_title, address_title in subjects:
        customer = db.scalar(select(Customer).where(Customer.tenant_id == TENANT_ID, Customer.external_ref == ref))
        if not customer:
            continue
        existing = db.scalars(select(WorkflowInstance).where(WorkflowInstance.customer_id == customer.id)).all()
        workflow = next((row for row in existing if (row.context or {}).get("review_pack")), None)
        if workflow is None:
            _customer, workflow = start_customer_workflow(
                db,
                engine,
                tenant,
                principal,
                kind="individual",
                external_ref=ref,
                data={"firstName": given, "lastName": family},
                context={"review_pack": True, "entity": {"type": "individual", "data": {"firstName": given, "lastName": family}}},
                config={},
                synthetic=True,
                definition_key="clarityKycIndividual",
            )
        pages = (
            ("passport" if "Passeport" in identity_title else "id_card", identity_title, [f"{given} {family}", "Nationalite declaree sur le dossier", "Ne pas utiliser comme preuve"]),
            ("proof_of_address", address_title, ["10 rue de la Paix", "75002 Paris", "Fournisseur de demonstration"]),
        )
        for doc_type, title, lines in pages:
            path, digest = store_bytes(ROOT / "data" / "uploads", TENANT_ID, f"{ref}-{doc_type}.svg", _specimen(title, lines), db=db)
            db.add(
                IdentityDocument(
                    tenant_id=TENANT_ID,
                    customer_id=customer.id,
                    workflow_id=workflow.id,
                    category="identity" if doc_type != "proof_of_address" else "proof_of_address",
                    doc_type=doc_type,
                    issuing_country="FR",
                    issuing_version="clarity-review-pack",
                    status="provided",
                    properties={"fileName": f"{title}.svg", "title": title, "mime": "image/svg+xml", "synthetic": True},
                    storage_path=path,
                    sha256=digest,
                )
            )
    for case in db.scalars(select(CaseRecord).where(CaseRecord.tenant_id == TENANT_ID)).all():
        if db.scalar(select(Evidence).where(Evidence.case_id == case.id)):
            continue
        path, digest = store_bytes(ROOT / "data" / "uploads", TENANT_ID, f"note-{case.id[:8]}.svg", _specimen("Note jointe au dossier", [case.name, "Piece versée par le gestionnaire"]), db=db)
        db.add(Evidence(tenant_id=TENANT_ID, case_id=case.id, customer_id=case.customer_id, file_name="note-investigation.svg", storage_path=path, sha256=digest))
    db.flush()


def ensure_risk_score_case(db: Session, engine=None, seed_clients: bool = True) -> None:
    """Keep the risk-score-improvement flow executable and seed one session on it."""
    import uuid

    from app.models import ConfigDocument, WorkflowTask
    from app.services import _complete_active, _screen_customer, _sync_task, assess_customer

    definition_row = db.scalar(
        select(ConfigDocument).where(ConfigDocument.tenant_id == TENANT_ID, ConfigDocument.kind == "workflow_definition", ConfigDocument.name == "risk-score-improvement")
    )
    if definition_row and definition_row.payload.get("displayName") in {None, "", "risk-score-improvement"}:
        definition_row.payload = {**definition_row.payload, "displayName": "Revue du score de risque"}
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(definition_row, "payload")
    for task in db.scalars(select(WorkflowTask).where(WorkflowTask.tenant_id == TENANT_ID, WorkflowTask.task_key == "analystReview")).all():
        if task.name in {"Revue analyste", "analystReview", "manual review"}:
            task.name = "Revue analyste"
    if not seed_clients:
        return
    if db.scalar(select(WorkflowInstance).where(WorkflowInstance.tenant_id == TENANT_ID, WorkflowInstance.definition_key == "risk-score-improvement")):
        return
    tenant = db.get(Tenant, TENANT_ID)
    analyst = db.scalar(select(User).where(User.tenant_id == TENANT_ID, User.email == ANALYST_EMAIL))
    if not tenant or not analyst or engine is None:
        return
    customer = Customer(
        tenant_id=TENANT_ID,
        kind="individual",
        display_name="Nadia Ben Romdhane",
        external_ref="cus-risk-score",
        canonical_status="in_review",
        purpose="Compte de dépôt",
        expected_activity="Salaire",
        source_of_funds="Salaire",
        synthetic=True,
    )
    db.add(customer)
    db.flush()
    db.add(
        IndividualProfile(
            customer_id=customer.id,
            legal_name="Nadia Ben Romdhane",
            given_name="Nadia",
            family_name="Ben Romdhane",
            date_of_birth="1988-04-12",
            nationality="TN",
            residence_country="TN",
            address="14 avenue de la Liberté, Tunis",
            email="nadia.benromdhane@example.test",
        )
    )
    instance_id, task = engine.start("risk-score-improvement", customer.id, {})
    workflow = WorkflowInstance(
        tenant_id=TENANT_ID,
        customer_id=customer.id,
        definition_key="risk-score-improvement",
        definition_name="risk-score-improvement",
        engine=engine.mode,
        engine_instance_id=instance_id,
        ballerine_runtime_id=str(uuid.uuid4()),
        context={"source": "risk-score-improvement"},
        config={},
        created_by=analyst.email,
    )
    db.add(workflow)
    db.flush()
    _sync_task(db, workflow, task)
    _screen_customer(db, tenant, customer)
    _complete_active(db, engine, workflow, {"action": "_"})
    assess_customer(db, tenant, customer, source="ruleset")
    _complete_active(db, engine, workflow, {"action": "_"})
    db.flush()


def seed_score_scenarios(db: Session, engine=None) -> dict:
    """Stamp documents, face results and screening so demo scores diverge clearly."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import Alert, CaseRecord, ScreeningCheck, ScreeningMatch
    from app.services import assess_customer

    tenant = db.get(Tenant, TENANT_ID)
    if not tenant:
        return {"customers": 0, "scenarios": 0}
    specs = {
        "cus-claire": {
            "face": {"score": 0.88, "match": True, "source": "selfie", "detail": "Scénario faible risque."},
            "mrz": True,
            "confirm_screening": False,
            "ops_confirmed": False,
        },
        "cus-sofia": {
            "face": {"score": 0.81, "match": True, "source": "selfie", "detail": "Scénario faible risque."},
            "mrz": True,
            "confirm_screening": False,
            "ops_confirmed": False,
        },
        "cus-amina": {
            "face": {"score": 0.46, "match": True, "source": "selfie", "detail": "Visage juste au-dessus du seuil."},
            "mrz": False,
            "mrz_text": "",
            "confirm_screening": False,
            "ops_confirmed": False,
        },
        "cus-noah": {
            "face": {"score": 0.52, "match": True, "source": "video", "detail": "Adverse media candidat."},
            "mrz": False,
            "mrz_text": "IDFRAXXXXXXXX<<<<<<<<<<<<<<<",
            "confirm_screening": False,
            "ops_confirmed": True,
        },
        "cus-viktor": {
            "face": {"score": 0.22, "match": False, "source": "selfie", "detail": "Échec facial + sanction."},
            "mrz": False,
            "mrz_text": "P<RUSPETROV<<VIKTOR<<<<<<<<<<<<<<<<<<<",
            "confirm_screening": True,
            "ops_confirmed": False,
        },
        "cus-putin": {
            "face": {"score": 0.18, "match": False, "source": "selfie", "detail": "Profil multi-topics sanctions."},
            "mrz": False,
            "mrz_text": "P<RUSPUTIN<<VLADIMIR<<<<<<<<<<<<<<<<<<",
            "confirm_screening": True,
            "ops_confirmed": True,
        },
    }
    applied = 0
    for ref, spec in specs.items():
        customer = db.scalar(select(Customer).where(Customer.tenant_id == TENANT_ID, Customer.external_ref == ref))
        if not customer:
            continue
        workflow = db.scalar(
            select(WorkflowInstance)
            .where(WorkflowInstance.customer_id == customer.id)
            .order_by(WorkflowInstance.created_at.desc())
        )
        if workflow:
            context = dict(workflow.context or {})
            context["faceMatch"] = {
                "score": spec["face"]["score"],
                "match": spec["face"]["match"],
                "threshold": 40,
                "source": spec["face"]["source"],
                "detail": spec["face"]["detail"],
                "engine": "seed",
            }
            workflow.context = context
            flag_modified(workflow, "context")
            if workflow.state not in {"analystReview", "checkerReview", "approved", "rejected"}:
                workflow.state = "analystReview"
        for document in db.scalars(select(IdentityDocument).where(IdentityDocument.customer_id == customer.id)).all():
            if document.doc_type not in {"passport", "id_card", "drivers_license"}:
                continue
            props = dict(document.properties or {})
            text = spec.get("mrz_text") or ""
            props["ocr"] = {
                "engine": "seed",
                "confidence": 0.9 if spec["mrz"] else 0.35,
                "production": False,
                "mrz": bool(spec["mrz"]),
                "extracted_text": text if not spec["mrz"] else "MRZ OK",
            }
            if document.doc_type == "id_card" and ref == "cus-amina":
                props["title"] = "id_card front" if "front" not in str(props.get("title") or "") else props.get("title")
            document.properties = props
            flag_modified(document, "properties")
        screening_ids = list(db.scalars(select(ScreeningCheck.id).where(ScreeningCheck.customer_id == customer.id)).all())
        matches = []
        if screening_ids:
            matches = list(db.scalars(select(ScreeningMatch).where(ScreeningMatch.screening_id.in_(screening_ids))).all())
        if spec["confirm_screening"]:
            for match in matches:
                match.status = "confirmed_hit"
            for screening_id in screening_ids:
                screening = db.get(ScreeningCheck, screening_id)
                if screening:
                    screening.status = "confirmed_hit"
        elif ref in {"cus-claire", "cus-sofia"}:
            for match in matches:
                match.status = "no_hit"
            for screening_id in screening_ids:
                screening = db.get(ScreeningCheck, screening_id)
                if screening:
                    screening.status = "no_hit"
        if spec["ops_confirmed"]:
            existing = db.scalar(
                select(CaseRecord).where(
                    CaseRecord.customer_id == customer.id,
                    CaseRecord.outcome == "confirmed_risk",
                )
            )
            if not existing:
                case = CaseRecord(
                    tenant_id=TENANT_ID,
                    inbox_id=INBOX_ID,
                    customer_id=customer.id,
                    name=f"Opération à risque — {customer.display_name}",
                    status="closed",
                    outcome="confirmed_risk",
                )
                db.add(case)
                db.flush()
                db.add(
                    Alert(
                        tenant_id=TENANT_ID,
                        customer_id=customer.id,
                        case_id=case.id,
                        title=case.name,
                        priority="high",
                        status="closed",
                        disposition="valuable_alert",
                        dedup_key=f"seed-ops-{customer.id}",
                        rationale="Scénario de démonstration : risque confirmé par un gestionnaire.",
                    )
                )
        assess_customer(db, tenant, customer, source="ruleset")
        applied += 1
    db.flush()
    return {"customers": applied, "scenarios": applied}


def ensure_kyc_cycles(db: Session) -> None:
    """Give each client one KYC verification with creation, validation and expiry."""
    from app.models import KycCycle
    from app.services import refresh_kyc_cycles

    now = utcnow()
    specs = {
        "cus-claire": (now - timedelta(days=40), now + timedelta(days=365 * 3 - 40), now - timedelta(days=40)),
        "cus-sofia": (now - timedelta(days=20), now + timedelta(days=365 * 3 - 20), now - timedelta(days=20)),
        "cus-amina": (None, None, now - timedelta(days=12)),
        "cus-viktor": (now - timedelta(days=365 * 3 + 10), now - timedelta(days=10), now - timedelta(days=365 * 3 + 10)),
        "cus-noah": (now - timedelta(days=365 * 3 + 45), now - timedelta(days=45), now - timedelta(days=365 * 3 + 45)),
        "cus-putin": (None, None, now - timedelta(days=3)),
    }
    customers = db.scalars(select(Customer).where(Customer.tenant_id == TENANT_ID)).all()
    for customer in customers:
        row = db.scalar(select(KycCycle).where(KycCycle.customer_id == customer.id).order_by(KycCycle.created_at.desc()))
        if row and row.seeded:
            continue
        spec = specs.get(customer.external_ref or "")
        if spec is None:
            if row is None and customer.canonical_status == "approved":
                db.add(
                    KycCycle(
                        tenant_id=TENANT_ID,
                        customer_id=customer.id,
                        status="valid",
                        created_at=now - timedelta(days=1),
                        validated_at=now - timedelta(days=1),
                        expires_at=now + timedelta(days=365 * 3),
                        seeded=True,
                    )
                )
            elif row is None:
                db.add(KycCycle(tenant_id=TENANT_ID, customer_id=customer.id, status="pending", created_at=customer.created_at, seeded=True))
            continue
        validated_at, expires_at, created_at = spec
        if row is None:
            row = KycCycle(tenant_id=TENANT_ID, customer_id=customer.id)
            db.add(row)
        row.status = "pending" if validated_at is None else "valid"
        row.created_at = created_at
        row.validated_at = validated_at
        row.expires_at = expires_at
        row.reminder = "none"
        row.reminded_at = None
        row.seeded = True
        row.production = False
    db.flush()
    refresh_kyc_cycles(db, TENANT_ID)
