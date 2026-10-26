"""Read identity fields from a stored document.

The reader uses the text embedded in the file, or Tesseract when the file is a
picture and the `tesseract` program is installed. It never copies the name the
customer typed. A local read is not a production identity check.
"""

import re
import shutil
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

_ACCOUNT = ("firstName", "lastName", "dateOfBirth", "nationality", "documentNumber", "expiry", "gender", "address")
_LABELS = (
    ("dateOfBirth", ("date de naissance", "date of birth", "ne e le", "nee le", "ne le", "dob")),
    ("expiry", ("date d expiration", "date expiration", "expiration", "expiry", "expires")),
    ("lastName", ("nom de famille", "surname", "family name", "last name")),
    ("firstName", ("given names", "given name", "first names", "first name")),
    ("nationality", ("nationalite", "nationality")),
    ("documentNumber", ("numero de document", "document number", "numero")),
    ("gender", ("sexe", "gender")),
    ("address", ("adresse", "address", "domicile")),
)
_STOP = {
    "specimen", "synthetique", "synthetic", "demonstration", "document", "passeport", "passport",
    "carte", "identite", "justificatif", "domicile", "preuve", "nationalite", "declaree", "dossier",
    "utiliser", "paris", "fournisseur", "note", "jointe", "gestionnaire", "piece", "reelle",
}
_PARTICLES = {"el", "de", "le", "la", "van", "von", "da", "di"}
_IMAGE_SUFFIX = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG": ".png",
    b"II*\x00": ".tif",
    b"MM\x00*": ".tif",
    b"RIFF": ".webp",
    b"BM": ".bmp",
}


def read_identity(content: bytes) -> dict:
    text, engine = _extract_text(content, "6")
    parsed = _parse(text, engine)
    names = parsed["parsedData"]
    if engine == "tesseract" and not names["firstName"] and not names["lastName"]:
        alternative, alternative_engine = _extract_text(content, "4")
        other = _parse(alternative, alternative_engine)
        other_names = other["parsedData"]
        if other_names["firstName"] or other_names["lastName"] or other_names["dateOfBirth"]:
            return other
    return parsed


def _parse(text: str, engine: str) -> dict:
    fields = _blank()
    _fill_mrz(text, fields)
    _fill_labels(text, fields)
    if not fields["firstName"] and not fields["lastName"]:
        guessed = _guess_name(text)
        fields["firstName"] = guessed[0]
        fields["lastName"] = guessed[1]
    filled = sum(1 for key in ("firstName", "lastName", "dateOfBirth") if fields[key])
    if engine == "unavailable":
        confidence = 0.0
    elif filled >= 2:
        confidence = 0.85
    elif filled == 1:
        confidence = 0.55
    elif text.strip():
        confidence = 0.2
    else:
        confidence = 0.0
    return {
        "parsedData": fields,
        "extracted_text": text.strip(),
        "confidence": confidence,
        "engine": engine,
        "production": False,
    }


def apply_read(document, result: dict) -> None:
    """Keep only the values the reader actually found."""
    from sqlalchemy.orm.attributes import flag_modified

    properties = dict(document.properties or {})
    extracted = dict(properties.get("extracted") or {})
    for key, value in result["parsedData"].items():
        previous = "" if extracted.get(key) is None else str(extracted.get(key))
        if value:
            extracted[key] = value
            properties[key] = value
            if key == "expiry":
                document.expiry = value
            continue
        stored = previous or str(properties.get(key) or "")
        if key in {"firstName", "lastName"} and _rejected_name(stored):
            if _rejected_name(str(properties.get(key) or "")):
                properties.pop(key, None)
            extracted.pop(key, None)
    properties["extracted"] = extracted
    properties["ocr"] = {
        "engine": result["engine"],
        "confidence": result["confidence"],
        "production": False,
        "extracted_text": result["extracted_text"][:4000],
    }
    document.properties = properties
    flag_modified(document, "properties")


def _blank() -> dict[str, str]:
    return {key: "" for key in _ACCOUNT}


def _extract_text(content: bytes, psm: str = "6") -> tuple[str, str]:
    stripped = content.lstrip()
    if stripped.startswith(b"<svg") or stripped.startswith(b"<?xml"):
        return _svg_text(content), "svg-text"
    suffix = _image_suffix(content)
    if not suffix:
        return "", "unavailable"
    if shutil.which("tesseract") is None:
        return "", "unavailable"
    with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
        handle.write(content)
        handle.flush()
        try:
            completed = subprocess.run(
                ["tesseract", handle.name, "stdout", "-l", "fra+eng", "--psm", psm],
                capture_output=True,
                timeout=25,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "", "unavailable"
    if completed.returncode != 0:
        return "", "tesseract"
    return completed.stdout.decode("utf-8", "replace"), "tesseract"


def _svg_text(content: bytes) -> str:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return ""
    lines = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "text":
            continue
        parts = [node.text or "", *(child.tail or "" for child in list(node))]
        line = " ".join(part.strip() for part in parts if part and part.strip())
        if line:
            lines.append(line)
    return "\n".join(lines)


def _image_suffix(content: bytes) -> str:
    for magic, suffix in _IMAGE_SUFFIX.items():
        if content.startswith(magic):
            return suffix
    return ""


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _iso_date(value: str) -> str:
    text = value.strip()
    match = re.fullmatch(r"(\d{2})[./\s-](\d{2})[./\s-](\d{4})", text)
    if match:
        text = f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    compact = re.sub(r"\s+", "", text)
    packed = re.fullmatch(r"(\d{2})(\d{2})(\d{4})", compact)
    if packed:
        text = f"{packed.group(3)}-{packed.group(2)}-{packed.group(1)}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return ""
    try:
        date.fromisoformat(text)
    except ValueError:
        return ""
    return text


def _mrz_date(value: str, birth: bool) -> str:
    if not re.fullmatch(r"\d{6}", value):
        return ""
    year = int(value[:2])
    current = date.today().year % 100
    full = 1900 + year if birth and year > current else 2000 + year
    if not birth and year > current + 20 and full > date.today().year:
        full -= 100
    return _iso_date(f"{full:04d}-{value[2:4]}-{value[4:6]}")


def _mrz_name(field: str) -> tuple[str, str]:
    primary, _, secondary = field.partition("<<")
    last = re.sub(r"\s+", " ", primary.replace("<", " ")).strip().title()
    first = re.sub(r"\s+", " ", secondary.replace("<", " ")).strip().title()
    return first, last


def _fill_mrz(text: str, fields: dict[str, str]) -> None:
    lines = []
    for raw in text.splitlines():
        compact = re.sub(r"\s+", "", raw.upper())
        if re.fullmatch(r"[A-Z0-9<]{30,44}", compact):
            lines.append(compact)
    if len(lines) >= 2 and len(lines[0]) >= 44 and lines[0].startswith("P"):
        first, last = _mrz_name(lines[0][5:])
        fields["firstName"] = first
        fields["lastName"] = last
        fields["documentNumber"] = lines[1][0:9].replace("<", "")
        fields["nationality"] = lines[1][10:13].replace("<", "")
        fields["dateOfBirth"] = _mrz_date(lines[1][13:19], True)
        fields["gender"] = lines[1][20] if lines[1][20] in {"M", "F"} else ""
        fields["expiry"] = _mrz_date(lines[1][21:27], False)
        return
    if len(lines) >= 3 and all(len(line) >= 30 for line in lines[:3]):
        fields["documentNumber"] = lines[0][5:14].replace("<", "")
        fields["dateOfBirth"] = _mrz_date(lines[1][0:6], True)
        fields["gender"] = lines[1][7] if lines[1][7] in {"M", "F"} else ""
        fields["expiry"] = _mrz_date(lines[1][8:14], False)
        fields["nationality"] = lines[1][15:18].replace("<", "")
        first, last = _mrz_name(lines[2])
        fields["firstName"] = first
        fields["lastName"] = last


def _noise_word(word: str) -> bool:
    if word in _STOP:
        return True
    if word.startswith(("nation", "repub", "identit", "passeport", "passport", "naissanc")):
        return True
    return word in {"francaise", "francais", "francaie", "france", "french"} or bool(re.fullmatch(r"francai\w*", word))


def _rejected_name(value: str) -> bool:
    words = [_fold(word) for word in re.split(r"\s+", value.strip()) if word]
    return not words or any(_noise_word(word) for word in words)


def _match_label(line: str) -> tuple[str, int]:
    folded = _fold(line)
    if not folded:
        return "", 0
    best: tuple[str, str] | None = None
    for key, phrases in _LABELS:
        for phrase in phrases:
            if folded == phrase or folded.startswith(phrase + " "):
                if best is None or len(phrase) > len(best[1]):
                    best = (key, phrase)
    if best is not None:
        return best[0], len(best[1].split())
    first = folded.split(" ", 1)[0]
    if first.startswith("nation"):
        return "nationality", 1
    if first.startswith("prenom"):
        return "firstName", 1
    if first == "nom":
        return "lastName", 1
    return "", 0


def _raw_rest(line: str, words_to_drop: int) -> str:
    if ":" in line:
        return line.split(":", 1)[1].strip(" .")
    parts = line.strip().split()
    return " ".join(parts[words_to_drop:]).strip(" .")


def _normalize_field(key: str, value: str) -> str:
    text = value.strip(" .")
    if not text:
        return ""
    if key in {"dateOfBirth", "expiry"}:
        return _iso_date(text)
    if key == "gender":
        letter = text[:1].upper()
        return letter if letter in {"M", "F"} else ""
    if key == "nationality":
        folded = _fold(text)
        if folded.startswith("franc"):
            return "FRA"
        if re.fullmatch(r"[A-Za-z]{2,3}", text):
            return text.upper()
        if len(text.split()) == 1 and not _noise_word(folded):
            return text[:40]
        return ""
    if key in {"firstName", "lastName"}:
        if _rejected_name(text) or len(text) > 80:
            return ""
        return re.sub(r"\s+", " ", text)
    if key == "address":
        return re.sub(r"\s+", " ", text)[:160]
    return text[:80]


def _fill_labels(text: str, fields: dict[str, str]) -> None:
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        key, drop = _match_label(lines[index])
        if not key:
            index += 1
            continue
        multiline = key == "address"
        inline = _raw_rest(lines[index], drop)
        collected = [inline] if inline else []
        index += 1
        while index < len(lines) and len(collected) < (3 if multiline else 1):
            nxt = lines[index].strip()
            if not nxt:
                index += 1
                if collected:
                    break
                continue
            if _match_label(nxt)[0] or re.fullmatch(r"[A-Z0-9<]{30,44}", re.sub(r"\s+", "", nxt.upper())):
                break
            collected.append(nxt)
            index += 1
            if not multiline:
                break
        if fields.get(key):
            continue
        fields[key] = _normalize_field(key, ", ".join(part for part in collected if part) if multiline else " ".join(collected))


def _guess_name(text: str) -> tuple[str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, raw in enumerate(lines):
        if _match_label(raw)[0] or _rejected_name(raw):
            continue
        words = raw.split()
        if len(words) != 1 or not words[0].isupper():
            continue
        if not re.fullmatch(r"[A-ZÀ-Ý][A-ZÀ-Ý'’-]{1,}", words[0]):
            continue
        if index + 1 >= len(lines):
            continue
        given = lines[index + 1]
        given_words = given.split()
        if not 1 <= len(given_words) <= 4 or _match_label(given)[0] or _rejected_name(given):
            continue
        if all(re.fullmatch(r"[A-ZÀ-Ý][A-Za-zÀ-ÿ'’-]*", word) for word in given_words):
            return given, words[0]
    for raw in lines:
        words = [word for word in raw.split() if word]
        if not 2 <= len(words) <= 4 or _rejected_name(raw):
            continue
        if not any(character.islower() for character in raw):
            continue
        if not all(re.fullmatch(r"[A-ZÀ-Ý][A-Za-zÀ-ÿ'’-]*", word) or _fold(word) in _PARTICLES for word in words):
            continue
        return words[0], " ".join(words[1:])
    return "", ""


def run_on_document(document) -> dict | None:
    if not document.storage_path or not Path(document.storage_path).is_file():
        return None
    result = read_identity(Path(document.storage_path).read_bytes())
    apply_read(document, result)
    return result


def read_path(path: str | None) -> dict:
    if not path:
        return read_identity(b"")
    file = Path(path)
    if not file.is_file():
        return read_identity(b"")
    return read_identity(file.read_bytes())
