"""denormalize category_ids and language_codes onto posts, drop junction tables

Step 1 of 2: adds the array columns (instant ADD COLUMN with DEFAULT).
The heavy backfill + index creation + junction table drop runs in
``alembic/backfill_006.py`` (autocommit batches), called from the Docker
entrypoint between ``alembic upgrade head`` and ``uvicorn``.

Revision ID: 006
Revises: 005
Create Date: 2026-04-01
"""

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS category_ids int[] NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS language_codes text[] NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE posts DROP COLUMN IF EXISTS language_codes")
    op.execute("ALTER TABLE posts DROP COLUMN IF EXISTS category_ids")
