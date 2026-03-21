"""Tests for Pydantic schema validation."""
import pytest
from pydantic import ValidationError

from project.api.schemas import PostCreate


# ── PostCreate ───────────────────────────────────────────────────────────────

class TestPostCreate:
    def test_minimal_valid(self):
        p = PostCreate(author="alice", permlink="my-post")
        assert p.author == "alice"
        assert p.categories == []
        assert p.languages == []
        assert p.sentiment is None
        assert p.sentiment_score is None
        assert p.community_id is None

    def test_full_valid(self):
        p = PostCreate(
            author="alice",
            permlink="my-post",
            categories=["crypto", "finance"],
            languages=["en", "de"],
            sentiment="positive",
            sentiment_score=0.72,
            community_id="hive-174578",
        )
        assert p.sentiment == "positive"
        assert p.sentiment_score == 0.72
        assert p.community_id == "hive-174578"

    def test_author_too_long(self):
        with pytest.raises(ValidationError):
            PostCreate(author="a" * 17, permlink="ok")

    def test_permlink_too_long(self):
        with pytest.raises(ValidationError):
            PostCreate(author="alice", permlink="x" * 257)

    def test_sentiment_invalid_value(self):
        with pytest.raises(ValidationError):
            PostCreate(author="alice", permlink="p", sentiment="angry")

    def test_sentiment_valid_values(self):
        for val in ("positive", "negative", "neutral"):
            p = PostCreate(author="alice", permlink="p", sentiment=val)
            assert p.sentiment == val

    def test_sentiment_score_out_of_range_high(self):
        with pytest.raises(ValidationError):
            PostCreate(author="alice", permlink="p", sentiment_score=1.5)

    def test_sentiment_score_out_of_range_low(self):
        with pytest.raises(ValidationError):
            PostCreate(author="alice", permlink="p", sentiment_score=-1.5)

    def test_sentiment_score_boundary_values(self):
        p1 = PostCreate(author="a", permlink="p", sentiment_score=-1.0)
        assert p1.sentiment_score == -1.0
        p2 = PostCreate(author="a", permlink="p", sentiment_score=1.0)
        assert p2.sentiment_score == 1.0

    def test_community_id_too_long(self):
        with pytest.raises(ValidationError):
            PostCreate(author="alice", permlink="p", community_id="x" * 21)

    def test_community_id_none_allowed(self):
        p = PostCreate(author="alice", permlink="p", community_id=None)
        assert p.community_id is None

    def test_missing_required_author(self):
        with pytest.raises(ValidationError):
            PostCreate(permlink="p")

    def test_missing_required_permlink(self):
        with pytest.raises(ValidationError):
            PostCreate(author="alice")

    def test_categories_max_length(self):
        p = PostCreate(author="a", permlink="p", categories=["c"] * 10)
        assert len(p.categories) == 10

    def test_languages_max_length(self):
        p = PostCreate(author="a", permlink="p", languages=["en"] * 10)
        assert len(p.languages) == 10
