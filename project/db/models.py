from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String,
    Table, UniqueConstraint, func,
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
    primary_language = Column(String(10), nullable=True, index=True)
    is_nsfw = Column(Boolean, nullable=False, server_default="false")
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


class StreamCursor(Base):
    __tablename__ = "stream_cursors"

    key = Column(String(100), primary_key=True)
    block_num = Column(Integer, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PostReport(Base):
    __tablename__ = "post_reports"

    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    reporter = Column(String(16), nullable=False)
    reason = Column(String, nullable=False)
    signature = Column(String(200), nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("post_id", "reporter", name="uq_post_report_user"),
    )


class CommunityMapping(Base):
    __tablename__ = "community_mappings"

    community_id = Column(String(20), primary_key=True)
    category_slug = Column(String(100), nullable=True)
    community_name = Column(String(200), nullable=False, server_default="")
    score = Column(Float, nullable=False, server_default="0")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


