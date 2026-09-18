"""transcript to jsonb

Revision ID: b81e0f4c7d25
Revises: 4d9accb49351
Create Date: 2026-09-17

Converts the PR 5-era JSON column to JSONB on Postgres, in place and
without data loss (``transcript::jsonb`` preserves every stored row —
validated on the existing dev DB with a row carrying a real transcript
before merge). On a fresh database the initial migration creates the
column as plain JSON and this migration converts it, so both paths end
at JSONB. SQLite tests never run alembic; the model's
``JSON().with_variant(JSONB, "postgresql")`` keeps them on plain JSON.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b81e0f4c7d25"
down_revision: str | Sequence[str] | None = "4d9accb49351"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Convert ``transcript`` JSON -> JSONB, preserving all stored rows."""
    op.execute("ALTER TABLE calls ALTER COLUMN transcript TYPE JSONB USING transcript::jsonb")


def downgrade() -> None:
    """Revert to plain JSON (jsonb has no direct ::json cast; go via text)."""
    op.execute("ALTER TABLE calls ALTER COLUMN transcript TYPE JSON USING transcript::text::json")
