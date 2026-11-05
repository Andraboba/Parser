"""Пользователи, роли, сессии и журнал аудита."""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.domain.security import (
    DUMMY_PASSWORD_HASH,
    Role,
    hash_password,
    verify_password,
)
from coordinator.errors import InvalidCredentials, UserConflict, UserNotFound
from coordinator.storage.models import AuditEntry, RoleRow, SessionRow, User, UserRole


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class AuthUser:
    id: int
    login: str
    role: Role
    csrf_token: str


@dataclass(frozen=True)
class UserView:
    id: int
    login: str
    role: Role
    disabled: bool


async def _set_role(session: AsyncSession, user_id: int, role: Role) -> None:
    role_id = await session.scalar(select(RoleRow.id).where(RoleRow.name == role))
    await session.execute(delete(UserRole).where(UserRole.user_id == user_id))
    await session.execute(insert(UserRole).values(user_id=user_id, role_id=role_id))


async def create_user(session: AsyncSession, login: str, password: str, role: Role) -> int:
    user_id = await session.scalar(
        pg_insert(User).values(login=login, password_hash=hash_password(password))
        .on_conflict_do_nothing(index_elements=[User.login]).returning(User.id)
    )
    if user_id is None:
        raise UserConflict(f"пользователь {login} уже существует")
    await _set_role(session, user_id, role)
    await session.commit()
    return user_id


async def ensure_admin(session: AsyncSession, login: str, password: str) -> None:
    """Начальный администратор. Существующего не трогает: пароль уже мог быть сменён."""
    user_id = await session.scalar(
        pg_insert(User).values(login=login, password_hash=hash_password(password))
        .on_conflict_do_nothing(index_elements=[User.login]).returning(User.id)
    )
    if user_id is not None:
        await _set_role(session, user_id, Role.ADMINISTRATOR)
    await session.commit()


async def _view(session: AsyncSession, condition: Any) -> list[UserView]:
    rows = await session.execute(
        select(User.id, User.login, RoleRow.name, User.disabled_at)
        .join(UserRole, UserRole.user_id == User.id)
        .join(RoleRow, RoleRow.id == UserRole.role_id)
        .where(condition).order_by(User.login)
    )
    return [
        UserView(user_id, name, Role(role), disabled is not None)
        for user_id, name, role, disabled in rows
    ]


async def list_users(session: AsyncSession) -> list[UserView]:
    return await _view(session, True)


async def get_user(session: AsyncSession, user_id: int) -> UserView:
    views = await _view(session, User.id == user_id)
    if not views:
        raise UserNotFound(f"пользователь {user_id} не найден")
    return views[0]


async def update_user(
    session: AsyncSession, user_id: int, *, role: Role | None = None,
    password: str | None = None, disabled: bool | None = None,
) -> None:
    await get_user(session, user_id)
    if role is not None:
        await _set_role(session, user_id, role)
    if password is not None:
        await session.execute(
            update(User).where(User.id == user_id).values(password_hash=hash_password(password))
        )
    if disabled is not None:
        await session.execute(update(User).where(User.id == user_id).values(
            disabled_at=func.now() if disabled else None,
        ))
    # Смена пароля, роли или отключение: старые сессии больше не должны работать.
    await session.execute(delete(SessionRow).where(SessionRow.user_id == user_id))
    await session.commit()


async def count_active_admins(session: AsyncSession) -> int:
    return await session.scalar(
        select(func.count()).select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(RoleRow, RoleRow.id == UserRole.role_id)
        .where(RoleRow.name == Role.ADMINISTRATOR, User.disabled_at.is_(None))
    ) or 0


async def login(
    session: AsyncSession, name: str, password: str, ttl_minutes: int,
) -> tuple[str, AuthUser]:
    """Возвращает токен сессии (только в cookie, в базе его хэш) и пользователя."""
    user = await session.scalar(select(User).where(User.login == name))
    stored = user.password_hash if user else DUMMY_PASSWORD_HASH
    valid = verify_password(password, stored)
    if user is None or not valid or user.disabled_at is not None:
        raise InvalidCredentials("неверный логин или пароль")
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    await session.execute(insert(SessionRow).values(
        token_hash=_hash_token(token), user_id=user.id, csrf_token=csrf,
        expires_at=func.now() + timedelta(minutes=ttl_minutes),
    ))
    await session.commit()
    role = await session.scalar(
        select(RoleRow.name).join(UserRole, UserRole.role_id == RoleRow.id)
        .where(UserRole.user_id == user.id)
    )
    return token, AuthUser(user.id, user.login, Role(role), csrf)


async def find_session(session: AsyncSession, token: str) -> AuthUser | None:
    row = (await session.execute(
        select(User.id, User.login, RoleRow.name, SessionRow.csrf_token)
        .join(SessionRow, SessionRow.user_id == User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .join(RoleRow, RoleRow.id == UserRole.role_id)
        .where(
            SessionRow.token_hash == _hash_token(token), SessionRow.expires_at > func.now(),
            User.disabled_at.is_(None),
        )
    )).one_or_none()
    return None if row is None else AuthUser(row[0], row[1], Role(row[2]), row[3])


async def logout(session: AsyncSession, token: str) -> None:
    await session.execute(delete(SessionRow).where(SessionRow.token_hash == _hash_token(token)))
    await session.commit()


async def purge_expired_sessions(session: AsyncSession) -> None:
    await session.execute(delete(SessionRow).where(SessionRow.expires_at < func.now()))
    await session.commit()


async def record_audit(
    session: AsyncSession, actor: str, action: str, object_: str | None, result: str,
    ip: str | None = None, details: dict[str, Any] | None = None,
) -> None:
    await session.execute(insert(AuditEntry).values(
        actor=actor, action=action, object=object_, result=result, ip=ip, details=details,
    ))
    await session.commit()


async def list_audit(
    session: AsyncSession, *, actor: str | None = None, action: str | None = None,
    before_id: int | None = None, limit: int = 100,
) -> list[AuditEntry]:
    stmt = select(AuditEntry)
    if actor:
        stmt = stmt.where(AuditEntry.actor == actor)
    if action:
        stmt = stmt.where(AuditEntry.action.icontains(action, autoescape=True))
    if before_id is not None:
        stmt = stmt.where(AuditEntry.id < before_id)
    return list(await session.scalars(stmt.order_by(AuditEntry.id.desc()).limit(limit)))
