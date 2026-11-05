"""Пользователи и роли, сессии, журнал аудита только на добавление, журнал событий.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("login", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("disabled_at", TZ),
    )
    roles = op.create_table(
        "roles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(32), nullable=False, unique=True),
    )
    op.bulk_insert(roles, [{"name": "viewer"}, {"name": "operator"}, {"name": "administrator"}])
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("role_id", sa.Integer, sa.ForeignKey("roles.id", ondelete="CASCADE"),
                  primary_key=True),
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TZ, nullable=False),
    )
    op.create_index("ix_sessions_expires", "sessions", ["expires_at"])

    # Без внешнего ключа на users: удаление пользователя не должно менять записи журнала.
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("object", sa.String(255)),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("ip", sa.String(64)),
        sa.Column("details", JSONB),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.execute("""
        CREATE FUNCTION audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log: записи только добавляются';
        END
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()
    """)

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("component", sa.String(32), nullable=False),
        sa.Column("job_id", sa.Integer),
        sa.Column("worker_id", sa.Integer),
        sa.Column("chunk_id", sa.BigInteger),
        sa.Column("external_id", sa.BigInteger),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("message", sa.Text),
    )
    op.create_index("ix_events_job_at", "events", ["job_id", "at"])
    op.create_index("ix_events_at", "events", ["at"])
    op.create_index("ix_events_worker_at", "events", ["worker_id", "at"])


def downgrade() -> None:
    op.drop_table("events")
    op.execute("DROP TRIGGER audit_log_no_change ON audit_log")
    op.execute("DROP FUNCTION audit_log_append_only()")
    op.drop_table("audit_log")
    op.drop_table("sessions")
    op.drop_table("user_roles")
    op.drop_table("roles")
    op.drop_table("users")
