import pytest
from alembic import command
from alembic.config import Config


@pytest.mark.integration
def test_migrations_upgrade_downgrade_roundtrip() -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
