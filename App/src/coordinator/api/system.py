"""Состояние стенда в целом."""
from fastapi import APIRouter, Depends

from coordinator.api.deps import RedisDep, SessionDep, SettingsDep, require_viewer
from coordinator.api.schemas import RateLimitOut
from coordinator.storage import ratelimit

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/rate-limit", dependencies=[Depends(require_viewer)])
async def rate_limit(session: SessionDep, redis: RedisDep, settings: SettingsDep) -> RateLimitOut:
    return RateLimitOut.model_validate(await ratelimit.snapshot(session, redis, settings))
