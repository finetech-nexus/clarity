"""Compare the running API with the Marble public contracts and the Ballerine routes Clarity serves."""

import re

import yaml
from fastapi import FastAPI

from app.api.console_coverage import console_operations
from app.config import ROOT

_METHODS = ("get", "post", "put", "patch", "delete")

# Routes a Ballerine client calls. Session login, magic link and Prometheus stay on Ballerine's own auth stack.
BALLERINE_REPLACEMENT = [
    ("POST", "/api/v1/external/workflows/run"),
    ("GET", "/api/v1/external/workflows"),
    ("GET", "/api/v1/external/workflows/{runtime_id}/context"),
    ("GET", "/api/v1/external/workflows/{runtime_id}"),
    ("POST", "/api/v1/external/workflows/{runtime_id}/event"),
    ("POST", "/api/v1/external/workflows/{runtime_id}"),
    ("PATCH", "/api/v1/external/workflows/{runtime_id}"),
    ("POST", "/api/v1/external/workflows/{runtime_id}/hook/{event}"),
    ("POST", "/api/v1/external/workflows/create-token"),
    ("POST", "/api/v1/external/workflows/create-collection-flow-url"),
    ("POST", "/api/v1/external/documents"),
    ("POST", "/api/v1/external/documents/request-upload"),
    ("GET", "/api/v1/external/documents/tracker/{runtime_id}"),
    ("GET", "/api/v1/external/documents/{entity_id}/{runtime_id}"),
    ("GET", "/api/v1/external/documents/by-entity-ids/{entity_ids}/{runtime_id}"),
    ("PATCH", "/api/v1/external/documents/{document_id}"),
    ("PATCH", "/api/v1/external/documents/{document_id}/decision"),
    ("PATCH", "/api/v1/external/documents/decision/batch"),
    ("POST", "/api/v1/external/documents/{runtime_id}/{file_id}"),
    ("DELETE", "/api/v1/external/documents"),
    ("POST", "/api/v1/external/kyc"),
    ("POST", "/api/v1/external/transactions"),
    ("POST", "/api/v1/external/transactions/alt"),
    ("POST", "/api/v1/external/transactions/bulk"),
    ("POST", "/api/v1/external/transactions/alt/bulk"),
    ("GET", "/api/v1/external/transactions"),
    ("GET", "/api/v1/workflow-definition"),
    ("POST", "/api/v1/workflow-definition"),
    ("PUT", "/api/v1/workflow-definition/{definition_id}/documents-schema"),
    ("PUT", "/api/v1/workflow-definition/{definition_id}/definition"),
    ("GET", "/api/v1/collection-flow/context"),
    ("PATCH", "/api/v1/collection-flow/sync/context"),
    ("POST", "/api/v1/collection-flow/files"),
    ("GET", "/api/v1/collection-flow/files"),
    ("GET", "/api/v1/collection-flow/files/{file_id}"),
    ("POST", "/api/v1/collection-flow/final-submission"),
    ("GET", "/api/v1/collection-flow/documents"),
    ("POST", "/api/v1/collection-flow/documents"),
    ("GET", "/api/v1/collection-flow/documents/{document_id}"),
    ("PUT", "/api/v1/collection-flow/documents/{document_id}"),
    ("DELETE", "/api/v1/collection-flow/documents/{document_id}"),
    ("DELETE", "/api/v1/collection-flow/documents/{document_id}/files"),
    ("POST", "/api/v1/collection-flow/entity"),
    ("PUT", "/api/v1/collection-flow/entity/{entity_id}"),
    ("DELETE", "/api/v1/collection-flow/entity/{entity_id}"),
    ("POST", "/api/v1/collection-flow/end-user"),
    ("GET", "/api/v1/collection-flow/business/business-information"),
    ("GET", "/api/v1/case-management/workflow-definition/{definition_id}"),
    ("POST", "/api/v1/case-management"),
    ("GET", "/api/v1/case-management/transactions"),
    ("GET", "/api/v1/case-management/profiles/individuals"),
    ("GET", "/api/v1/workflow-logs/{workflow_id}"),
    ("POST", "/api/v1/external/storage"),
    ("GET", "/api/v1/internal/workflows/{runtime_id}/documents/{document_id}/run-ocr"),
    ("GET", "/api/v1/_health/live"),
    ("GET", "/api/v1/_health/ready"),
]

NOT_REPLACED = [
    "POST /api/v1/internal/auth/login",
    "POST /api/v1/internal/auth/magic-link",
    "POST /api/v1/internal/auth/logout",
    "GET /api/v1/metrics",
]


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _spec_operations(filename: str, prefix: str) -> list[tuple[str, str]]:
    document = yaml.safe_load((ROOT / "docs" / "api-compatibility" / "marble" / "original" / filename).read_text())
    found = []
    for path, item in document["paths"].items():
        for method in _METHODS:
            if method in item:
                found.append((method.upper(), prefix + path))
    return found


def served_operations(app: FastAPI) -> set[tuple[str, str]]:
    found = set()
    for path, item in app.openapi()["paths"].items():
        for method in item:
            if method in _METHODS:
                found.add((method.upper(), _norm(path)))
    return found


def _missing(expected: list[tuple[str, str]], served: set[tuple[str, str]]) -> list[str]:
    return [f"{method} {path}" for method, path in expected if (method, _norm(path)) not in served]


def replacement_report(app: FastAPI) -> dict:
    served = served_operations(app)
    marble = _spec_operations("v1.yml", "/v1") + _spec_operations("v1beta.yml", "/v1beta")
    console = [(method, path) for method, path, _operation in console_operations()]
    ballerine_missing = _missing(BALLERINE_REPLACEMENT, served)
    marble_missing = _missing(marble, served)
    console_missing = _missing(console, served)
    return {
        "marble_public": {"expected": len(marble), "missing": marble_missing, "replaces": not marble_missing},
        "marble_console": {"expected": len(console), "missing": console_missing[:12], "missing_count": len(console_missing), "replaces": not console_missing},
        "ballerine": {"expected": len(BALLERINE_REPLACEMENT), "missing": ballerine_missing, "replaces": not ballerine_missing},
        "not_replaced": NOT_REPLACED,
        "schema_identical": False,
        "note": "Les chemins publics Marble et les routes d'intégration Ballerine listées sont servis. Les schémas ne sont pas certifiés identiques, et la session Ballerine ainsi que Prometheus restent hors remplacement.",
    }
