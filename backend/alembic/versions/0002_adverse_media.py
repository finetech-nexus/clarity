"""Add adverse media tables from the canonical model."""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0002_adverse_media"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_TABLES = (
    "adverse_media_sources",
    "adverse_media_articles",
    "adverse_media_article_versions",
    "adverse_media_entities",
    "adverse_media_entity_mentions",
    "adverse_media_events",
    "adverse_media_matches",
    "adverse_media_alerts",
    "adverse_media_investigations",
    "adverse_media_decisions",
    "adverse_media_collection_logs",
    "adverse_media_processing_logs",
    "adverse_media_audit_logs",
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = [Base.metadata.tables[name] for name in _TABLES]
    Base.metadata.create_all(bind=bind, tables=tables)


def downgrade() -> None:
    bind = op.get_bind()
    tables = [Base.metadata.tables[name] for name in reversed(_TABLES)]
    Base.metadata.drop_all(bind=bind, tables=tables)
