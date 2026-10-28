"""Compare the face on a selfie or liveness video with the identity document photo.

The comparison is the InsightFace check already served by ai-api. Clarity does not
invent a score when that service is absent or cannot see a face. OK and KO follow
the tenant threshold, not the provider's own verified flag.
"""

import uuid
from pathlib import Path

import httpx
from sqlalchemy.orm.attributes import flag_modified

from app.catalog import add_doc, get_doc, touch
from app.config import current_settings
from app.ids import TENANT_ID
from app.models import IdentityDocument, WorkflowInstance
from app.services import clarity_error

_IDENTITY_TYPES = {"passport", "id_card", "id-card", "drivers_license", "residence_permit"}
DEFAULT_FACE_THRESHOLD = 40


def face_threshold(db, tenant_id: str) -> int:
    row = get_doc(db, tenant_id, "face_threshold", _threshold_id(tenant_id))
    if not row:
        return DEFAULT_FACE_THRESHOLD
    return _clamp_threshold(row.payload.get("threshold"), strict=False)


def set_face_threshold(db, tenant_id: str, value) -> int:
    saved = _clamp_threshold(value, strict=True)
    doc_id = _threshold_id(tenant_id)
    row = get_doc(db, tenant_id, "face_threshold", doc_id)
    if row:
        row.payload = {"threshold": saved}
        touch(row)
    else:
        add_doc(db, tenant_id, "face_threshold", "Seuil de correspondance faciale", {"threshold": saved}, doc_id=doc_id)
    return saved


def stored_face_match(db, workflow: WorkflowInstance) -> dict | None:
    raw = (workflow.context or {}).get("faceMatch")
    if not isinstance(raw, dict):
        return None
    return _with_threshold(raw, face_threshold(db, workflow.tenant_id))


def compare_case_faces(db, workflow: WorkflowInstance, documents: list[IdentityDocument]) -> dict:
    identity = _identity_photo(documents)
    probe, source = _probe_photo(documents)
    if identity is None or probe is None:
        raise clarity_error(422, "face_missing", "Il faut une photo de pièce et un selfie ou une vidéo pour comparer les visages.")
    url = current_settings().face_api_url.strip().rstrip("/")
    if not url:
        raise clarity_error(503, "face_unavailable", "Le service de reconnaissance faciale n'est pas configuré.")
    try:
        response = httpx.post(
            f"{url}/api/v1/kyc/verify",
            files={
                "id_document": ("identity.jpg", identity, "image/jpeg"),
                "selfie_image": ("selfie.jpg", probe, "image/jpeg"),
            },
            timeout=90,
        )
    except httpx.HTTPError as exc:
        raise clarity_error(502, "face_unavailable", "La comparaison des visages n'a pas abouti.") from exc
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("detail") or body.get("message") or "")
        except ValueError:
            detail = ""
        if response.status_code == 400:
            result = _with_threshold(
                {"score": None, "source": source, "engine": "insightface", "detail": detail or "Aucun visage détecté."},
                face_threshold(db, workflow.tenant_id),
            )
            _save(workflow, result)
            return result
        raise clarity_error(502, "face_unavailable", detail or "La comparaison des visages n'a pas abouti.")
    payload = response.json()
    details = payload.get("face_verification_details") if isinstance(payload.get("face_verification_details"), dict) else {}
    score = payload.get("face_match_score")
    if score is None:
        score = details.get("confidence")
    try:
        score = None if score is None else round(float(score), 4)
    except (TypeError, ValueError):
        score = None
    result = _with_threshold(
        {"score": score, "source": source, "engine": "insightface", "detail": ""},
        face_threshold(db, workflow.tenant_id),
    )
    _save(workflow, result)
    return result


def _threshold_id(tenant_id: str) -> str:
    return str(uuid.uuid5(uuid.UUID(TENANT_ID), f"{tenant_id}:face-threshold"))


def _clamp_threshold(value, strict: bool) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = None
    if number is None or number < 0 or number > 100:
        if strict:
            raise clarity_error(422, "invalid", "Le seuil doit être un nombre entre 0 et 100.")
        return DEFAULT_FACE_THRESHOLD
    return number


def _percent(score) -> int | None:
    if score is None:
        return None
    try:
        number = float(score)
    except (TypeError, ValueError):
        return None
    if number <= 1:
        number *= 100
    return max(0, min(100, round(number)))


def _with_threshold(raw: dict, threshold: int) -> dict:
    percent = _percent(raw.get("score"))
    match = None if percent is None else percent >= threshold
    return {**raw, "match": match, "threshold": threshold}


def _save(workflow: WorkflowInstance, result: dict) -> None:
    context = dict(workflow.context or {})
    context["faceMatch"] = result
    workflow.context = context
    flag_modified(workflow, "context")


def _file_bytes(document: IdentityDocument) -> bytes | None:
    path = document.storage_path
    if not path or not Path(path).is_file():
        return None
    data = Path(path).read_bytes()
    if not data or data.lstrip().startswith(b"<") or data.lstrip().startswith(b"<?xml"):
        return None
    return data


def _identity_photo(documents: list[IdentityDocument]) -> bytes | None:
    ranked = [document for document in documents if document.doc_type in _IDENTITY_TYPES and document.status != "requested"]
    ranked.sort(key=lambda document: 0 if (document.properties or {}).get("side") == "front" else 1)
    for document in ranked:
        data = _file_bytes(document)
        if data:
            return data
    return None


def _probe_photo(documents: list[IdentityDocument]) -> tuple[bytes | None, str]:
    selfies = [document for document in documents if document.doc_type == "selfie" and document.status != "requested"]
    for document in selfies:
        data = _file_bytes(document)
        if data:
            return data, "selfie"
    videos = [document for document in documents if document.doc_type == "liveness_video" and document.status != "requested"]
    for document in videos:
        data = _file_bytes(document)
        if not data:
            continue
        frame = _video_frame(data)
        if frame:
            return frame, "video"
    return None, ""


def _video_frame(content: bytes) -> bytes | None:
    import tempfile

    import cv2

    with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
        handle.write(content)
        handle.flush()
        capture = cv2.VideoCapture(handle.name)
        if not capture.isOpened():
            return None
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 2:
            capture.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
        ok, frame = capture.read()
        capture.release()
    if not ok or frame is None:
        return None
    encoded, buffer = cv2.imencode(".jpg", frame)
    if not encoded:
        return None
    return buffer.tobytes()
