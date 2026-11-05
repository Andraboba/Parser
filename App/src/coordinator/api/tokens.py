"""Токены агентов для администратора: выпуск и отзыв, то есть ротация без остановки узла."""
import secrets
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from coordinator.api.deps import AdminDep, SessionDep
from coordinator.errors import TokenNotFound
from coordinator.storage import tokens

router = APIRouter(prefix="/api/agent-tokens", tags=["tokens"])


class TokenCreate(BaseModel):
    # Пусто — общий токен парка. Лучше выпускать отдельный на каждый узел (раздел 11 плана).
    worker_name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    expires_in_days: Annotated[int, Field(ge=1, le=3650)] | None = None


class TokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    worker_name: str | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


class TokenIssued(TokenOut):
    """Единственный момент, когда токен виден: в базе остаётся только его хэш."""

    token: str


@router.get("")
async def list_tokens(session: SessionDep, _: AdminDep) -> list[TokenOut]:
    return [TokenOut.model_validate(t) for t in await tokens.list_tokens(session)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def issue_token(body: TokenCreate, session: SessionDep, _: AdminDep) -> TokenIssued:
    value = secrets.token_hex(24)
    row = await tokens.issue(session, body.worker_name, value, body.expires_in_days)
    return TokenIssued(**TokenOut.model_validate(row).model_dump(), token=value)


@router.post("/{token_id}/revoke")
async def revoke_token(token_id: int, session: SessionDep, _: AdminDep) -> TokenOut:
    row = await tokens.revoke(session, token_id)
    if row is None:
        raise TokenNotFound(f"токен {token_id} не найден")
    return TokenOut.model_validate(row)
