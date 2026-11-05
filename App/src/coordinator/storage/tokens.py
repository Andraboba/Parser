"""Токены агентов: в базе только хэш, токен показывается один раз при выпуске."""
import hashlib
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.storage.models import AgentToken


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def parse_configured_tokens(raw: str) -> list[tuple[str | None, str]]:
    """Разбирает `worker-01=секрет,*=секрет`. Звёздочка — общий токен парка узлов."""
    pairs = []
    for part in filter(None, (p.strip() for p in raw.split(","))):
        name, separator, token = part.partition("=")
        if not separator or not name or not token:
            raise ValueError("ожидается формат имя=токен, через запятую")
        pairs.append((None if name == "*" else name, token))
    return pairs


async def upsert_token(session: AsyncSession, worker_name: str | None, token: str) -> None:
    stmt = insert(AgentToken).values(worker_name=worker_name, token_hash=hash_token(token))
    await session.execute(stmt.on_conflict_do_update(
        index_elements=[AgentToken.token_hash],
        set_={"worker_name": worker_name, "revoked_at": None},
    ))


async def issue(
    session: AsyncSession, worker_name: str | None, token: str, expires_in_days: int | None,
) -> AgentToken:
    row = AgentToken(
        worker_name=worker_name, token_hash=hash_token(token),
        expires_at=func.now() + timedelta(days=expires_in_days) if expires_in_days else None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def revoke(session: AsyncSession, token_id: int) -> AgentToken | None:
    row = await session.get(AgentToken, token_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = func.now()
    await session.commit()
    await session.refresh(row)
    return row


async def list_tokens(session: AsyncSession) -> list[AgentToken]:
    return list(await session.scalars(select(AgentToken).order_by(AgentToken.id.desc())))


async def find_active(session: AsyncSession, token: str) -> AgentToken | None:
    return await session.scalar(select(AgentToken).where(
        AgentToken.token_hash == hash_token(token),
        AgentToken.revoked_at.is_(None),
        (AgentToken.expires_at.is_(None)) | (AgentToken.expires_at > func.now()),
    ))
