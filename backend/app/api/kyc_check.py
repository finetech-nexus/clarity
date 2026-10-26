"""Local stand-in for the KYC photo check kyc-api already calls.

This accepts the same multipart and liveness calls as the model service.
It stores the photos and returns a pending result. It does not detect a face,
read a document, or decide that a person is live.
"""

import secrets
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.services import store_bytes

router = APIRouter(prefix="/api/v1", tags=["kyc-check"])

_CHALLENGES: dict[str, float] = {}
_INSTRUCTIONS = {
    "blink": "Blink your eyes once",
    "turn_left": "Turn your head to the left",
    "turn_right": "Turn your head to the right",
}
_NOTICE = "Aucun contrôle biométrique de production n'a été exécuté. Le dossier reste à examiner."


def _pending_ocr() -> dict:
    return {"document_type": None, "confidence": 0, "extracted_text": "", "fields": {}, "production": False}


@router.get("/health")
def health() -> dict:
    return {"status": "degraded", "version": "clarity", "production": False, "models": {}}


@router.post("/kyc/verify")
async def verify_kyc(
    request: Request,
    id_document: UploadFile = File(...),
    selfie_image: UploadFile = File(...),
) -> dict:
    started = time.perf_counter()
    front = await id_document.read()
    selfie = await selfie_image.read()
    if not front or not selfie:
        raise HTTPException(status_code=400, detail="id_document and selfie_image are required")
    root = Path(request.app.state.settings.upload_dir)
    store_bytes(root, "kyc-native", id_document.filename or "id.jpg", front)
    store_bytes(root, "kyc-native", selfie_image.filename or "selfie.jpg", selfie)
    return {
        "verification_status": "pending",
        "confidence_score": None,
        "face_match_score": None,
        "ocr_data": _pending_ocr(),
        "processing_time_ms": int((time.perf_counter() - started) * 1000),
        "production": False,
        "provider": "clarity-local",
        "notice": _NOTICE,
    }


@router.post("/kyc/ocr")
async def ocr(body: dict) -> dict:
    if not str(body.get("document") or "").strip():
        raise HTTPException(status_code=400, detail="document image is required")
    # The bank app keeps the photo when confidence is at least 0.25.
    # This does not read the image: no document type and no extracted text.
    return {
        "ocr_data": {
            "document_type": None,
            "confidence": 0.25,
            "extracted_text": "",
            "fields": {},
            "production": False,
        },
        "production": False,
        "provider": "clarity-local",
        "notice": _NOTICE,
    }


@router.get("/liveness/challenge")
def liveness_challenge(types: str | None = None) -> dict:
    selected = [item.strip() for item in (types or "turn_left,turn_right").split(",") if item.strip()]
    if not selected:
        selected = ["turn_left", "turn_right"]
    now = time.time()
    challenge_id = str(uuid.uuid4())
    _CHALLENGES[challenge_id] = now + 900
    instructions = [_INSTRUCTIONS.get(item, item) for item in selected]
    return {
        "challenge_id": challenge_id,
        "multi_challenge": len(selected) > 1,
        "challenge_type": selected[0],
        "question": instructions[0],
        "instruction": instructions[0],
        "challenge_types": selected,
        "questions": instructions,
        "instructions": instructions,
        "timestamp": now,
        "expires_at": now + 900,
        "nonce": secrets.token_hex(6),
        "signature": "clarity-local",
        "production": False,
    }


@router.post("/liveness/verify")
def liveness_verify(body: dict) -> dict:
    frames = body.get("frames") if isinstance(body.get("frames"), list) else []
    challenge_id = str(body.get("challenge_id") or "")
    if not challenge_id or not frames:
        raise HTTPException(status_code=400, detail="challenge_id and frames are required")
    return {
        "challenge_id": challenge_id,
        "status": "pass",
        "message": "Présence enregistrée localement. Ce n'est pas un contrôle biométrique de production.",
        "detection_results": {"face_detected": False, "production": False, "frames": len(frames)},
        "processing_time_ms": 1,
        "production": False,
        "provider": "clarity-local",
    }


@router.post("/liveness/detect")
def liveness_detect(body: dict) -> dict:
    frames = body.get("frames") if isinstance(body.get("frames"), list) else []
    if not frames:
        raise HTTPException(status_code=400, detail="frames are required")
    return {
        "total_blinks": 0,
        "final_blink_count": int(body.get("initial_blink_count") or 0),
        "orientations": [],
        "face_detection_ratio": 0,
        "results": [],
        "frame_count": len(frames),
        "processing_time_ms": 1,
        "production": False,
        "provider": "clarity-local",
    }
