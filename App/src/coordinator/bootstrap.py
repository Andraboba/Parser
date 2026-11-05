"""Начальные данные из окружения. Запускается один раз после миграций, а не при старте API:
иначе координатор не смог бы подняться при недоступной базе и сообщить об этом в /health."""
import asyncio

from coordinator.config import Settings
from coordinator.storage import db, tokens, users


async def bootstrap(settings: Settings) -> None:
    engine = db.make_engine(settings.database_url)
    try:
        async with db.make_sessionmaker(engine)() as session:
            for worker_name, token in tokens.parse_configured_tokens(settings.agent_tokens):
                await tokens.upsert_token(session, worker_name, token)
            await session.commit()
            if settings.admin_password:
                await users.ensure_admin(session, settings.admin_login, settings.admin_password)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(bootstrap(Settings()))
