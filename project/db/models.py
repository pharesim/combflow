from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, String,
    Table, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

post_category = Table(
    "post_category",
    Base.metadata,
    Column("post_id", Integer, ForeignKey("posts.id")),
    Column("category_id", Integer, ForeignKey("categories.id")),
)

post_language = Table(
    "post_language",
    Base.metadata,
    Column("post_id", Integer, ForeignKey("posts.id")),
    Column("language", String(10), nullable=False),
    UniqueConstraint("post_id", "language", name="uq_post_language"),
)


class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True)
    author = Column(String, nullable=False)
    permlink = Column(String, nullable=False)
    created = Column(DateTime(timezone=True))  # from chain, used for sorting
    sentiment = Column(String(10))          # positive, negative, neutral
    sentiment_score = Column(Float)          # -1.0 to 1.0
    community_id = Column(String(20), nullable=True, index=True)  # hive-NNNNNN or NULL
    title = Column(String, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    classified_at = Column(DateTime(timezone=True), server_default=func.now())
    categories = relationship("Category", secondary=post_category, back_populates="posts")

    __table_args__ = (
        UniqueConstraint("author", "permlink", name="uq_author_permlink"),
    )


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)
    parent_id = Column(Integer, ForeignKey("categories.id"), index=True)
    parent = relationship("Category", remote_side=[id], backref="children")
    posts = relationship("Post", secondary=post_category, back_populates="categories")


class CategoryCentroid(Base):
    __tablename__ = "category_centroids"

    id = Column(Integer, primary_key=True)
    category_name = Column(String(100), nullable=False, unique=True)
    centroid = Column(Text, nullable=False)
    post_count = Column(Integer, nullable=False, server_default="0")
    llm_model = Column(String(200), nullable=False, server_default="")
    embedding_model = Column(String(200), nullable=False, server_default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StreamCursor(Base):
    __tablename__ = "stream_cursors"

    key = Column(String(100), primary_key=True)
    block_num = Column(Integer, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CommunityMapping(Base):
    __tablename__ = "community_mappings"

    community_id = Column(String(20), primary_key=True)
    category_slug = Column(String(100), nullable=True)
    community_name = Column(String(200), nullable=False, server_default="")
    score = Column(Float, nullable=False, server_default="0")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


