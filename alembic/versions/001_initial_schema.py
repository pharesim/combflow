"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-03-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VECTOR_DIM = 384


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── Content ───────────────────────────────────────────────────────────────
    op.create_table(
        "posts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("permlink", sa.String(), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sentiment", sa.String(length=10), nullable=True),
        sa.Column("sentiment_score", sa.Float(), nullable=True),
        sa.Column("classified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("community_id", sa.String(length=20), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("author", "permlink", name="uq_author_permlink"),
    )
    op.create_index("ix_posts_created_desc", "posts", ["created"], postgresql_using="btree", postgresql_ops={"created": "DESC"})
    op.create_index("ix_posts_community_id", "posts", ["community_id"])

    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["categories.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_categories_parent_id", "categories", ["parent_id"])

    op.create_table(
        "post_category",
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"]),
    )
    op.create_index("ix_post_category_post_id_category_id", "post_category", ["post_id", "category_id"])
    op.create_index("ix_post_category_category_id", "post_category", ["category_id"])

    op.create_table(
        "post_language",
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"]),
        sa.UniqueConstraint("post_id", "language", name="uq_post_language"),
    )
    op.create_index("ix_post_language_language", "post_language", ["language"])
    op.create_index("ix_post_language_post_id", "post_language", ["post_id"])

    # ── Centroids ─────────────────────────────────────────────────────────────
    op.create_table(
        "category_centroids",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category_name", sa.String(length=100), nullable=False),
        sa.Column("centroid", sa.Text(), nullable=False),
        sa.Column("post_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_model", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("embedding_model", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("category_name"),
    )
    op.execute(
        f"ALTER TABLE category_centroids "
        f"ALTER COLUMN centroid TYPE vector({VECTOR_DIM}) "
        f"USING centroid::vector({VECTOR_DIM})"
    )
    op.execute(
        "CREATE INDEX category_centroids_hnsw_idx "
        "ON category_centroids USING hnsw (centroid vector_cosine_ops)"
    )

    # ── Stream cursors ────────────────────────────────────────────────────────
    op.create_table(
        "stream_cursors",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("block_num", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    # ── Community mappings ──────────────────────────────────────────────────────
    op.create_table(
        "community_mappings",
        sa.Column("community_id", sa.String(length=20), nullable=False),
        sa.Column("category_slug", sa.String(length=100), nullable=True),
        sa.Column("community_name", sa.String(length=200), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("community_id"),
    )
    op.create_index("ix_community_mappings_category_slug", "community_mappings", ["category_slug"])


def downgrade() -> None:
    op.drop_table("community_mappings")
    op.drop_table("stream_cursors")
    op.drop_table("category_centroids")
    op.drop_table("post_language")
    op.drop_table("post_category")
    op.drop_index("ix_categories_parent_id", table_name="categories")
    op.drop_table("categories")
    op.drop_index("ix_posts_created_desc", table_name="posts")
    op.drop_table("posts")
    op.execute("DROP EXTENSION IF EXISTS vector")
