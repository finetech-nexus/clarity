from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLARITY_", extra="ignore")

    env: str = "development"
    database_url: str = f"sqlite:///{ROOT / 'data' / 'clarity.db'}"
    jwt_secret: str = "clarity-dev-secret-change-me-32b-min"
    jwt_ttl_minutes: int = 480
    flowable_url: str = ""
    flowable_user: str = "rest-admin"
    flowable_password: str = "test"
    unified_api_shared_secret: str = "clarity-dev-webhook-secret"
    upload_dir: str = str(ROOT / "data" / "uploads")
    seed_demo: bool = True
    seed_clients: bool = True
    cors_origins: str = "http://localhost:3000,http://localhost:3010"
    opensanctions_api_key: str = ""
    veriff_api_key: str = ""
    veriff_shared_secret: str = ""
    veriff_base_url: str = "https://stationapi.veriff.com"
    face_api_url: str = ""
    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ocr_url: str = ""
    public_app_url: str = "http://localhost:3010"
    # Ballerine-compatible onboarding webhook (workflowservice /hooks/onboarding).
    # Prefer this over calling core-banking directly — the webhook obtains the token and forwards.
    account_hook_url: str = ""
    # Deprecated alias kept for older manifests; used only when account_hook_url is empty.
    core_banking_hook_url: str = ""
    admin_username: str = "admin"
    admin_password: str = "password"
    # Registre National des Entreprises (WS-KYC / WS-BE). Token and subscription ids
    # are issued by the RNE and are not part of the public website.
    rne_base_url: str = "https://api.registre-entreprises.tn:8243"
    rne_token: str = ""
    rne_kyc_subscription_id: str = ""
    rne_kyc_subscriber_id: str = ""
    rne_ubo_subscription_id: str = ""
    rne_ubo_subscriber_id: str = ""
    rne_ubo_motif: str = "1"
    rne_kyc_path: str = "/WS-KYC/1"
    rne_ubo_path: str = "/WS-BE/1"
    auth_mode: str = "oidc"
    oidc_issuer: str = ""
    oidc_client_id: str = "web-app"
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_admins: str = "mifos"

    @property
    def resolved_auth_mode(self) -> str:
        return "password" if self.auth_mode.strip().lower() == "password" else "oidc"

    @property
    def oidc_admin_names(self) -> set[str]:
        return {item.strip().lower() for item in self.oidc_admins.split(",") if item.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def resolved_account_hook_url(self) -> str:
        return (self.account_hook_url or self.core_banking_hook_url or "").strip()


_runtime_overlay: dict[str, str] = {}
_bound_settings: Settings | None = None


def reset_runtime_overlay() -> None:
    global _runtime_overlay
    _runtime_overlay = {}


def bind_settings(settings: Settings) -> None:
    global _bound_settings
    _bound_settings = settings


def set_runtime_overlay(values: dict[str, str]) -> None:
    for key, value in values.items():
        if not value or key not in Settings.model_fields:
            continue
        _runtime_overlay[key] = value
        if _bound_settings is not None:
            setattr(_bound_settings, key, value)


def current_settings() -> Settings:
    return _bound_settings or get_settings()


def get_settings() -> Settings:
    settings = Settings()
    if not _runtime_overlay:
        return settings
    return settings.model_copy(update=_runtime_overlay)
