"""Screening sources consulted through OpenSanctions.

Each source is a true/false control. A name match is kept only when it comes
from a source that is switched on and that OpenSanctions actually publishes.
Sources without a published dataset stay configurable, and no hit is invented
for them.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ProviderConnection

# dataset: OpenSanctions collection used by /match/{dataset}
# program: narrower regime inside that dataset, when the list is not the whole dataset
SCREENING_SOURCES: list[dict] = [
    {
        "id": "tn_arp",
        "dataset": "tn_arp",
        "program": None,
        "level": "PEP 1",
        "name": "Chambre des représentants de Tunisie",
        "detail": "Registre des PEP. Membres du Parlement et hauts fonctionnaires de la branche législative.",
    },
    {
        "id": "bvmt",
        "dataset": None,
        "program": None,
        "level": "PEP 3",
        "name": "Bourse de Tunis",
        "detail": "Registre des mesures d'exécution, actions d'exécution des sociétés cotées et décisions disciplinaires.",
    },
    {
        "id": "ugtt",
        "dataset": None,
        "program": None,
        "level": "PEP 3",
        "name": "Union Générale Tunisienne du Travail (UGTT)",
        "detail": "Registre des PEP. Hauts responsables syndicaux.",
    },
    {
        "id": "gouvernorat_tunis",
        "dataset": None,
        "program": None,
        "level": "PEP 4",
        "name": "Gouvernorat de Tunis",
        "detail": "Registre des PEP. Fonctionnaires du gouvernement régional et cadres du gouvernorat.",
    },
    {
        "id": "agi",
        "dataset": None,
        "program": None,
        "level": "Médias défavorables",
        "name": "Agenzia Giornalistica Italia (AGI)",
        "detail": "Signaux de médias défavorables. Filtrage des nouvelles négatives de la presse internationale couvrant la Tunisie.",
    },
    {
        "id": "tn_cnlct",
        "dataset": "tn_cnlct",
        "program": None,
        "level": "Sanctions",
        "name": "Commission Nationale de Lutte Contre le Terrorisme",
        "detail": "Liste nationale des sanctions. Désignations de financement du terrorisme en vertu de la Loi n° 2015-26.",
    },
    {
        "id": "ca_facfoa_tun",
        "dataset": "ca_facfoa",
        "program": "CA-FACFOA-TUN",
        "level": "Sanctions",
        "name": "Gouvernement du Canada — FACFOA Tunisie",
        "detail": "Règlement sur le blocage des avoirs des agents publics étrangers corrompus (Tunisie), SOR/2011-78.",
    },
    {
        "id": "ctaf",
        "dataset": None,
        "program": None,
        "level": "Sanctions",
        "name": "Commission d'Analyse Financière (CTAF)",
        "detail": "Registre des mesures d'exécution et des désignations de la cellule de renseignement financier.",
    },
    {
        "id": "menafatf",
        "dataset": None,
        "program": None,
        "level": "Surveillance",
        "name": "MENAFATF",
        "detail": "Désignations des États membres. Liste de surveillance régionale du Groupe d'action financière du Moyen-Orient et de l'Afrique du Nord.",
    },
    {
        "id": "un_sc_sanctions",
        "dataset": "un_sc_sanctions",
        "program": None,
        "level": "Sanctions",
        "name": "Conseil de sécurité de l'ONU",
        "detail": "Liste consolidée des sanctions. Sanctions multilatérales transposées par la CTAF.",
    },
    {
        "id": "us_ofac_sdn",
        "dataset": "us_ofac_sdn",
        "program": None,
        "level": "Sanctions",
        "name": "OFAC SDN",
        "detail": "Liste des ressortissants spécialement désignés. Désignations du Trésor américain, couverture de l'Afrique du Nord.",
    },
    {
        "id": "arab_league",
        "dataset": None,
        "program": None,
        "level": "Sanctions",
        "name": "Ligue arabe",
        "detail": "Registre des sanctions politiques et économiques. Décisions d'exécution des organismes régionaux.",
    },
    {
        "id": "basel_aml_index",
        "dataset": None,
        "program": None,
        "level": "Risque pays",
        "name": "Indice AML de Bâle",
        "detail": "Signaux de risque pour la Tunisie. Contexte de risque national mis à jour annuellement.",
    },
    {
        "id": "fatf",
        "dataset": None,
        "program": None,
        "level": "Risque pays",
        "name": "GAFI",
        "detail": "Liste des juridictions à haut risque et sous surveillance. Contexte de risque réglementaire mondial.",
    },
    {
        "id": "interpol_red_notices",
        "dataset": "interpol_red_notices",
        "program": None,
        "level": "Recherches",
        "name": "Interpol",
        "detail": "Registre des notices pénales. Personnes recherchées transfrontalières et diffusions criminelles.",
    },
]

_BY_ID = {row["id"]: row for row in SCREENING_SOURCES}


def source_catalog(stored: dict | None = None, *, master: bool = True) -> list[dict]:
    chosen = stored if isinstance(stored, dict) else {}
    rows = []
    for source in SCREENING_SOURCES:
        rows.append(
            {
                "id": source["id"],
                "name": source["name"],
                "level": source["level"],
                "detail": source["detail"],
                "dataset": source["dataset"],
                "covered": bool(source["dataset"]),
                "enabled": bool(chosen.get(source["id"], True)),
            }
        )
    rows.insert(
        0,
        {
            "id": "opensanctions",
            "name": "OpenSanctions",
            "level": "Fournisseur",
            "detail": "Interrupteur général. Aucune liste n'est interrogée quand il est faux.",
            "dataset": "default",
            "covered": True,
            "enabled": bool(master),
        },
    )
    return rows


def _connection(db: Session, tenant_id: str) -> ProviderConnection | None:
    return db.scalar(
        select(ProviderConnection).where(
            ProviderConnection.tenant_id == tenant_id,
            ProviderConnection.kind == "sanctions",
        )
    )


def screening_scope(db: Session, tenant_id: str) -> tuple[bool, list[dict]]:
    """Master switch, then the sources that are on."""
    row = _connection(db, tenant_id)
    detail = (row.detail or {}) if row else {}
    master = bool(detail.get("enabled", True))
    stored = detail.get("sources") if isinstance(detail.get("sources"), dict) else {}
    selected = [source for source in SCREENING_SOURCES if bool(stored.get(source["id"], True))]
    return master, selected


def store_sources(detail: dict, incoming: dict) -> None:
    stored = dict(detail.get("sources") or {})
    for key, value in incoming.items():
        if key in _BY_ID:
            stored[key] = bool(value)
    detail["sources"] = stored


def _blob(item: dict) -> str:
    datasets = [str(name) for name in (item.get("datasets") or [])]
    properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
    return " ".join(datasets) + " " + json.dumps(properties, ensure_ascii=False)


def sources_for_hit(item: dict, enabled: list[dict]) -> list[dict]:
    """Sources whose published OpenSanctions dataset contains this candidate."""
    datasets = {str(name) for name in (item.get("datasets") or [])}
    blob = _blob(item).upper()
    matched = []
    for source in enabled:
        dataset = source.get("dataset")
        if not dataset or dataset not in datasets:
            continue
        program = source.get("program")
        if program and program.upper() not in blob and "PROGRAM" in blob:
            continue
        matched.append(source)
    return matched
