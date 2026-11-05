import hmac
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.config import Settings
from coordinator.domain.security import Role, role_allows
from coordinator.errors import (
    CsrfFailed,
    NotAuthenticated,
    PermissionDenied,
    ProtocolMismatch,
    TokenForbidden,
    TokenInvalid,
    TokenRequired,
    WorkerNotFound,
)
from coordinator.storage import tokens, users
from coordinator.storage.models import AgentToken, Worker


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _redis(request: Request) -> Redis:
    return request.app.state.redis


SessionDep = Annotated[AsyncSession, Depends(_session)]
SettingsDep = Annotated[Settings, Depends(_settings)]
RedisDep = Annotated[Redis, Depends(_redis)]


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SESSION_COOKIE = "coord_session"


async def _current_user(
    request: Request, session: SessionDep,
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> users.AuthUser:
    token = request.cookies.get(SESSION_COOKIE)
    user = await users.find_session(session, token) if token else None
    if user is None:
        raise NotAuthenticated("нужно войти в систему")
    request.state.audit_actor = user.login
    # Браузер сам приложит cookie к запросу с чужого сайта, а заголовок чужой сайт поставить
    # не может и значения токена не знает (ТЗ, п. 14).
    if request.method not in _SAFE_METHODS and (
        not x_csrf_token or not hmac.compare_digest(x_csrf_token, user.csrf_token)
    ):
        raise CsrfFailed("запрос без действующего CSRF-токена")
    return user


CurrentUserDep = Annotated[users.AuthUser, Depends(_current_user)]


def _require(role: Role):
    async def check(user: CurrentUserDep) -> users.AuthUser:
        if not role_allows(user.role, role):
            raise PermissionDenied(f"нужна роль {role.value}, у вас {user.role.value}")
        return user

    return check


require_viewer = _require(Role.VIEWER)
require_operator = _require(Role.OPERATOR)
require_admin = _require(Role.ADMINISTRATOR)
AdminDep = Annotated[users.AuthUser, Depends(require_admin)]


def check_protocol(version: int, settings: Settings) -> None:
    if not settings.min_protocol_version <= version <= settings.protocol_version:
        raise ProtocolMismatch(
            f"версия протокола {version} не поддерживается, координатор принимает "
            f"{settings.min_protocol_version}..{settings.protocol_version}"
        )


async def _agent_token(
    session: SessionDep,
    settings: SettingsDep,
    x_agent_token: Annotated[str | None, Header()] = None,
    x_protocol_version: Annotated[int | None, Header()] = None,
) -> AgentToken:
    if not x_agent_token:
        raise TokenRequired("нужен заголовок X-Agent-Token")
    token = await tokens.find_active(session, x_agent_token)
    if token is None:
        raise TokenInvalid("токен агента неизвестен, отозван или истёк")
    if x_protocol_version is not None:
        check_protocol(x_protocol_version, settings)
    return token


AgentTokenDep = Annotated[AgentToken, Depends(_agent_token)]


async def _agent_worker(
    worker_id: int, token: AgentTokenDep, session: SessionDep, request: Request,
) -> Worker:
    worker = await session.get(Worker, worker_id)
    if worker is None:
        raise WorkerNotFound(f"узел {worker_id} не найден")
    if not token.allows(worker.name):
        raise TokenForbidden(f"токен не принадлежит узлу {worker.name}")
    request.state.audit_actor = f"agent:{worker.name}"
    return worker


AgentWorkerDep = Annotated[Worker, Depends(_agent_worker)]
