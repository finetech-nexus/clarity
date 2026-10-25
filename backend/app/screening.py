import re
import unicodedata
from difflib import SequenceMatcher

CYRILLIC = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ы": "y",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "ь": "",
    "ъ": "",
}


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = "".join(CYRILLIC.get(char, char) for char in text.lower())
    text = text.replace("ø", "o").replace("æ", "ae").replace("œ", "oe").replace("ß", "ss")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def name_score(left: str, right: str) -> int:
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return 0
    if a == b:
        return 100
    direct = SequenceMatcher(None, a, b).ratio()
    tokens_a = " ".join(sorted(a.split()))
    tokens_b = " ".join(sorted(b.split()))
    tokens = SequenceMatcher(None, tokens_a, tokens_b).ratio()
    return int(round(max(direct, tokens) * 100))


def explain_match(query_name: str, candidate_name: str, score: int, dob: str | None, countries: list[str]) -> list[str]:
    reasons = [
        f"Correspondance de nom {score}/100 entre « {query_name} » et « {candidate_name} » après normalisation.",
    ]
    if dob:
        reasons.append(f"Date de naissance fournie : {dob}. Une date discordante réduit le score, elle ne confirme pas l'identité.")
    if countries:
        reasons.append("Pays comparés : " + ", ".join(countries) + ".")
    reasons.append("Le score est un indice. Il ne constitue pas une correspondance confirmée.")
    return reasons


def extract_query_name(body: dict) -> tuple[str, str, dict]:
    for kind in ("Person", "Organization", "Vehicle", "Thing"):
        if kind in body and isinstance(body[kind], dict):
            return kind, str(body[kind].get("name") or ""), body[kind]
    return "", "", {}
