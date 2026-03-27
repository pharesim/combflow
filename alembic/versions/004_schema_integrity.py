"""unique constraint on post_category, NOT NULL on categories.name

Revision ID: 004
Revises: 003
Create Date: 2026-03-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Remove duplicate post_category rows (keep one per pair)
    op.execute(
        """
        DELETE FROM post_category a
        USING post_category b
        WHERE a.ctid < b.ctid
          AND a.post_id = b.post_id
          AND a.category_id = b.category_id
        """
    )

    # 2. Add UNIQUE constraint on post_category(post_id, category_id)
    op.create_unique_constraint(
        "uq_post_category", "post_category", ["post_id", "category_id"]
    )

    # 3. Fix any NULL category names (defensive — should not exist)
    op.execute(
        "UPDATE categories SET name = 'unnamed-' || id WHERE name IS NULL"
    )

    # 4. Make categories.name NOT NULL
    op.alter_column("categories", "name", nullable=False)


def downgrade() -> None:
    op.alter_column("categories", "name", nullable=True)
    op.drop_constraint("uq_post_category", "post_category", type_="unique")
