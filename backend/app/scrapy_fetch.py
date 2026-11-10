"""Every adverse-media download goes through one Scrapy request."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app.adverse_media import AdverseMediaError


def scrapy_get(url: str, timeout: int) -> bytes:
    if not url.startswith(("https://", "http://")):
        raise AdverseMediaError("URL refusée.")
    limit = max(3, min(int(timeout), 30))
    backend = str(Path(__file__).resolve().parent.parent)
    env = os.environ.copy()
    env["PYTHONPATH"] = backend + os.pathsep + env.get("PYTHONPATH", "")
    with tempfile.NamedTemporaryFile(prefix="clarity-scrapy-", delete=False) as handle:
        dest = handle.name
    command = [sys.executable, "-m", "app.scrapy_fetch", url, str(limit), dest]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=limit + 2, check=False, env=env)
    except subprocess.TimeoutExpired as exc:
        raise AdverseMediaError("Source indisponible : délai dépassé.") from exc
    except OSError as exc:
        raise AdverseMediaError(f"Source indisponible : {exc.__class__.__name__}") from exc
    try:
        raw = Path(dest).read_bytes() if Path(dest).exists() else b""
    finally:
        Path(dest).unlink(missing_ok=True)
    status, _, body = raw.partition(b"\n")
    try:
        code = int(status or b"0")
    except ValueError:
        code = 0
    if code == 429 or b"limit requests" in body.lower():
        raise AdverseMediaError("GDELT limite les requêtes à une toutes les 5 secondes.")
    if completed.returncode != 0 or code == 0 or code >= 400 or not body:
        if code >= 400:
            raise AdverseMediaError(f"Source indisponible : HTTP {code}.")
        detail = completed.stderr.decode("utf-8", errors="replace")[:180].strip()
        raise AdverseMediaError(detail or "Source indisponible : Scrapy.")
    return body[:750_000]


def _download(url: str, timeout: int, dest: str) -> None:
    import scrapy
    from scrapy.crawler import CrawlerProcess

    class OnePage(scrapy.Spider):
        name = "clarity_fetch"
        handle_httpstatus_all = True
        custom_settings = {
            "ROBOTSTXT_OBEY": False,
            "USER_AGENT": "ClarityAdverseMedia/1.0",
            "DOWNLOAD_TIMEOUT": timeout,
            "REDIRECT_MAX_TIMES": 3,
            "LOG_ENABLED": False,
            "TELNETCONSOLE_ENABLED": False,
            "REQUEST_FINGERPRINTER_IMPLEMENTATION": "2.7",
        }

        def __init__(self, target: str, output: str, **kwargs):
            super().__init__(**kwargs)
            self.target = target
            self.output = output

        def start_requests(self):
            yield scrapy.Request(self.target, dont_filter=True, errback=self.failed)

        def parse(self, response):
            Path(self.output).write_bytes(f"{response.status}\n".encode() + response.body[:750_000])

        def failed(self, _failure):
            Path(self.output).write_bytes(b"0\n")

    process = CrawlerProcess()
    process.crawl(OnePage, target=url, output=dest)
    process.start()


if __name__ == "__main__":
    _download(sys.argv[1], int(sys.argv[2]), sys.argv[3])
