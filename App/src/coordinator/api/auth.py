"""Вход, выход, управление пользователями, просмотр аудита и журнала событий."""
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from coordinator.api.deps import (
    SESSION_COOKIE,
    AdminDep,
    CurrentUserDep,
    RedisDep,
    SessionDep,
    SettingsDep,
    require_viewer,
)
from coordinator.domain.security import MIN_PASSWORD_LENGTH, Role
from coordinator.errors import LastAdmin, TooManyAttempts
from coordinator.storage import events, users

router = APIRouter(prefix="/api", tags=["auth"])

_MAX_LOGIN_FAILURES = 5
_LOGIN_LOCK_SECONDS = 300

Password = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)]
Login = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")]


class LoginRequest(BaseModel):
    login: str = Field(max_length=64)
    password: str = Field(max_length=256)


class MeOut(BaseModel):
    login: str
    role: Role
    csrf_token: str


class UserCreate(BaseModel):
    login: Login
    password: Password
    role: Role


class UserPatch(BaseModel):
    role: Role | None = None
    password: Password | None = None
    disabled: bool | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    login: str
    role: Role
    disabled: bool


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    actor: str
    action: str
    object: str | None
    result: str
    ip: str | None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    level: str
    component: str
    job_id: int | None
    worker_id: int | None
    chunk_id: int | None
    external_id: int | None
    operation: str
    result: str
    error_code: str | None
    message: str | None


async def _check_throttle(redis: Redis, key: str) -> None:
    try:
        failures = await redis.get(key)
    except RedisError:
        return  # без Redis вход работает, остаётся только стоимость scrypt
    if failures is not None and int(failures) >= _MAX_LOGIN_FAILURES:
        raise TooManyAttempts("слишком много неудачных попыток входа, подождите 5 минут")


async def _note_failure(redis: Redis, key: str) -> None:
    try:
        await redis.incr(key)
        await redis.expire(key, _LOGIN_LOCK_SECONDS)
    except RedisError:
        pass


@router.post("/auth/login")
async def login(
    body: LoginRequest, request: Request, response: Response, session: SessionDep,
    settings: SettingsDep, redis: RedisDep,
) -> MeOut:
    key = f"login:failures:{body.login.lower()}"
    await _check_throttle(redis, key)
    request.state.audit_actor = body.login
    try:
        token, user = await users.login(
            session, body.login, body.password, settings.session_ttl_minutes,
        )
    except Exception:
        await _note_failure(redis, key)
        raise
    try:
        await redis.delete(key)
    except RedisError:
        pass
    response.set_cookie(
        SESSION_COOKIE, token, max_age=settings.session_ttl_minutes * 60, httponly=True,
        samesite="strict", secure=settings.cookie_secure, path="/",
    )
    return MeOut(login=user.login, role=user.role, csrf_token=user.csrf_token)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, session: SessionDep) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await users.logout(session, token)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/auth/me")
async def me(user: CurrentUserDep) -> MeOut:
    return MeOut(login=user.login, role=user.role, csrf_token=user.csrf_token)


@router.get("/users")
async def list_users(session: SessionDep, _: AdminDep) -> list[UserOut]:
    return [UserOut.model_validate(u) for u in await users.list_users(session)]


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate, session: SessionDep, _: AdminDep) -> UserOut:
    user_id = await users.create_user(session, body.login, body.password, body.role)
    return UserOut.model_validate(await users.get_user(session, user_id))


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: int, body: UserPatch, session: SessionDep, _: AdminDep,
) -> UserOut:
    target = await users.get_user(session, user_id)
    loses_admin = (
        target.role == Role.ADMINISTRATOR and not target.disabled
        and (body.disabled or (body.role is not None and body.role != Role.ADMINISTRATOR))
    )
    if loses_admin and await users.count_active_admins(session) <= 1:
        raise LastAdmin("нельзя лишить прав последнего администратора")
    await users.update_user(
        session, user_id, role=body.role, password=body.password, disabled=body.disabled,
    )
    return UserOut.model_validate(await users.get_user(session, user_id))


@router.get("/audit")
async def audit_log(
    session: SessionDep, _: AdminDep,
    actor: Annotated[str | None, Query(max_length=128)] = None,
    action: Annotated[str | None, Query(max_length=128)] = None,
    before_id: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditOut]:
    rows = await users.list_audit(
        session, actor=actor, action=action, before_id=before_id, limit=limit,
    )
    return [AuditOut.model_validate(r) for r in rows]


def _event_filter(
    job_id: int | None = None, worker_id: int | None = None, chunk_id: int | None = None,
    external_id: int | None = None, level: Annotated[str | None, Query(max_length=16)] = None,
    operation: Annotated[str | None, Query(max_length=64)] = None,
    result: Annotated[str | None, Query(max_length=32)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    since: datetime | None = None, until: datetime | None = None,
    before_id: Annotated[int | None, Query(ge=0)] = None,
) -> events.EventFilter:
    return events.EventFilter(
        job_id=job_id, worker_id=worker_id, chunk_id=chunk_id, external_id=external_id,
        level=level, operation=operation, result=result, q=q, since=since, until=until,
        before_id=before_id,
    )


@router.get("/events", dependencies=[Depends(require_viewer)])
async def list_events(
    session: SessionDep, flt: Annotated[events.EventFilter, Depends(_event_filter)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventOut]:
    return [EventOut.model_validate(e) for e in await events.search(session, flt, limit)]


@router.get("/jobs/{job_id}/events", dependencies=[Depends(require_viewer)])
async def job_events(
    job_id: int, session: SessionDep,
    flt: Annotated[events.EventFilter, Depends(_event_filter)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventOut]:
    scoped: Any = events.EventFilter(**{**flt.__dict__, "job_id": job_id})
    return [EventOut.model_validate(e) for e in await events.search(session, scoped, limit)]
