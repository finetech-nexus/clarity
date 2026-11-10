"""One-URL download through Scrapy when the plain HTTP call has already failed."""

from __future__ import annotations

import subprocess
import sys

from app.adverse_media import AdverseMediaError


def scrapy_get(url: str, timeout: int) -> bytes:
    if not url.startswith(("https://", "http://")):
        raise AdverseMediaError("URL refusée.")
    limit = max(3, min(int(timeout), 30))
    command = [
        sys.executable,
        "-m",
        "scrapy",
        "fetch",
        "--nolog",
        "-s",
        "ROBOTSTXT_OBEY=False",
        "-s",
        "USER_AGENT=ClarityAdverseMedia/1.0",
        "-s",
        f"DOWNLOAD_TIMEOUT={limit}",
        "-s",
        "REDIRECT_MAX_TIMES=3",
        url,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=limit + 2, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AdverseMediaError("Source indisponible : délai dépassé.") from exc
    except OSError as exc:
        raise AdverseMediaError(f"Source indisponible : {exc.__class__.__name__}") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise AdverseMediaError("Source indisponible : Scrapy.")
    return completed.stdout[:750_000]
