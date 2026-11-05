"""Начальные данные из окружения. Запускается один раз после миграций, а не при старте API:
иначе координатор не смог бы подняться при недоступной базе и сообщить об этом в /health."""
import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.config import Settings
from coordinator.storage import db, tokens, users
from coordinator.storage.models import Worker


def read_servers(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


async def seed_servers(
    session: AsyncSession, rows: list[dict[str, Any]], settings: Settings,
) -> None:
    """Серверы из конфигурации (ТЗ, п. 4). Уже заведённые не трогает: их мог менять оператор."""
    for row in rows:
        await session.execute(
            insert(Worker).values(
                name=row["name"], provisioning_mode="ssh", ssh_host=row["host"],
                ssh_port=row.get("ssh_port", 22), ssh_user=row.get("ssh_user", "agent"),
                ssh_key_ref=row.get("ssh_key", "ssh_client_key"), threads=row.get("threads", 4),
                storage_path=row.get("storage_path", "/data/videos"),
                min_free_space_gb=row.get("min_free_space_gb"),
                agent_kind=row.get("agent_kind", settings.agent_kind),
                desired_agent_state="running", status="offline", agent_version="",
                protocol_version=0, enabled=True,
            ).on_conflict_do_nothing(index_elements=[Worker.name])
        )
    await session.commit()


async def bootstrap(settings: Settings) -> None:
    engine = db.make_engine(settings.database_url)
    try:
        async with db.make_sessionmaker(engine)() as session:
            for worker_name, token in tokens.parse_configured_tokens(settings.agent_tokens):
                await tokens.upsert_token(session, worker_name, token)
            await session.commit()
            if settings.admin_password:
                await users.ensure_admin(session, settings.admin_login, settings.admin_password)
            if settings.seed_servers_file:
                rows = read_servers(Path(settings.seed_servers_file))
                await seed_servers(session, rows, settings)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(bootstrap(Settings()))
