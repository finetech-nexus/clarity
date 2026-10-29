"""Document storage: local disk, S3, or MinIO.

Admin configures the backend under Administration → Stockage. When object
storage is not set, files stay on the local upload directory.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse

from app.catalog import add_doc, get_doc, touch
from app.config import ROOT, current_settings

_STORAGE_KIND = "document_storage"
_PROVIDERS = {"filesystem", "s3", "minio"}


def _storage_id(tenant_id: str) -> str:
    return f"storage-{tenant_id}"


def _env_defaults() -> dict:
    endpoint = (os.getenv("AWS_S3_ENDPOINT") or os.getenv("CLARITY_S3_ENDPOINT") or "").strip()
    bucket = (os.getenv("AWS_S3_BUCKET_NAME") or os.getenv("CLARITY_S3_BUCKET") or "").strip()
    key = (os.getenv("AWS_S3_BUCKET_KEY") or os.getenv("CLARITY_S3_ACCESS_KEY") or "").strip()
    secret = (os.getenv("AWS_S3_BUCKET_SECRET") or os.getenv("CLARITY_S3_SECRET_KEY") or "").strip()
    region = (os.getenv("AWS_REGION") or os.getenv("AWS_S3_REGION") or os.getenv("CLARITY_S3_REGION") or "").strip()
    force = (os.getenv("AWS_S3_FORCE_PATH_STYLE") or os.getenv("CLARITY_S3_FORCE_PATH_STYLE") or "true").strip().lower()
    provider = (os.getenv("CLARITY_STORAGE_PROVIDER") or "").strip().lower()
    if provider not in _PROVIDERS:
        provider = "minio" if endpoint or bucket else "filesystem"
    return {
        "provider": provider,
        "bucket": bucket,
        "access_key": key,
        "secret_key": secret,
        "endpoint": endpoint,
        "region": region,
        "force_path_style": force in {"1", "true", "yes", "on"},
    }


def _normalize(payload: dict | None) -> dict:
    base = _env_defaults()
    raw = dict(payload or {})
    provider = str(raw.get("provider") or base["provider"] or "filesystem").strip().lower()
    if provider not in _PROVIDERS:
        provider = "filesystem"
    force = raw.get("force_path_style")
    if force is None:
        force = base["force_path_style"]
    return {
        "provider": provider,
        "bucket": str(raw.get("bucket") if raw.get("bucket") is not None else base["bucket"] or "").strip(),
        "access_key": str(raw.get("access_key") if raw.get("access_key") is not None else base["access_key"] or "").strip(),
        "secret_key": str(raw.get("secret_key") if raw.get("secret_key") is not None else base["secret_key"] or "").strip(),
        "endpoint": str(raw.get("endpoint") if raw.get("endpoint") is not None else base["endpoint"] or "").strip(),
        "region": str(raw.get("region") if raw.get("region") is not None else base["region"] or "").strip(),
        "force_path_style": bool(force),
    }


def storage_config(db, tenant_id: str) -> dict:
    row = get_doc(db, tenant_id, _STORAGE_KIND, _storage_id(tenant_id)) if db is not None else None
    return _normalize(row.payload if row else None)


def set_storage_config(db, tenant_id: str, payload: dict) -> dict:
    from app.services import clarity_error

    saved = _normalize(payload)
    if saved["provider"] in {"s3", "minio"}:
        if not saved["bucket"]:
            raise clarity_error(422, "invalid", "Le bucket est requis pour S3 ou MinIO.")
        if not saved["access_key"] or not saved["secret_key"]:
            raise clarity_error(422, "invalid", "La clé d'accès et le secret sont requis pour S3 ou MinIO.")
        if saved["provider"] == "minio" and not saved["endpoint"]:
            raise clarity_error(422, "invalid", "L'endpoint MinIO est requis.")
    doc_id = _storage_id(tenant_id)
    row = get_doc(db, tenant_id, _STORAGE_KIND, doc_id)
    if row:
        row.payload = saved
        touch(row)
    else:
        add_doc(db, tenant_id, _STORAGE_KIND, "Stockage des pièces", saved, doc_id=doc_id)
    return saved


def storage_view(config: dict) -> dict:
    """Admin payload: access key is shown, not masked."""
    return {
        "provider": config["provider"],
        "bucket": config["bucket"],
        "access_key": config["access_key"],
        "secret_key": config["secret_key"],
        "endpoint": config["endpoint"],
        "region": config["region"],
        "force_path_style": config["force_path_style"],
        "object_storage": config["provider"] in {"s3", "minio"} and bool(config["bucket"] and config["access_key"] and config["secret_key"]),
    }


def _upload_root(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    return Path(current_settings().upload_dir or (ROOT / "data" / "uploads"))


def _object_key(tenant_id: str, digest: str, filename: str) -> str:
    safe = Path(filename).name or "document.bin"
    return f"{tenant_id}/{digest[:16]}-{safe}"


def _s3_client(config: dict):
    try:
        import boto3
        from botocore.client import Config
    except ImportError as exc:  # pragma: no cover - installed with the API image
        from app.services import clarity_error

        raise clarity_error(503, "storage_unavailable", "Le client S3 n'est pas installé sur l'API.") from exc
    kwargs = {
        "service_name": "s3",
        "aws_access_key_id": config["access_key"],
        "aws_secret_access_key": config["secret_key"],
        "config": Config(s3={"addressing_style": "path" if config["force_path_style"] else "auto"}),
    }
    if config["region"]:
        kwargs["region_name"] = config["region"]
    if config["endpoint"]:
        kwargs["endpoint_url"] = config["endpoint"]
    return boto3.client(**kwargs)


def store_bytes(root: Path, tenant_id: str, filename: str, content: bytes, db=None) -> tuple[str, str]:
    digest = hashlib.sha256(content).hexdigest()
    safe = Path(filename).name or "evidence.bin"
    config = storage_config(db, tenant_id) if db is not None else _normalize(None)
    if config["provider"] in {"s3", "minio"} and config["bucket"] and config["access_key"] and config["secret_key"]:
        if config["provider"] == "minio" and not config["endpoint"]:
            from app.services import clarity_error

            raise clarity_error(422, "invalid", "Configurez l'endpoint MinIO avant d'envoyer un fichier.")
        key = _object_key(tenant_id, digest, safe)
        client = _s3_client(config)
        client.put_object(Bucket=config["bucket"], Key=key, Body=content)
        scheme = "minio" if config["provider"] == "minio" else "s3"
        return f"{scheme}://{config['bucket']}/{key}", digest
    folder = _upload_root(root) / tenant_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest[:16]}-{safe}"
    path.write_bytes(content)
    return str(path), digest


def is_object_uri(storage_path: str | None) -> bool:
    text = str(storage_path or "")
    return text.startswith("s3://") or text.startswith("minio://")


def read_stored_bytes(storage_path: str | None, tenant_id: str, db=None) -> bytes | None:
    if not storage_path:
        return None
    if is_object_uri(storage_path):
        parsed = urlparse(storage_path)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if not bucket or not key:
            return None
        config = storage_config(db, tenant_id) if db is not None else _normalize(None)
        if not config["bucket"]:
            config = {**config, "bucket": bucket}
        if config["provider"] == "filesystem":
            config = {**config, "provider": "minio" if storage_path.startswith("minio://") else "s3"}
        try:
            response = _s3_client(config).get_object(Bucket=bucket, Key=key)
            return response["Body"].read()
        except Exception:  # noqa: BLE001
            return None
    path = Path(storage_path)
    if path.is_file():
        return path.read_bytes()
    name = path.name
    for base in (_upload_root(), ROOT / "data" / "uploads"):
        candidate = base / tenant_id / name
        if candidate.is_file():
            return candidate.read_bytes()
    return None
