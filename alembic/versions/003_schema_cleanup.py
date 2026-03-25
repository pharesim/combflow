"""indexes, CASCADE DELETE, nullable fixes

Revision ID: 003
Revises: 002
Create Date: 2026-03-25
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add missing indexes
    op.create_index("ix_posts_author", "posts", ["author"])
    op.create_index("ix_post_reports_reporter", "post_reports", ["reporter"])

    # 2. Add CASCADE DELETE on junction table foreign keys
    # post_category.post_id -> posts.id
    op.drop_constraint("post_category_post_id_fkey", "post_category", type_="foreignkey")
    op.create_foreign_key(
        "post_category_post_id_fkey", "post_category", "posts",
        ["post_id"], ["id"], ondelete="CASCADE",
    )

    # post_language.post_id -> posts.id
    op.drop_constraint("post_language_post_id_fkey", "post_language", type_="foreignkey")
    op.create_foreign_key(
        "post_language_post_id_fkey", "post_language", "posts",
        ["post_id"], ["id"], ondelete="CASCADE",
    )

    # post_reports.post_id -> posts.id
    op.drop_constraint("post_reports_post_id_fkey", "post_reports", type_="foreignkey")
    op.create_foreign_key(
        "post_reports_post_id_fkey", "post_reports", "posts",
        ["post_id"], ["id"], ondelete="CASCADE",
    )

    # 3. Fix nullable on junction table FK columns
    op.alter_column("post_category", "post_id", nullable=False)
    op.alter_column("post_category", "category_id", nullable=False)
    op.alter_column("post_language", "post_id", nullable=False)


def downgrade() -> None:
    # Reverse nullable changes
    op.alter_column("post_language", "post_id", nullable=True)
    op.alter_column("post_category", "category_id", nullable=True)
    op.alter_column("post_category", "post_id", nullable=True)

    # Revert CASCADE to simple FK
    for table, col in [
        ("post_reports", "post_id"),
        ("post_language", "post_id"),
        ("post_category", "post_id"),
    ]:
        op.drop_constraint(f"{table}_{col}_fkey", table, type_="foreignkey")
        op.create_foreign_key(f"{table}_{col}_fkey", table, "posts", [col], ["id"])

    # Drop indexes
    op.drop_index("ix_post_reports_reporter", "post_reports")
    op.drop_index("ix_posts_author", "posts")
