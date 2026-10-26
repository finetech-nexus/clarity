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
    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ocr_url: str = ""
    public_app_url: str = "http://localhost:3010"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


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


def get_settings() -> Settings:
    settings = Settings()
    if not _runtime_overlay:
        return settings
    return settings.model_copy(update=_runtime_overlay)
