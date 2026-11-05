import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from coordinator.config import Settings
from coordinator.storage import cache, db


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.engine = db.make_engine(settings.database_url)
        app.state.redis = cache.make_redis(settings.redis_url, settings.health_timeout)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    app = FastAPI(title="Coordinator API", version="0.1.0", lifespan=lifespan)

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


async def _check(probe: Awaitable[None], limit_s: float) -> str:
    try:
        async with asyncio.timeout(limit_s):
            await probe
    except Exception as exc:  # /health сообщает о любой ошибке зависимости
        return f"error: {type(exc).__name__}"
    return "ok"
