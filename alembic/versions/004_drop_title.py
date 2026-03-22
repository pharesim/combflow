"""drop title from posts

Revision ID: 004
Revises: 003
Create Date: 2026-03-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("posts", "title")


def downgrade() -> None:
    op.add_column("posts", sa.Column("title", sa.String(), nullable=True))
