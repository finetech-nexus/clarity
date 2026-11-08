"""Read the barcode printed on a stored identity-card photo."""

import io
import re


def barcode_result(payload: str, expected: str) -> dict:
    text = " ".join(str(payload or "").split())
    printed = str(expected or "").strip()
    digits = re.sub(r"\D", "", printed)
    payload_digits = re.sub(r"\D", "", text)
    matched = bool(text) and (not digits or digits in payload_digits or printed.upper() in text.upper())
    shown = text if len(text) <= 240 else f"{text[:240]}…"
    if not text:
        detail = "barcode not read"
        status = "incomplete"
    elif matched:
        detail = shown
        status = "passed"
    else:
        detail = f"{shown} (printed number {printed} not found)"
        status = "failed"
    return {
        "status": status,
        "value": text[:500],
        "expected": printed,
        "detail": detail,
    }


def decode_barcode(image_bytes: bytes) -> str | None:
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(image_bytes))
    image = ImageOps.exif_transpose(image) or image
    image = image.convert("RGB")
    candidates = [image, image.crop((0, int(image.height * 0.55), image.width, image.height))]
    wide = candidates[1].resize((candidates[1].width * 2, candidates[1].height * 2))
    candidates.append(wide)
    found: list[str] = []
    for candidate in candidates:
        found.extend(_zxing(candidate))
        found.extend(_opencv(candidate))
    found = [item.strip() for item in found if item and item.strip()]
    if not found:
        return None
    return max(found, key=len)


def _zxing(image) -> list[str]:
    try:
        import zxingcpp
    except ImportError:
        return []
    try:
        return [item.text for item in zxingcpp.read_barcodes(image) if getattr(item, "text", None)]
    except Exception:
        return []


def _opencv(image) -> list[str]:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []
    detector = getattr(getattr(cv2, "barcode", None), "BarcodeDetector", None)
    if detector is None:
        return []
    array = np.array(image)
    array = cv2.cvtColor(array, cv2.COLOR_RGB2BGR)
    try:
        decoded = detector().detectAndDecode(array)
    except Exception:
        return []
    info = decoded[1] if isinstance(decoded, tuple) and len(decoded) > 1 else decoded
    if isinstance(info, str):
        return [info]
    if isinstance(info, (list, tuple)):
        return [item for item in info if isinstance(item, str)]
    return []
