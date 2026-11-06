import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from coordinator.api import auth as auth_api
from coordinator.api import containers as containers_api
from coordinator.api import jobs as jobs_api
from coordinator.api import monitoring as monitoring_api
from coordinator.api import servers as servers_api
from coordinator.api import system as system_api
from coordinator.api import terminal as terminal_api
from coordinator.api import tokens as tokens_api
from coordinator.api import videos as videos_api
from coordinator.api import workers as workers_api
from coordinator.config import Settings
from coordinator.errors import CoordinatorError
from coordinator.runner_client import RunnerClient
from coordinator.storage import cache, db, users

log = logging.getLogger("coordinator.api")


def create_app(settings: Settings | None = None, runner: RunnerClient | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.engine = db.make_engine(settings.database_url)
        app.state.sessionmaker = db.make_sessionmaker(app.state.engine)
        app.state.redis = cache.make_redis(settings.redis_url, settings.health_timeout)
        app.state.runner = runner or RunnerClient(settings.runner_url, settings.runner_token)
        try:
            yield
        finally:
            await app.state.runner.aclose()
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    app = FastAPI(title="Coordinator API", version="0.1.0", lifespan=lifespan)
    app.include_router(jobs_api.router)
    app.include_router(workers_api.router)
    app.include_router(workers_api.agent_router)
    app.include_router(system_api.router)
    app.include_router(containers_api.router)
    app.include_router(servers_api.router)
    app.include_router(auth_api.router)
    app.include_router(monitoring_api.router)
    app.include_router(tokens_api.router)
    app.include_router(terminal_api.router)
    app.include_router(videos_api.router)
    app.middleware("http")(_audit_requests)
    app.add_exception_handler(CoordinatorError, _on_coordinator_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(DBAPIError, _on_database_error)
    app.add_exception_handler(SQLAlchemyTimeoutError, _on_database_error)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        state = request.app.state
        checks = {
            "db": await _check(db.ping(state.engine), settings.health_timeout),
            "redis": await _check(cache.ping(state.redis), settings.health_timeout),
        }
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ok" if ok else "degraded", "checks": checks},
        )

    return app


# Частые служебные вызовы агентов аудит бы заполонили. Они есть в журнале событий.
_NOT_AUDITED = {
    "/api/workers/{worker_id}/heartbeat", "/api/workers/{worker_id}/lease",
    "/api/workers/{worker_id}/progress", "/api/workers/{worker_id}/complete",
}


async def _audit_requests(request: Request, call_next: Any) -> Any:
    """Журнал аудита действий пользователей и агентов (ТЗ, п. 14). Тело запроса не пишется:
    в нём могут быть пароли и токены."""
    response = await call_next(request)
    if request.method in ("GET", "HEAD", "OPTIONS") or not request.url.path.startswith("/api/"):
        return response
    route = request.scope.get("route")
    template = getattr(route, "path", request.url.path)
    if template in _NOT_AUDITED:
        return response
    params = request.scope.get("path_params") or {}
    try:
        async with request.app.state.sessionmaker() as session:
            await users.record_audit(
                session, getattr(request.state, "audit_actor", "anonymous"),
                f"{request.method} {template}",
                ", ".join(f"{k}={v}" for k, v in params.items()) or None,
                str(response.status_code),
                request.client.host if request.client else None,
            )
    except DBAPIError as exc:
        log.error("запись аудита не удалась: %s", type(exc).__name__)
    return response


async def _on_coordinator_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, CoordinatorError)
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), "code": exc.code})


async def _on_validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return JSONResponse(status_code=422, content={
        "detail": jsonable_encoder(exc.errors()), "code": "validation_error",
    })


async def _on_database_error(_: Request, exc: Exception) -> JSONResponse:
    """База недоступна или сняла транзакцию: запрос не применён, и клиент должен это знать,
    чтобы повторить. Подтверждать такой запрос нельзя (ТЗ, п. 13)."""
    log.warning("ошибка базы: %s", type(exc).__name__)
    return JSONResponse(
        status_code=503, headers={"Retry-After": "2"},
        content={"detail": "база данных временно недоступна", "code": "db_unavailable"},
    )


async def _check(probe: Awaitable[None], limit_s: float) -> str:
    try:
        async with asyncio.timeout(limit_s):
            await probe
    except Exception as exc:  # /health сообщает о любой ошибке зависимости
        return f"error: {type(exc).__name__}"
    return "ok"
