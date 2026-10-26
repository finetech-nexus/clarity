"""Read identity fields from a stored document.

Photos are read by the PP-OCRv5 models shipped in the image. The text embedded
in the file, then Tesseract, are used only when those models are not installed.
The reader never copies the name the customer typed. A local read is not a
production identity check.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
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
    paddle_text, paddle_fields, used_paddle = _paddle_read(content)
    if used_paddle:
        parsed = _parse(paddle_text, "paddleocr")
        _merge_paddle_fields(parsed, paddle_fields)
        return parsed
    text, engine = _extract_text(content, "6")
    parsed = _parse(text, engine)
    if engine == "tesseract" and not _trusted(parsed):
        alternative, alternative_engine = _extract_text(content, "4")
        parsed = _choose(parsed, _parse(alternative, alternative_engine))
    return parsed


_PADDLE_LOCK = threading.Lock()
_PADDLE_ENGINE = None


def _paddle_dir(name: str, fallback: str) -> Path:
    return Path(os.environ.get(name, fallback))


def _paddle_ready() -> bool:
    det = _paddle_dir("CLARITY_PADDLE_DET", "/opt/paddleocr/det")
    rec = _paddle_dir("CLARITY_PADDLE_REC", "/opt/paddleocr/rec")
    return (det / "inference.pdiparams").is_file() and (rec / "inference.pdiparams").is_file()


def _local_paddle():
    """Load the models shipped in the image. Missing files mean this environment has no model."""
    global _PADDLE_ENGINE
    if _PADDLE_ENGINE is not None:
        return _PADDLE_ENGINE or None
    with _PADDLE_LOCK:
        if _PADDLE_ENGINE is not None:
            return _PADDLE_ENGINE or None
        if not _paddle_ready():
            _PADDLE_ENGINE = False
            return None
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        det = _paddle_dir("CLARITY_PADDLE_DET", "/opt/paddleocr/det")
        rec = _paddle_dir("CLARITY_PADDLE_REC", "/opt/paddleocr/rec")
        try:
            from paddleocr import PaddleOCR

            _PADDLE_ENGINE = PaddleOCR(
                text_detection_model_name="PP-OCRv5_mobile_det",
                text_detection_model_dir=str(det),
                text_recognition_model_name="latin_PP-OCRv5_mobile_rec",
                text_recognition_model_dir=str(rec),
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        except Exception:
            _PADDLE_ENGINE = False
            return None
    return _PADDLE_ENGINE or None


def _paddle_lines(result) -> str:
    rows: list[tuple[float, str]] = []
    items = result if isinstance(result, list) else [result]
    for item in items:
        texts = None
        polys = None
        if hasattr(item, "rec_texts"):
            texts = list(item.rec_texts or [])
            polys = list(getattr(item, "dt_polys", None) or [])
        elif isinstance(item, dict):
            payload = item.get("res") if isinstance(item.get("res"), dict) else item
            texts = list(payload.get("rec_texts") or [])
            polys = list(payload.get("dt_polys") or [])
        elif isinstance(item, list):
            for block in item:
                if not isinstance(block, (list, tuple)) or len(block) < 2:
                    continue
                box, recognized = block[0], block[1]
                text = recognized[0] if isinstance(recognized, (list, tuple)) else recognized
                if not str(text).strip():
                    continue
                try:
                    y = min(float(point[1]) for point in box)
                except (TypeError, ValueError, IndexError):
                    y = 0.0
                rows.append((y, str(text).strip()))
            continue
        if not texts:
            continue
        for index, text in enumerate(texts):
            if not str(text).strip():
                continue
            y = float(index)
            if index < len(polys) and polys[index] is not None:
                try:
                    y = min(float(point[1]) for point in polys[index])
                except (TypeError, ValueError, IndexError):
                    y = float(index)
            rows.append((y, str(text).strip()))
    rows.sort(key=lambda row: row[0])
    return "\n".join(text for _, text in rows)


def _paddle_read(content: bytes) -> tuple[str, dict, bool]:
    """Read a photo with the local PP-OCRv5 models. SVG text stays in the file parser."""
    if not _image_suffix(content):
        return "", {}, False
    engine = _local_paddle()
    if engine is None:
        return "", {}, False
    suffix = _image_suffix(content)
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
            handle.write(content)
            handle.flush()
            predict = getattr(engine, "predict", None) or engine.ocr
            result = predict(handle.name)
    except Exception:
        return "", {}, False
    return _paddle_lines(result), {}, True


def _merge_paddle_fields(parsed: dict, fields: dict) -> None:
    data = parsed["parsedData"]
    mapping = {
        "dateOfBirth": fields.get("date_of_birth"),
        "expiry": fields.get("expiry_date"),
        "nationality": fields.get("nationality"),
        "documentNumber": fields.get("document_number"),
        "gender": fields.get("gender"),
        "address": fields.get("address"),
    }
    for key, value in mapping.items():
        if data.get(key) or value is None:
            continue
        normalized = _normalize_field(key, str(value))
        if normalized:
            data[key] = normalized
    if not data["firstName"] and not data["lastName"]:
        parts = [part for part in str(fields.get("full_name") or "").split() if part]
        if len(parts) == 2 and _plausible_name(parts[0]) and _plausible_name(parts[1]):
            data["firstName"] = parts[0]
            data["lastName"] = parts[1]
    if not parsed.get("mrz"):
        _drop_fragments(data)
    _refresh_confidence(parsed)


def _refresh_confidence(parsed: dict) -> None:
    if parsed["engine"] == "unavailable":
        return
    filled = sum(1 for key in ("firstName", "lastName", "dateOfBirth") if parsed["parsedData"].get(key))
    if parsed.get("mrz") and filled >= 2:
        parsed["confidence"] = max(parsed["confidence"], 0.9)
    elif filled >= 2:
        parsed["confidence"] = max(parsed["confidence"], 0.85)
    elif filled == 1:
        parsed["confidence"] = max(parsed["confidence"], 0.55)


def _merge_missing(primary: dict, secondary: dict) -> dict:
    for key, value in secondary["parsedData"].items():
        if value and not primary["parsedData"].get(key):
            primary["parsedData"][key] = value
    if not primary["extracted_text"] and secondary["extracted_text"]:
        primary["extracted_text"] = secondary["extracted_text"]
    filled = sum(1 for key in ("firstName", "lastName", "dateOfBirth") if primary["parsedData"].get(key))
    if primary["engine"] != "unavailable" and filled >= 2:
        primary["confidence"] = max(primary["confidence"], 0.85)
    elif primary["engine"] != "unavailable" and filled == 1:
        primary["confidence"] = max(primary["confidence"], 0.55)
    return primary


def _parse(text: str, engine: str) -> dict:
    fields = _blank()
    mrz = _fill_mrz(text, fields)
    _fill_labels(text, fields)
    if not fields["firstName"] and not fields["lastName"]:
        guessed = _guess_name(text)
        fields["firstName"] = guessed[0]
        fields["lastName"] = guessed[1]
    if not mrz:
        _drop_fragments(fields)
    filled = sum(1 for key in ("firstName", "lastName", "dateOfBirth") if fields[key])
    if engine == "unavailable":
        confidence = 0.0
    elif mrz and filled >= 2:
        confidence = 0.9
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
        "mrz": mrz,
        "production": False,
    }


def _drop_fragments(fields: dict[str, str]) -> None:
    """A short all-caps token left without its pair is OCR noise, not a name."""
    for key, other in (("firstName", "lastName"), ("lastName", "firstName")):
        value = fields[key]
        if value.isupper() and " " not in value and len(value) <= 4 and not fields[other]:
            fields[key] = ""


def _trusted(parsed: dict) -> bool:
    data = parsed["parsedData"]
    if parsed.get("mrz"):
        return bool(data["dateOfBirth"] and (data["firstName"] or data["lastName"]))
    return bool(data["firstName"] and data["lastName"] and data["dateOfBirth"])


def _choose(primary: dict, secondary: dict) -> dict:
    if secondary.get("mrz") and not primary.get("mrz"):
        _merge_missing(secondary, primary)
        return secondary
    _merge_missing(primary, secondary)
    return primary


def apply_read(document, result: dict) -> None:
    """Keep only the values the reader actually found."""
    from sqlalchemy.orm.attributes import flag_modified

    properties = dict(document.properties or {})
    extracted = dict(properties.get("extracted") or {})
    for key in _ACCOUNT:
        value = str(result["parsedData"].get(key) or "").strip()
        if value:
            extracted[key] = value
            properties[key] = value
            if key == "expiry":
                document.expiry = value
            continue
        extracted[key] = ""
        properties.pop(key, None)
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
    iso = _iso_date(f"{full:04d}-{value[2:4]}-{value[4:6]}")
    if birth and iso and not _past_birth(iso):
        return ""
    return iso


def _past_birth(iso: str) -> bool:
    try:
        born = date.fromisoformat(iso)
    except ValueError:
        return False
    return date(1900, 1, 1) <= born <= date.today()


def _mrz_check_digit(data: str) -> str:
    total = 0
    for index, char in enumerate(data):
        if char == "<":
            value = 0
        elif char.isdigit():
            value = int(char)
        elif "A" <= char <= "Z":
            value = ord(char) - 55
        else:
            return ""
        total += value * (7, 3, 1)[index % 3]
    return str(total % 10)


def _check_matches(data: str, digit: str) -> bool:
    expected = _mrz_check_digit(data)
    return bool(expected) and digit == expected


def _mrz_name(field: str) -> tuple[str, str]:
    primary, _, secondary = field.partition("<<")
    last = re.sub(r"\s+", " ", primary.replace("<", " ")).strip().title()
    first = re.sub(r"\s+", " ", secondary.replace("<", " ")).strip().title()
    return first, last


def _mrz_lines(text: str) -> list[str]:
    found = []
    for raw in text.splitlines():
        compact = re.sub(r"\s+", "", raw.upper())
        if re.fullmatch(r"[A-Z0-9<]{30}", compact) or re.fullmatch(r"[A-Z0-9<]{44}", compact):
            found.append(compact)
    return found


def _assign_name(fields: dict[str, str], first: str, last: str) -> None:
    if _plausible_name(first):
        fields["firstName"] = first
    if _plausible_name(last):
        fields["lastName"] = last


def _apply_td3(line_one: str, line_two: str, fields: dict[str, str]) -> bool:
    if len(line_one) != 44 or len(line_two) != 44 or not line_one.startswith("P"):
        return False
    if not (
        _check_matches(line_two[0:9], line_two[9])
        and _check_matches(line_two[13:19], line_two[19])
        and _check_matches(line_two[21:27], line_two[27])
    ):
        return False
    first, last = _mrz_name(line_one[5:])
    _assign_name(fields, first, last)
    fields["documentNumber"] = line_two[0:9].replace("<", "")
    nationality = line_two[10:13].replace("<", "")
    if re.fullmatch(r"[A-Z]{3}", nationality):
        fields["nationality"] = nationality
    fields["dateOfBirth"] = _mrz_date(line_two[13:19], True)
    fields["gender"] = line_two[20] if line_two[20] in {"M", "F"} else ""
    fields["expiry"] = _mrz_date(line_two[21:27], False)
    return True


def _apply_td1(line_one: str, line_two: str, line_three: str, fields: dict[str, str]) -> bool:
    if len(line_one) != 30 or len(line_two) != 30 or len(line_three) != 30:
        return False
    if not (_check_matches(line_two[0:6], line_two[6]) and _check_matches(line_two[8:14], line_two[14])):
        return False
    if _check_matches(line_one[5:14], line_one[14]):
        fields["documentNumber"] = line_one[5:14].replace("<", "")
    fields["dateOfBirth"] = _mrz_date(line_two[0:6], True)
    fields["gender"] = line_two[7] if line_two[7] in {"M", "F"} else ""
    fields["expiry"] = _mrz_date(line_two[8:14], False)
    nationality = line_two[15:18].replace("<", "")
    if re.fullmatch(r"[A-Z]{3}", nationality):
        fields["nationality"] = nationality
    first, last = _mrz_name(line_three)
    _assign_name(fields, first, last)
    return True


def _fill_mrz(text: str, fields: dict[str, str]) -> bool:
    """Accept a passport or identity-card MRZ only when its check digits match."""
    lines = _mrz_lines(text)
    for index in range(len(lines) - 1):
        if _apply_td3(lines[index], lines[index + 1], fields):
            return True
    for index in range(len(lines) - 2):
        if _apply_td1(lines[index], lines[index + 1], lines[index + 2], fields):
            return True
    return False


_TRANSLATIONS = {
    "surname", "name", "names", "given", "nom", "prenom", "prenoms", "nationality", "nationalite",
    "date", "birth", "of", "sex", "sexe", "family", "naissance",
}


def _strip_translation(value: str) -> str:
    parts = [part for part in re.split(r"[\s/|]+", value.strip()) if part]
    while parts and _fold(parts[0]) in _TRANSLATIONS:
        parts.pop(0)
    return " ".join(parts)


def _noise_word(word: str) -> bool:
    if word in _STOP:
        return True
    if word.startswith(("nation", "repub", "identit", "passeport", "passport", "naissanc")):
        return True
    return word in {"francaise", "francais", "francaie", "france", "french"} or bool(re.fullmatch(r"francai\w*", word))


def _rejected_name(value: str) -> bool:
    words = [_fold(word) for word in re.split(r"\s+", value.strip()) if word]
    return not words or any(_noise_word(word) for word in words)


def _plausible_piece(word: str) -> bool:
    folded = _fold(word)
    if folded in _PARTICLES:
        return True
    if not folded.isalpha() or not 3 <= len(folded) <= 30 or _noise_word(folded):
        return False
    if not any(char in "aeiouy" for char in folded):
        return False
    return re.search(r"[^aeiouy]{5,}", folded) is None


def _plausible_name(value: str) -> bool:
    words = [word for word in re.split(r"\s+", value.strip()) if word]
    if not 1 <= len(words) <= 4 or _rejected_name(value):
        return False
    pieces_ok = []
    for word in words:
        parts = [part for part in re.split(r"[-'’]", word) if part]
        if not parts or not all(_plausible_piece(part) for part in parts):
            return False
        pieces_ok.extend(parts)
    return any(_fold(part) not in _PARTICLES for part in pieces_ok)


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
        iso = _iso_date(text)
        if key == "dateOfBirth" and iso and not _past_birth(iso):
            return ""
        return iso
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
        cleaned = re.sub(r"\s+", " ", text)
        if not _plausible_name(cleaned):
            return ""
        return cleaned
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
        inline = _strip_translation(_raw_rest(lines[index], drop))
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
        if not _plausible_name(words[0]):
            continue
        if index + 1 >= len(lines):
            continue
        given = lines[index + 1]
        if _match_label(given)[0] or not _plausible_name(given):
            continue
        return given, words[0]
    for raw in lines:
        words = [word for word in raw.split() if word]
        if not 2 <= len(words) <= 4 or not any(character.islower() for character in raw):
            continue
        first = words[0]
        last = " ".join(words[1:])
        if _plausible_name(first) and _plausible_name(last):
            return first, last
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
