"""add calls

Revision ID: 4d9accb49351
Revises:
Create Date: 2026-09-10 16:40:15.575082

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d9accb49351"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_STATUS_WHERE = sa.text("status IN ('queued', 'joining', 'recording', 'processing')")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "calls",
        sa.Column("meeting_url", sa.Text(), nullable=False),
        sa.Column("user_ref", sa.Text(), nullable=False),
        sa.Column("consent_ack", sa.Boolean(), nullable=False),
        sa.Column(
            "platform",
            sa.Enum("google_meet", name="call_platform", native_enum=False, length=50),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "joining",
                "recording",
                "processing",
                "done",
                "failed",
                name="call_status",
                native_enum=False,
                length=50,
            ),
            nullable=False,
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column("audio_url", sa.Text(), nullable=True),
        sa.Column("transcript", sa.JSON(), nullable=True),
        sa.Column("webhook_url", sa.Text(), nullable=False),
        sa.Column("webhook_secret", sa.Text(), nullable=False),
        sa.Column("webhook_delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("webhook_attempts", sa.Integer(), nullable=False),
        sa.Column("bot_container_name", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calls")),
    )
    op.create_index(op.f("ix_calls_created_at"), "calls", ["created_at"], unique=False)
    op.create_index(
        "ix_calls_status_active",
        "calls",
        ["status"],
        unique=False,
        postgresql_where=_ACTIVE_STATUS_WHERE,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_calls_status_active",
        table_name="calls",
        postgresql_where=_ACTIVE_STATUS_WHERE,
    )
    op.drop_index(op.f("ix_calls_created_at"), table_name="calls")
    op.drop_table("calls")
