import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from app.api.ballerine import router as ballerine_router
from app.api.ballerine_studio import router as ballerine_studio_router
from app.api.clarity import router as clarity_router
from app.api.marble import v1 as marble_v1
from app.api.marble import v1beta as marble_v1beta
from app.api.marble_console import router as marble_console_router
from app.config import ROOT, Settings, get_settings
from app.db import Database
from app.engine import build_engine
from app.seed import seed_demo
from app.services import ApiFailure

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger("clarity")


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage().lower()
        if any(word in text for word in ("authorization", "x-api-key", "password", "secret", "bearer ")):
            record.msg = "redacted sensitive log record"
            record.args = ()
        return True


_logger.addFilter(_RedactFilter())


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        (ROOT / "data" / "uploads").mkdir(parents=True, exist_ok=True)
        database = Database(settings.database_url)
        database.create_all()
        engine, flowable_error = build_engine(
            settings.flowable_url,
            settings.flowable_user,
            settings.flowable_password,
            ROOT / "flowable" / "processes",
        )
        app.state.settings = settings
        app.state.database = database
        app.state.engine = engine
        app.state.flowable_error = flowable_error
        from app.config import bind_settings, reset_runtime_overlay
        from app.integrations_store import load_overlay

        reset_runtime_overlay()
        bind_settings(settings)
        session = database.session_factory()
        try:
            if settings.seed_demo:
                seed_demo(session, engine, seed_clients=settings.seed_clients)
                session.commit()
            load_overlay(session)
        finally:
            session.close()
        yield

    app = FastAPI(
        title="Clarity",
        version="0.1.0",
        summary="Plateforme bancaire KYC et AML",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(marble_v1)
    app.include_router(marble_v1beta)
    app.include_router(marble_console_router)
    app.include_router(ballerine_router)
    from app.api.kyc_check import router as kyc_check_router

    app.include_router(kyc_check_router)
    app.include_router(ballerine_studio_router)
    app.include_router(clarity_router)
    from app.api.desks import router as desks_router

    app.include_router(desks_router)
    from app.api.console_coverage import mount

    mount(app)

    @app.exception_handler(ApiFailure)
    async def _api_failure(_request, exc: ApiFailure):
        if exc.body is None:
            return Response(status_code=exc.status)
        return JSONResponse(status_code=exc.status, content=exc.body)

    return app


app = create_app()
