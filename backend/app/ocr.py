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

_ACCOUNT = ("firstName", "lastName", "dateOfBirth", "nationality", "documentNumber", "expiry", "gender")
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
    text, engine = _extract_text(content)
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
        if not value:
            continue
        extracted[key] = value
        properties[key] = value
        if key == "expiry":
            document.expiry = value
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


def _extract_text(content: bytes) -> tuple[str, str]:
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
                ["tesseract", handle.name, "stdout", "-l", "fra+eng", "--psm", "6"],
                capture_output=True,
                timeout=40,
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
    match = re.fullmatch(r"(\d{2})[./-](\d{2})[./-](\d{4})", text)
    if match:
        text = f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
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


def _fill_labels(text: str, fields: dict[str, str]) -> None:
    patterns = (
        ("firstName", r"(?:pr[ée]nom|given names?|first names?)\s*[:.]\s*(.+)"),
        ("lastName", r"(?<![A-Za-zÀ-ÿ])(?:nom de famille|surname|family name|last name|nom)\s*[:.]\s*(.+)"),
        ("dateOfBirth", r"(?:date de naissance|n[ée]e?(?:\s+le)?|date of birth|dob)\s*[:.]?\s*(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}-\d{2}-\d{2})"),
        ("expiry", r"(?:date d'expiration|expiration|expiry|expires)\s*[:.]?\s*(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}-\d{2}-\d{2})"),
        ("nationality", r"(?:nationalit[ée]|nationality)\s*[:.]\s*([A-Za-z]{2,3})\b"),
        ("documentNumber", r"(?:n[°o]\s*(?:de\s*)?(?:document|pi[èe]ce)?|document number|numero)\s*[:.]\s*([A-Z0-9][A-Z0-9-]{4,})"),
        ("gender", r"(?:sexe|gender)\s*[:.]\s*([MFmf])\b"),
    )
    for key, pattern in patterns:
        if fields[key]:
            continue
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip(" .")
        if key in {"dateOfBirth", "expiry"}:
            value = _iso_date(value)
        elif key == "gender":
            value = value.upper()
        elif key == "nationality":
            value = value.upper()
        elif len(value) > 80:
            value = ""
        fields[key] = value


def _guess_name(text: str) -> tuple[str, str]:
    for raw in text.splitlines():
        words = [word for word in re.split(r"\s+", raw.strip()) if word]
        if not 2 <= len(words) <= 5:
            continue
        if not any(character.islower() for character in raw):
            continue
        folded = [_fold(word) for word in words]
        if any(word in _STOP for word in folded):
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
