from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False, "timeout": 20} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args, future=True)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=True, autocommit=False, future=True)

    def create_all(self) -> None:
        from app import models  # noqa: F401

        Base.metadata.create_all(bind=self.engine)
        self._ensure_tenant_columns()
        self._ensure_field_columns()

    def _ensure_tenant_columns(self) -> None:
        dialect = self.engine.dialect.name
        with self.engine.begin() as connection:
            if dialect == "sqlite":
                rows = connection.exec_driver_sql("PRAGMA table_info(tenants)").fetchall()
                names = {row[1] for row in rows}
            elif dialect in {"postgresql", "postgres"}:
                rows = connection.exec_driver_sql(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'tenants'"
                ).fetchall()
                names = {row[0] for row in rows}
            else:
                return
            if not names:
                return
            if "kyc_validity_years" not in names:
                connection.exec_driver_sql(
                    "ALTER TABLE tenants ADD COLUMN kyc_validity_years INTEGER NOT NULL DEFAULT 1"
                )
            if "maker_checker_enforced" not in names:
                connection.exec_driver_sql(
                    "ALTER TABLE tenants ADD COLUMN maker_checker_enforced BOOLEAN NOT NULL DEFAULT TRUE"
                )

    def _ensure_field_columns(self) -> None:
        if self.engine.dialect.name != "sqlite":
            return
        with self.engine.begin() as connection:
            rows = connection.exec_driver_sql("PRAGMA table_info(data_model_fields)").fetchall()
            names = {row[1] for row in rows}
            if not names:
                return
            if "data_type" not in names:
                connection.exec_driver_sql("ALTER TABLE data_model_fields ADD COLUMN data_type VARCHAR(40) NOT NULL DEFAULT ''")
            if "description" not in names:
                connection.exec_driver_sql("ALTER TABLE data_model_fields ADD COLUMN description TEXT NOT NULL DEFAULT ''")

    def session(self) -> Iterator[Session]:
        db = self.session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
