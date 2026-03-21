"""Shared Pydantic models used across multiple route modules."""
from datetime import datetime

from pydantic import BaseModel, Field


class PostCreate(BaseModel):
    author: str = Field(examples=["alice"], max_length=16)
    permlink: str = Field(examples=["my-bitcoin-journey-20260101"], max_length=256)
    created: datetime | None = Field(default=None, examples=["2026-01-01T12:00:00"])
    categories: list[str] = Field(default=[], examples=[["crypto", "finance"]], max_length=10)
    languages: list[str] = Field(default=[], examples=[["en", "es"]], max_length=10)
    sentiment: str | None = Field(
        default=None, examples=["positive"],
        pattern=r"^(positive|negative|neutral)$",
    )
    sentiment_score: float | None = Field(default=None, examples=[0.72], ge=-1.0, le=1.0)
    community_id: str | None = Field(default=None, examples=["hive-174578"], max_length=20)
    title: str | None = Field(default=None, examples=["My Bitcoin Journey"])
    thumbnail_url: str | None = Field(default=None, examples=["https://images.hive.blog/p/image.jpg"])
