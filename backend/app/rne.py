"""Lookup a Tunisian company on the Registre National des Entreprises.

WS-KYC and WS-BE are the subscribed web services described by the RNE
(Annexe 2). The public site https://www.registre-entreprises.tn/ does not
expose this payload. Without a bearer token and subscription ids the check
is reported as unavailable and no company data is invented.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.config import Settings, current_settings

IDENTIFIANT_RE = re.compile(r"^[0-9]{7}[A-Z]$")
_SPACE = re.compile(r"\s+")


def normalize_identifiant(value: str | None) -> str:
    text = _SPACE.sub("", (value or "")).upper()
    match = re.match(r"^(\d{7}[A-Z])", text)
    return match.group(1) if match else text


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _flag(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"1", "oui", "yes", "true", "o"}:
        return True
    if text in {"0", "non", "no", "false", "n"}:
        return False
    return None


def _country(value: Any) -> str | None:
    text = _text(value).lower()
    if text in {"tunisienne", "tunisien", "tunisia", "tunisie", "tn"}:
        return "TN"
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return None


def _join_address(*parts: Any) -> str:
    return ", ".join(part for part in (_text(item) for item in parts) if part)


def _pick(source: dict, *keys: str) -> Any:
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
    lowered = {str(key).lower(): value for key, value in source.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _as_list(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("beneficiaires", "beneficiaries", "declarations", "items", "data", "direction"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        if _pick(value, "NP_PERSONNE_FR", "NOM_PRENOM_GESTION_FR", "full_name"):
            return [value]
    return []


def _classify_http(status: int, payload: Any) -> tuple[str, str]:
    message = ""
    if isinstance(payload, dict):
        message = _text(payload.get("message") or payload.get("error") or payload.get("description"))
    elif isinstance(payload, str):
        message = payload.strip()
    lowered = message.lower()
    if "solde disponible" in lowered:
        return "insufficient_balance", message
    if "adresse ip" in lowered:
        return "ip_denied", message
    if "n'est pas autoris" in lowered or "n’est pas autoris" in lowered:
        return "forbidden", message
    if "abonnement est inactive" in lowered or "abonnement est inactif" in lowered:
        return "subscription_inactive", message
    if "entité active" in lowered or "entite active" in lowered:
        return "inactive", message
    if "aucune inscription" in lowered or status == 404:
        return "not_found", message or "Aucune inscription correspondant à l’identifiant fourni n’a été trouvée."
    if status >= 500:
        return "error", message or "Le registre des entreprises est indisponible."
    if status >= 400:
        return "error", message or "Le registre des entreprises a refusé la consultation."
    return "error", message or "Réponse inattendue du registre des entreprises."


def _fiche(payload: dict, identifiant: str) -> dict:
    activities = payload.get("activites") if isinstance(payload.get("activites"), dict) else {}
    primary = activities.get("primaryActivity") if isinstance(activities.get("primaryActivity"), dict) else {}
    legal_name = _text(_pick(payload, "denomination_FR", "denomination_fr"))
    trading_name = _text(_pick(payload, "nom_COMMERCIAL_FR", "nom_commercial_fr"))
    status = _text(_pick(payload, "STATUS", "status"))
    return {
        "identifiant_unique": _text(_pick(payload, "identifiant_UNIQUE", "identifiant_unique")) or identifiant,
        "internal_number": _text(_pick(payload, "num_REGISTRE_INTERNE")),
        "legal_name": legal_name,
        "legal_name_ar": _text(_pick(payload, "denomination_AR")),
        "trading_name": trading_name,
        "trading_name_ar": _text(_pick(payload, "nom_COMMERCIAL_AR")),
        "status": status,
        "active": status.upper() == "NORMAL" if status else None,
        "entity_type": _text(_pick(payload, "type_ENTITE")),
        "legal_form": _text(_pick(payload, "forme_JURIDIQUE_FR", "forme_Juridique_FR")),
        "legal_form_ar": _text(_pick(payload, "forme_Juridique_AR", "forme_JURIDIQUE_AR")),
        "capital_tnd": _pick(payload, "capital_TND"),
        "email": _text(_pick(payload, "adresse_EMAIL")),
        "activity_start": _text(_pick(payload, "date_DEBUT_ACTIVITE")),
        "registered_on": _text(_pick(payload, "date_ENREGISTREMENT_RNE")),
        "updated_on": _text(_pick(payload, "date_DERNIERE_MAJ")),
        "struck_off_on": _text(_pick(payload, "date_RADIATION")),
        "ubo_declared": _flag(_pick(payload, "declaration_BENEFICIAIRES_EFFECTIFS")),
        "address": _join_address(
            _pick(payload, "rue_SIEGE_FR"),
            _pick(payload, "ville_SIEGE_FR"),
            _pick(payload, "code_POSTAL_SIEGE"),
            _pick(payload, "gouvernorat_SIEGE_FR"),
        ),
        "activity_address": _join_address(
            _pick(payload, "rue_ACTIVITE_FR"),
            _pick(payload, "ville_ACTIVITE_FR"),
            _pick(payload, "code_POSTAL_ACTIVITE"),
            _pick(payload, "gouvernorat_ACTIVITE_FR"),
        ),
        "activity": _text(_pick(primary, "detailActiviteLatin", "detail_activite_latin")),
        "activity_code": _text(_pick(primary, "codeActivite")),
    }


def _is_legal_representative(powers: Any) -> bool:
    for power in powers if isinstance(powers, list) else []:
        if not isinstance(power, dict):
            continue
        label = _text(_pick(power, "LIBELLE_POUVOIR_FR", "LIBELLE_POUVOIR_AR")).lower()
        if "représentant" in label or "representant" in label:
            return True
    return False


def _directors(payload: dict) -> list[dict]:
    rows = []
    for item in _as_list(payload.get("direction")):
        name = _text(_pick(item, "NOM_PRENOM_GESTION_FR", "NOM_PRENOM_GESTION_AR"))
        if not name:
            continue
        representative = _is_legal_representative(item.get("pouvoirs"))
        rows.append(
            {
                "full_name": name,
                "role": "representative" if representative else "director",
                "quality": _text(_pick(item, "QALITE_GESTION_FR", "QUALITE_GESTION_AR")),
                "nationality": _country(_pick(item, "NATIONALITE_GESTION_FR", "NATIONALITE_GESTION_AR")),
                "date_of_birth": _text(_pick(item, "DATE_NAISSANCE_GESTION")) or None,
                "identity_number": _text(_pick(item, "NUM_PIECE_IDENTITE_GESTION")) or None,
                "rne_id": _text(_pick(item, "IDENTIFIANT_UNIQUE_GESTION")) or None,
                "source": "rne",
            }
        )
    return rows


def _owners(payload: Any) -> list[dict]:
    rows = []
    for item in _as_list(payload):
        name = _text(_pick(item, "NP_PERSONNE_FR", "NP_PERSONNE", "full_name"))
        if not name:
            continue
        control = _text(_pick(item, "TYPE_BENFICAIRE", "TYPE_BENEFICIAIRE"))
        rows.append(
            {
                "full_name": name,
                "role": "beneficial_owner",
                "nationality": _country(_pick(item, "NATIONALITE")),
                "identity_number": _text(_pick(item, "NID_PERSONNE")) or None,
                "resident": _text(_pick(item, "RESIDENT")) or None,
                "control": control or None,
                "ownership_pct": None,
                "source": "rne",
            }
        )
    return rows


def _configured(settings: Settings, kind: str) -> bool:
    if not settings.rne_token.strip():
        return False
    if kind == "ubo":
        return bool(settings.rne_ubo_subscription_id.strip() and settings.rne_ubo_subscriber_id.strip())
    return bool(settings.rne_kyc_subscription_id.strip() and settings.rne_kyc_subscriber_id.strip())


def _get(settings: Settings, path: str, params: dict) -> httpx.Response:
    url = settings.rne_base_url.rstrip("/") + "/" + path.lstrip("/")
    return httpx.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {settings.rne_token.strip()}", "Accept": "application/json"},
        timeout=20,
    )


def _read_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {"message": response.text}


def check_company(identifiant: str, settings: Settings | None = None) -> dict:
    """Consult WS-KYC, then WS-BE when the company has declared its beneficial owners."""
    settings = settings or current_settings()
    normalized = normalize_identifiant(identifiant)
    base = {
        "provider": "rne",
        "production": False,
        "identifiant_unique": normalized,
        "status": "invalid",
        "message": "L’identifiant unique RNE comporte 7 chiffres suivis d’une lettre.",
        "active": None,
        "fiche": None,
        "directors": [],
        "beneficial_owners": [],
        "ubo_declared": None,
        "name_mismatch": False,
    }
    if not IDENTIFIANT_RE.fullmatch(normalized):
        return base
    if not _configured(settings, "kyc"):
        base["status"] = "unavailable"
        base["message"] = "Le web service KYC du RNE n’est pas configuré (jeton et abonnement)."
        return base
    try:
        response = _get(
            settings,
            settings.rne_kyc_path,
            {
                "subscriptionId": settings.rne_kyc_subscription_id.strip(),
                "subscriberId": settings.rne_kyc_subscriber_id.strip(),
                "identifiantUnique": normalized,
            },
        )
    except httpx.HTTPError:
        base["status"] = "error"
        base["message"] = "Le registre des entreprises n’a pas répondu."
        return base
    payload = _read_json(response)
    if response.status_code != 200 or not isinstance(payload, dict):
        status, message = _classify_http(response.status_code, payload)
        base["status"] = status
        base["message"] = message
        base["production"] = True
        return base
    fiche = _fiche(payload, normalized)
    directors = _directors(payload)
    owners: list[dict] = []
    ubo_note = ""
    if fiche["ubo_declared"] is True and _configured(settings, "ubo"):
        try:
            ubo_response = _get(
                settings,
                settings.rne_ubo_path,
                {
                    "subscriptionId": settings.rne_ubo_subscription_id.strip(),
                    "subscriberId": settings.rne_ubo_subscriber_id.strip(),
                    "codeMotif": settings.rne_ubo_motif.strip() or "1",
                    "identifiantUnique": normalized,
                },
            )
        except httpx.HTTPError:
            ubo_note = "La déclaration des bénéficiaires effectifs n’a pas pu être lue."
        else:
            ubo_payload = _read_json(ubo_response)
            if ubo_response.status_code == 200:
                owners = _owners(ubo_payload)
            else:
                _, ubo_note = _classify_http(ubo_response.status_code, ubo_payload)
    elif fiche["ubo_declared"] is True:
        ubo_note = "La société a déclaré ses bénéficiaires effectifs, mais le web service BE n’est pas configuré."
    message = "Fiche lue sur le Registre national des entreprises."
    if fiche["active"] is False:
        message = f"Entité inscrite avec le statut {fiche['status']}."
    if ubo_note:
        message = f"{message} {ubo_note}"
    return {
        "provider": "rne",
        "production": True,
        "identifiant_unique": fiche["identifiant_unique"] or normalized,
        "status": "found",
        "message": message.strip(),
        "active": fiche["active"],
        "fiche": fiche,
        "directors": directors,
        "beneficial_owners": owners,
        "ubo_declared": fiche["ubo_declared"],
        "name_mismatch": False,
    }


def _same_name(left: str, right: str) -> bool:
    return re.sub(r"[^a-z0-9]", "", left.lower()) == re.sub(r"[^a-z0-9]", "", right.lower())


def _merge_people(primary: list[dict], extra: list[dict]) -> list[dict]:
    merged = [dict(item) for item in primary if _text(item.get("full_name") or item.get("name"))]
    for item in extra:
        name = _text(item.get("full_name") or item.get("name"))
        if not name:
            continue
        if any(_same_name(name, _text(row.get("full_name") or row.get("name"))) for row in merged):
            continue
        row = dict(item)
        row["full_name"] = name
        row.setdefault("source", "declared")
        merged.append(row)
    return merged


def prepare_business(data: dict, settings: Settings | None = None) -> dict:
    """Attach the RNE report and fill an empty company profile from the register."""
    prepared = dict(data)
    raw = prepared.get("identifiant_unique") or prepared.get("registration_number") or ""
    report = check_company(str(raw), settings)
    declared = _text(prepared.get("legal_name") or prepared.get("companyName"))
    fiche = report.get("fiche") or {}
    official = _text(fiche.get("legal_name"))
    if declared and official and not _same_name(declared, official):
        report["name_mismatch"] = True
        report["declared_legal_name"] = declared
    if report["status"] == "found" and fiche:
        if official:
            prepared["legal_name"] = official
            prepared["companyName"] = official
        if fiche.get("trading_name"):
            prepared["trading_name"] = fiche["trading_name"]
        prepared["registration_number"] = report["identifiant_unique"]
        prepared["jurisdiction"] = prepared.get("jurisdiction") or "TN"
        if fiche.get("legal_form"):
            prepared["legal_form"] = fiche["legal_form"]
        if fiche.get("address"):
            prepared["registered_address"] = fiche["address"]
        if fiche.get("activity"):
            prepared["activities"] = fiche["activity"]
            prepared["industry"] = prepared.get("industry") or fiche["activity"]
    prepared["directors"] = _merge_people(list(report.get("directors") or []), list(prepared.get("directors") or []))
    prepared["beneficial_owners"] = _merge_people(
        list(report.get("beneficial_owners") or []),
        list(prepared.get("beneficial_owners") or prepared.get("ubos") or []),
    )
    representative = _text(prepared.get("representative_name"))
    if representative and not any(
        row.get("role") == "representative" and _same_name(representative, _text(row.get("full_name")))
        for row in prepared["directors"]
    ):
        prepared["directors"].append(
            {
                "full_name": representative,
                "role": "representative",
                "nationality": "TN",
                "identity_number": _text(prepared.get("representative_id_number")) or None,
                "source": "declared",
            }
        )
    prepared["registry"] = report
    return prepared
