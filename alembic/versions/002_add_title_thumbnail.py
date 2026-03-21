"""add title and thumbnail_url to posts

Revision ID: 002
Revises: 001
Create Date: 2026-03-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("title", sa.String(), nullable=True))
    op.add_column("posts", sa.Column("thumbnail_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "thumbnail_url")
    op.drop_column("posts", "title")
