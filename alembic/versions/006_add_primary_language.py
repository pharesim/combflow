"""add primary_language to posts

Revision ID: 006
Revises: 005
Create Date: 2026-03-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("primary_language", sa.String(10), nullable=True))
    op.create_index("ix_posts_primary_language", "posts", ["primary_language"])


def downgrade() -> None:
    op.drop_index("ix_posts_primary_language", table_name="posts")
    op.drop_column("posts", "primary_language")
