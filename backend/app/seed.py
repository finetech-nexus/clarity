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


def seed_demo(db: Session, engine=None, seed_clients: bool = True) -> None:
    if db.get(Tenant, TENANT_ID):
        from app.catalog import ensure_studio

        ensure_studio(db)
        ensure_demo_logins(db)
        ensure_env_admin(db)
        ensure_risk_score_case(db, engine, seed_clients=seed_clients)
        if seed_clients:
            ensure_review_pack(db, engine)
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
        ("syn-pep-001", "pep", "Person", ["Amira El Fassi", "Amira Elfassi"], ["MA"], ["1982-11-02"], ["role.pep"], ["synthetic_pep"]),
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
        _seed_clients(db, tenant, user_ids)
    from app.catalog import ensure_studio

    ensure_studio(db)
    ensure_risk_score_case(db, engine, seed_clients=seed_clients)
    if seed_clients:
        ensure_review_pack(db, engine)
        ensure_kyc_cycles(db)


def _seed_clients(db: Session, tenant: Tenant, user_ids: dict) -> None:
    customers = [
        ("cus-amina", "Amina El Fassi", "1988-04-18", "FR", "FR", "amina.elfassi@example.test"),
        ("cus-viktor", "Viktor Petrov", "1974-03-12", "RU", "FR", "viktor.petrov@example.test"),
        ("cus-noah", "Noah Keller", "1990-01-19", "DE", "FR", "noah.keller@example.test"),
        ("cus-claire", "Claire Dumont", "1995-07-09", "FR", "FR", "claire.dumont@example.test"),
    ]
    customer_rows = {}
    for ref, name, dob, nationality, residence, email in customers:
        given, family = name.split(" ", 1)
        customer = Customer(
            tenant_id=TENANT_ID,
            kind="individual",
            display_name=name,
            external_ref=ref,
            canonical_status="approved" if ref == "cus-claire" else "in_review",
            purpose="Compte de dépôt",
            expected_activity="Virements européens mensuels",
            source_of_funds="Salaire",
            synthetic=True,
        )
        db.add(customer)
        db.flush()
        profile = IndividualProfile(
            customer_id=customer.id,
            legal_name=name,
            given_name=given,
            family_name=family,
            date_of_birth=dob,
            nationality=nationality,
            residence_country=residence,
            address="10 rue de la Paix, Paris",
            email=email,
            phone="+33100000000",
        )
        customer.individual = profile
        db.add(profile)
        customer_rows[ref] = customer
    db.flush()
    for ref in ("cus-viktor", "cus-noah", "cus-amina", "cus-claire"):
        customer = customer_rows[ref]
        persist_screening(
            db,
            tenant,
            {"Person": {"name": customer.display_name, "birthDate": customer.individual.date_of_birth, "nationality": customer.individual.nationality}},
            rule_id=None,
            decision_id=None,
            customer_id=customer.id,
            counterparty=ref,
        )
        assess_customer(db, tenant, customer)
    now = utcnow()
    samples = [
        ("tx-claire-1", "cus-claire", 450.0, "FR", "Salaire", now - timedelta(days=2)),
        ("tx-claire-border", "cus-claire", 2500.0, "DE", "Fournisseur", now - timedelta(days=1)),
        ("tx-viktor-high", "cus-viktor", 18000.0, "AE", "Transfert", now - timedelta(hours=5)),
        ("tx-round-1", "cus-amina", 5000.0, "FR", "Virement", now - timedelta(hours=30)),
        ("tx-round-2", "cus-amina", 4000.0, "FR", "Virement", now - timedelta(hours=20)),
        ("tx-round-3", "cus-amina", 3000.0, "FR", "Virement", now - timedelta(hours=6)),
    ]
    for correlation, ref, amount, country, description, occurred in samples:
        tx = Transaction(
            tenant_id=TENANT_ID,
            customer_id=customer_rows[ref].id,
            correlation_id=correlation,
            amount=amount,
            currency="EUR",
            base_amount=amount,
            base_currency="EUR",
            direction="outbound",
            country=country,
            counterparty_name="Contrepartie démonstration",
            description=description,
            occurred_at=occurred,
            payload={"synthetic": True},
            synthetic=True,
        )
        db.add(tx)
        db.flush()
        evaluate_transaction(db, tenant, tx)
    db.flush()
    for index in range(1, 6):
        tx = Transaction(
            tenant_id=TENANT_ID,
            customer_id=customer_rows["cus-noah"].id,
            correlation_id=f"tx-noah-{index}",
            amount=700 + index,
            currency="EUR",
            base_amount=700 + index,
            base_currency="EUR",
            direction="outbound",
            country="FR",
            counterparty_name="Commerce",
            description="Série de démonstration",
            occurred_at=now - timedelta(hours=index),
            payload={"synthetic": True},
            synthetic=True,
        )
        db.add(tx)
        db.flush()
        evaluate_transaction(db, tenant, tx)
    open_case = db.scalar(select(CaseRecord).where(CaseRecord.tenant_id == TENANT_ID))
    if open_case:
        open_case.assignee_id = user_ids[INVESTIGATOR_EMAIL]
        open_case.status = "investigating"
    db.flush()


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
            path, digest = store_bytes(ROOT / "data" / "uploads", TENANT_ID, f"{ref}-{doc_type}.svg", _specimen(title, lines))
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
        path, digest = store_bytes(ROOT / "data" / "uploads", TENANT_ID, f"note-{case.id[:8]}.svg", _specimen("Note jointe au dossier", [case.name, "Piece versée par le gestionnaire"]))
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
        display_name="Nadia Benali",
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
            legal_name="Nadia Benali",
            given_name="Nadia",
            family_name="Benali",
            date_of_birth="1988-04-12",
            nationality="FR",
            residence_country="FR",
            address="14 rue des Martyrs, Paris",
            email="nadia.benali@example.test",
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


def ensure_kyc_cycles(db: Session) -> None:
    """Give each client one KYC verification with creation, validation and expiry."""
    from app.models import KycCycle
    from app.services import refresh_kyc_cycles

    now = utcnow()
    specs = {
        "cus-claire": (now - timedelta(days=40), now + timedelta(days=365 * 3 - 40), now - timedelta(days=40)),
        "cus-amina": (None, None, now - timedelta(days=12)),
        "cus-viktor": (now - timedelta(days=365 * 3 + 10), now - timedelta(days=10), now - timedelta(days=365 * 3 + 10)),
        "cus-noah": (now - timedelta(days=365 * 3 + 45), now - timedelta(days=45), now - timedelta(days=365 * 3 + 45)),
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
