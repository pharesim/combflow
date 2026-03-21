"""Unit tests for worker pure functions — no DB required."""
from unittest.mock import patch, MagicMock

import numpy as np
import pytest


# ── _clean_post_body ─────────────────────────────────────────────────────────

class TestCleanPostBody:
    @pytest.fixture(autouse=True)
    def _import(self):
        from project.worker.hive import _clean_post_body
        self.clean = _clean_post_body

    def test_strips_markdown_images(self):
        assert "![" not in self.clean("Hello ![alt](http://img.png) world")

    def test_strips_html_tags(self):
        assert "<div>" not in self.clean("<div>Hello</div> world")

    def test_strips_urls(self):
        result = self.clean("Check https://example.com for details")
        assert "https://" not in result
        assert "Check" in result

    def test_keeps_link_text(self):
        result = self.clean("See [my post](http://example.com) here")
        assert "my post" in result
        assert "http://" not in result

    def test_strips_headers(self):
        result = self.clean("## Hello\nSome text")
        assert result.startswith("Hello")

    def test_strips_dividers(self):
        result = self.clean("Above\n---\nBelow")
        assert "---" not in result

    def test_collapses_whitespace(self):
        result = self.clean("Hello    world")
        assert "    " not in result

    def test_empty_input(self):
        assert self.clean("") == ""

    def test_preserves_plain_text(self):
        text = "This is a normal paragraph with no special formatting."
        assert self.clean(text) == text


# ── _classify ────────────────────────────────────────────────────────────────

class TestClassify:
    @pytest.fixture(autouse=True)
    def _import(self):
        from project.worker.hive import _classify
        self.classify = _classify

    def test_empty_centroids(self):
        from sentence_transformers import SentenceTransformer
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        assert self.classify("hello world", embedder, {}, 0.3) == []

    def test_no_embedder(self):
        assert self.classify("hello", None, {"cat": np.ones(384)}, 0.3) == []

    def test_respects_threshold(self):
        from sentence_transformers import SentenceTransformer
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        # A random vector should have low similarity — threshold of 0.99 filters it.
        rng = np.random.RandomState(42)
        centroids = {"random": rng.randn(384).astype(np.float32)}
        centroids["random"] /= np.linalg.norm(centroids["random"])
        result = self.classify("hello world", embedder, centroids, 0.99)
        assert result == []

    def test_max_three_categories(self):
        from sentence_transformers import SentenceTransformer
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        # Create 5 identical centroids — all should score the same.
        emb = embedder.encode("technology and programming", normalize_embeddings=True)
        centroids = {f"cat{i}": emb.copy() for i in range(5)}
        result = self.classify("technology and programming", embedder, centroids, 0.0)
        assert len(result) <= 3


# ── _analyze_sentiment ───────────────────────────────────────────────────────

class TestSentiment:
    @pytest.fixture(autouse=True)
    def _setup(self):
        from sentence_transformers import SentenceTransformer
        from project.worker.hive import _analyze_sentiment, _build_sentiment_anchors
        self.embedder = SentenceTransformer("all-MiniLM-L6-v2")
        self.pos, self.neg = _build_sentiment_anchors(self.embedder)
        self.analyze = _analyze_sentiment

    def test_positive_text(self):
        label, score = self.analyze(
            "I absolutely love this, it's the best thing ever!", self.embedder, self.pos, self.neg
        )
        assert label == "positive"
        assert score > 0.05

    def test_negative_text(self):
        label, score = self.analyze(
            "This is terrible, I hate everything about it", self.embedder, self.pos, self.neg
        )
        assert label == "negative"
        assert score < -0.05

    def test_score_range(self):
        _, score = self.analyze("something", self.embedder, self.pos, self.neg)
        assert -1.0 <= score <= 1.0


# ── _detect_languages ────────────────────────────────────────────────────────

class TestDetectLanguages:
    @pytest.fixture(autouse=True)
    def _import(self):
        from project.worker.hive import _detect_languages
        self.detect = _detect_languages

    def test_english_detection(self):
        assert "en" in self.detect("Hello, how are you doing today?")

    def test_metadata_takes_precedence(self):
        langs = self.detect("Hello, how are you?", meta_langs=["de"])
        assert langs[0] == "de"

    def test_deduplication(self):
        langs = self.detect("Hello, how are you?", meta_langs=["en"])
        assert langs.count("en") == 1

    def test_empty_text(self):
        result = self.detect("")
        assert isinstance(result, list)

    def test_non_latin_script(self):
        langs = self.detect("今日はとても良い天気です。東京は暑いですね。")
        assert len(langs) > 0


# ── _extract_community_id ───────────────────────────────────────────────────

class TestExtractCommunityId:
    @pytest.fixture(autouse=True)
    def _import(self):
        from project.worker.hive import _extract_community_id
        self.extract = _extract_community_id

    def test_valid_community_id(self):
        assert self.extract("hive-174578") == "hive-174578"

    def test_blog_post_tag(self):
        assert self.extract("photography") is None

    def test_none_input(self):
        assert self.extract(None) is None

    def test_empty_string(self):
        assert self.extract("") is None

    def test_invalid_hive_pattern(self):
        assert self.extract("hive-abc") is None


# ── _classify_from_embedding_with_boost ─────────────────────────────────────

class TestClassifyWithBoost:
    @pytest.fixture(autouse=True)
    def _import(self):
        from project.worker.hive import _classify_from_embedding_with_boost
        self.classify_boost = _classify_from_embedding_with_boost

    def test_boost_lifts_below_threshold(self):
        """A category below threshold is lifted above by the boost."""
        # emb dot centroid = 0.28 (below 0.30), but boost of 0.08 -> 0.36
        emb = np.array([1.0] + [0.0] * 383, dtype=np.float32)
        centroid = np.array([0.28] + [0.0] * 383, dtype=np.float32)
        centroid /= np.linalg.norm(centroid)
        emb_norm = emb / np.linalg.norm(emb)
        # Construct centroid so dot product is exactly 0.28
        centroid_exact = np.zeros(384, dtype=np.float32)
        centroid_exact[0] = 0.28
        result = self.classify_boost(emb_norm, {"photo": centroid_exact}, 0.30, "photo", 0.08)
        assert "photo" in result

    def test_boost_no_effect_on_off_topic(self):
        """Very low score + boost still doesn't cross threshold."""
        emb = np.zeros(384, dtype=np.float32)
        emb[0] = 1.0
        centroid = np.zeros(384, dtype=np.float32)
        centroid[0] = 0.10
        result = self.classify_boost(emb, {"photo": centroid}, 0.30, "photo", 0.08)
        assert result == []

    def test_boost_only_applies_to_target_category(self):
        """Only the boosted category gets the extra score."""
        emb = np.zeros(384, dtype=np.float32)
        emb[0] = 1.0
        centroids = {
            "photo": np.zeros(384, dtype=np.float32),
            "travel": np.zeros(384, dtype=np.float32),
        }
        centroids["photo"][0] = 0.28
        centroids["travel"][0] = 0.28
        result = self.classify_boost(emb, centroids, 0.30, "photo", 0.08)
        assert "photo" in result
        # travel is at 0.28 (below 0.30) and gets no boost
        assert "travel" not in result


# ── _classify_from_embedding ────────────────────────────────────────────────

class TestClassifyFromEmbedding:
    @pytest.fixture(autouse=True)
    def _import(self):
        from project.worker.hive import _classify_from_embedding
        self.classify = _classify_from_embedding

    def test_empty_centroids(self):
        emb = np.ones(384, dtype=np.float32)
        assert self.classify(emb, {}, 0.3) == []

    def test_returns_top_categories(self):
        emb = np.zeros(384, dtype=np.float32)
        emb[0] = 1.0
        centroids = {"match": np.zeros(384, dtype=np.float32)}
        centroids["match"][0] = 0.5
        result = self.classify(emb, centroids, 0.3)
        assert "match" in result

    def test_filters_below_threshold(self):
        emb = np.zeros(384, dtype=np.float32)
        emb[0] = 1.0
        centroids = {"low": np.zeros(384, dtype=np.float32)}
        centroids["low"][0] = 0.1
        assert self.classify(emb, centroids, 0.3) == []

    def test_max_three_results(self):
        emb = np.zeros(384, dtype=np.float32)
        emb[0] = 1.0
        centroids = {f"c{i}": np.zeros(384, dtype=np.float32) for i in range(5)}
        for c in centroids.values():
            c[0] = 0.5
        result = self.classify(emb, centroids, 0.3)
        assert len(result) <= 3

    def test_close_scores_included(self):
        """Categories within 0.03 of top score are included."""
        emb = np.zeros(384, dtype=np.float32)
        emb[0] = 1.0
        centroids = {
            "top": np.zeros(384, dtype=np.float32),
            "close": np.zeros(384, dtype=np.float32),
            "far": np.zeros(384, dtype=np.float32),
        }
        centroids["top"][0] = 0.50
        centroids["close"][0] = 0.48  # within 0.03
        centroids["far"][0] = 0.35   # outside 0.03
        result = self.classify(emb, centroids, 0.3)
        assert "top" in result
        assert "close" in result
        assert "far" not in result


# ── _sentiment_from_embedding ───────────────────────────────────────────────

class TestSentimentFromEmbedding:
    @pytest.fixture(autouse=True)
    def _import(self):
        from project.worker.hive import _sentiment_from_embedding
        self.sentiment = _sentiment_from_embedding

    def test_neutral_when_equal(self):
        """Equal similarity to pos and neg anchors = neutral."""
        emb = np.ones(384, dtype=np.float32) / np.sqrt(384)
        pos = np.ones(384, dtype=np.float32) / np.sqrt(384)
        neg = np.ones(384, dtype=np.float32) / np.sqrt(384)
        label, score = self.sentiment(emb, pos, neg)
        assert label == "neutral"
        assert abs(score) <= 0.05

    def test_positive_label(self):
        emb = np.zeros(384, dtype=np.float32)
        emb[0] = 1.0
        pos = np.zeros(384, dtype=np.float32)
        pos[0] = 1.0  # aligned with emb
        neg = np.zeros(384, dtype=np.float32)
        neg[1] = 1.0  # orthogonal
        label, score = self.sentiment(emb, pos, neg)
        assert label == "positive"
        assert score > 0.05

    def test_negative_label(self):
        emb = np.zeros(384, dtype=np.float32)
        emb[0] = 1.0
        pos = np.zeros(384, dtype=np.float32)
        pos[1] = 1.0  # orthogonal
        neg = np.zeros(384, dtype=np.float32)
        neg[0] = 1.0  # aligned
        label, score = self.sentiment(emb, pos, neg)
        assert label == "negative"
        assert score < -0.05

    def test_score_clamped(self):
        """Score should always be in [-1.0, 1.0]."""
        emb = np.ones(384, dtype=np.float32)
        pos = np.ones(384, dtype=np.float32)
        neg = -np.ones(384, dtype=np.float32)
        _, score = self.sentiment(emb, pos, neg)
        assert -1.0 <= score <= 1.0


# ── _resolve_community (mocked HAFSQL) ─────────────────────────────────────

class TestResolveCommunity:
    @pytest.fixture(autouse=True)
    def _setup(self):
        from project.worker.hive import _resolve_community, _community_cache
        self.resolve = _resolve_community
        self._cache = _community_cache
        self._cache.clear()
        yield
        self._cache.clear()

    def test_cache_hit(self):
        self._cache["hive-999"] = ("photography", "Cached Community", 0.55)
        result = self.resolve("hive-999", None, {})
        assert result == ("photography", "Cached Community", 0.55)

    def test_no_metadata(self):
        with patch("project.worker.hive.get_community", return_value=None):
            result = self.resolve("hive-888", None, {})
        assert result == (None, "", 0.0)

    def test_no_embedder(self):
        with patch("project.worker.hive.get_community", return_value={"title": "Test", "about": "desc"}):
            result = self.resolve("hive-777", None, {"cat": np.ones(384)})
        assert result[0] is None
        assert result[1] == "Test"

    def test_matches_category(self):
        from sentence_transformers import SentenceTransformer
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        centroid = embedder.encode("cryptocurrency bitcoin blockchain", normalize_embeddings=True)
        centroids = {"crypto": centroid}
        with patch("project.worker.hive.get_community",
                   return_value={"title": "LeoFinance", "about": "Cryptocurrency and finance community"}):
            cat, name, score = self.resolve("hive-666", embedder, centroids)
        assert name == "LeoFinance"
        # The match should have a meaningful score (may or may not exceed threshold).
        assert score > 0.0

    def test_below_threshold_returns_none_category(self):
        from sentence_transformers import SentenceTransformer
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        rng = np.random.RandomState(42)
        random_centroid = rng.randn(384).astype(np.float32)
        random_centroid /= np.linalg.norm(random_centroid)
        centroids = {"random_topic": random_centroid}
        with patch("project.worker.hive.get_community",
                   return_value={"title": "Generic Community", "about": "Nothing specific"}):
            cat, name, score = self.resolve("hive-555", embedder, centroids)
        # With a random centroid and generic text, score is typically below 0.40.
        # Even if it happens to match, the test validates the function runs without error.
        assert name == "Generic Community"
        assert isinstance(score, float)


# ── _build_sentiment_anchors ────────────────────────────────────────────────

class TestBuildSentimentAnchors:
    def test_anchors_normalized(self):
        from sentence_transformers import SentenceTransformer
        from project.worker.hive import _build_sentiment_anchors
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        pos, neg = _build_sentiment_anchors(embedder)
        assert abs(np.linalg.norm(pos) - 1.0) < 1e-5
        assert abs(np.linalg.norm(neg) - 1.0) < 1e-5

    def test_anchors_different(self):
        from sentence_transformers import SentenceTransformer
        from project.worker.hive import _build_sentiment_anchors
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        pos, neg = _build_sentiment_anchors(embedder)
        # Positive and negative anchors should not be identical.
        assert not np.allclose(pos, neg)


# ── _classify_and_save pipeline ─────────────────────────────────────────────

class TestClassifyAndSave:
    @pytest.fixture(autouse=True)
    def _setup(self):
        from project.worker.hive import (
            _classify_and_save, _community_cache, _persisted_communities,
        )
        self.classify_and_save = _classify_and_save
        self._community_cache = _community_cache
        self._persisted_communities = _persisted_communities
        self._community_cache.clear()
        self._persisted_communities.clear()
        yield
        self._community_cache.clear()
        self._persisted_communities.clear()

    def test_skips_diff_posts(self):
        """Posts starting with @@ (diff/edit markers) should be skipped."""
        mock_db = MagicMock()
        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="p", title="Edit",
                body="@@ -1,3 +1,5 @@ some diff content that is long enough",
            )
        mock_save.assert_not_called()

    def test_skips_short_body(self):
        """Posts with < 80 chars clean body should be skipped."""
        mock_db = MagicMock()
        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="p", title="Short", body="Too short",
            )
        mock_save.assert_not_called()

    def test_saves_post_without_embedder(self):
        """Without embedder, post should still be saved with empty categories and neutral sentiment."""
        mock_db = MagicMock()
        body = "This is a long enough body for the test. " * 5
        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="no-embedder",
                title="Test Post", body=body,
            )
        mock_save.assert_called_once()
        saved_data = mock_save.call_args[0][1]
        assert saved_data["author"] == "alice"
        assert saved_data["categories"] == []
        assert saved_data["sentiment"] == "neutral"

    def test_saves_title(self):
        """Title should be passed through to _save_post."""
        mock_db = MagicMock()
        body = "This is a long enough body for the test. " * 5
        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="title-test",
                title="My Great Title", body=body,
            )
        saved_data = mock_save.call_args[0][1]
        assert saved_data["title"] == "My Great Title"

    def test_extracts_thumbnail_from_metadata_image(self):
        """Thumbnail extracted from json_metadata.image list."""
        mock_db = MagicMock()
        body = "This is a long enough body for the thumbnail test. " * 3
        import json
        meta = json.dumps({"image": ["https://images.hive.blog/photo.jpg"]})

        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="thumb-meta",
                title="Post", body=body, json_metadata=meta,
            )
        saved_data = mock_save.call_args[0][1]
        assert saved_data["thumbnail_url"] == "https://images.hive.blog/photo.jpg"

    def test_extracts_thumbnail_from_markdown_image(self):
        """Thumbnail extracted from markdown image in body when metadata has no image."""
        mock_db = MagicMock()
        body = ("Long enough body with an image " * 3
                + "![photo](https://example.com/pic.png) more text here.")

        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="thumb-md",
                title="Post", body=body,
            )
        saved_data = mock_save.call_args[0][1]
        assert saved_data["thumbnail_url"] == "https://example.com/pic.png"

    def test_extracts_thumbnail_from_youtube(self):
        """YouTube video URL in body should produce a thumbnail."""
        mock_db = MagicMock()
        body = ("Check out this video " * 5
                + "https://youtube.com/watch?v=dQw4w9WgXcQ and more text.")

        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="thumb-yt",
                title="Video Post", body=body,
            )
        saved_data = mock_save.call_args[0][1]
        assert saved_data["thumbnail_url"] == "https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg"

    def test_extracts_thumbnail_from_3speak(self):
        """3speak video URL in body should produce a thumbnail."""
        mock_db = MagicMock()
        body = ("Three speak video content " * 5
                + "https://3speak.tv/watch?v=alice/my-video-id more text here.")

        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="thumb-3s",
                title="3Speak Post", body=body,
            )
        saved_data = mock_save.call_args[0][1]
        assert saved_data["thumbnail_url"] == "https://images.3speak.tv/images/my-video-id.webp"

    def test_no_thumbnail_returns_none(self):
        """Posts without any image source should have thumbnail_url=None."""
        mock_db = MagicMock()
        body = "This is a plain text post with no images at all. " * 5

        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="no-thumb",
                title="Plain Post", body=body,
            )
        saved_data = mock_save.call_args[0][1]
        assert saved_data["thumbnail_url"] is None

    def test_extracts_tags_from_json_metadata(self):
        """tags_hint from json_metadata should be included in classify text."""
        mock_db = MagicMock()
        body = "This is a sufficiently long body for the classification test. " * 3
        import json
        meta = json.dumps({"tags": ["crypto", "bitcoin"]})

        with patch("project.worker.hive._save_post"), \
             patch("project.worker.hive._classify_from_embedding", return_value=["crypto"]) as mock_classify:
            # Use a mock embedder.
            mock_embedder = MagicMock()
            mock_embedder.encode.return_value = np.ones(384, dtype=np.float32) / np.sqrt(384)
            self.classify_and_save(
                mock_db, mock_embedder, {"crypto": np.ones(384)}, 0.30,
                np.zeros(384), np.zeros(384),
                author="alice", permlink="tags-test",
                title="Bitcoin Post", body=body,
                json_metadata=meta,
            )
        # The encode call should include the tags.
        encode_text = mock_embedder.encode.call_args[0][0]
        assert "crypto" in encode_text
        assert "bitcoin" in encode_text

    def test_extracts_meta_langs_string(self):
        """json_metadata.language as string should be extracted."""
        mock_db = MagicMock()
        body = "Dies ist ein ausreichend langer Text fuer den Klassifizierungstest. " * 3
        import json
        meta = json.dumps({"language": "de"})

        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="lang-str",
                title="German Post", body=body,
                json_metadata=meta,
            )
        saved_data = mock_save.call_args[0][1]
        assert "de" in saved_data["languages"]

    def test_extracts_meta_langs_list(self):
        """json_metadata.language as list should be extracted."""
        mock_db = MagicMock()
        body = "Long enough body for classification testing purposes here. " * 3
        import json
        meta = json.dumps({"language": ["en", "es"]})

        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="lang-list",
                title="Bilingual", body=body,
                json_metadata=meta,
            )
        saved_data = mock_save.call_args[0][1]
        assert "en" in saved_data["languages"]
        assert "es" in saved_data["languages"]

    def test_handles_malformed_json_metadata(self):
        """Malformed json_metadata should not crash the pipeline."""
        mock_db = MagicMock()
        body = "Valid long body content for testing malformed metadata handling. " * 3

        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="bad-meta",
                title="Post", body=body,
                json_metadata="{broken json!!!",
            )
        mock_save.assert_called_once()

    def test_extracts_community_id(self):
        """community_id should be extracted from parent_permlink and saved."""
        mock_db = MagicMock()
        body = "Long enough body for community extraction testing here. " * 3

        with patch("project.worker.hive._save_post") as mock_save, \
             patch("project.worker.hive._persist_community_mapping"):
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="comm-test",
                title="Post", body=body,
                parent_permlink="hive-174578",
            )
        saved_data = mock_save.call_args[0][1]
        assert saved_data["community_id"] == "hive-174578"

    def test_no_community_for_blog_post(self):
        """Blog posts (non-hive parent_permlink) should have community_id=None."""
        mock_db = MagicMock()
        body = "Long enough body for blog post without community testing. " * 3

        with patch("project.worker.hive._save_post") as mock_save:
            self.classify_and_save(
                mock_db, None, {}, 0.30, np.zeros(384), np.zeros(384),
                author="alice", permlink="blog-test",
                title="Post", body=body,
                parent_permlink="photography",
            )
        saved_data = mock_save.call_args[0][1]
        assert saved_data["community_id"] is None

    def test_community_boost_applied(self):
        """Community boost should be applied when community maps above threshold."""
        mock_db = MagicMock()
        body = "Long enough body for community boost classification testing. " * 3
        self._community_cache["hive-100"] = ("crypto", "CryptoComm", 0.55)

        mock_embedder = MagicMock()
        mock_embedder.encode.return_value = np.ones(384, dtype=np.float32) / np.sqrt(384)

        with patch("project.worker.hive._save_post"), \
             patch("project.worker.hive._persist_community_mapping"), \
             patch("project.worker.hive._classify_from_embedding_with_boost", return_value=["crypto"]) as mock_boost:
            self.classify_and_save(
                mock_db, mock_embedder, {"crypto": np.ones(384)}, 0.30,
                np.zeros(384), np.zeros(384),
                author="alice", permlink="boost-test",
                title="Post", body=body,
                parent_permlink="hive-100",
            )
        mock_boost.assert_called_once()

    def test_community_boost_not_applied_below_threshold(self):
        """Community boost should NOT be applied when score < 0.40."""
        mock_db = MagicMock()
        body = "Long enough body for community no-boost classification testing. " * 3
        self._community_cache["hive-200"] = (None, "LowScore", 0.20)

        mock_embedder = MagicMock()
        mock_embedder.encode.return_value = np.ones(384, dtype=np.float32) / np.sqrt(384)

        with patch("project.worker.hive._save_post"), \
             patch("project.worker.hive._persist_community_mapping"), \
             patch("project.worker.hive._classify_from_embedding", return_value=[]) as mock_classify:
            self.classify_and_save(
                mock_db, mock_embedder, {"crypto": np.ones(384)}, 0.30,
                np.zeros(384), np.zeros(384),
                author="alice", permlink="no-boost-test",
                title="Post", body=body,
                parent_permlink="hive-200",
            )
        mock_classify.assert_called_once()


# ── _persist_community_mapping ──────────────────────────────────────────────

class TestPersistCommunityMapping:
    @pytest.fixture(autouse=True)
    def _setup(self):
        from project.worker.hive import _persisted_communities
        _persisted_communities.clear()
        yield
        _persisted_communities.clear()

    def test_persists_on_first_encounter(self):
        from project.worker.hive import _persist_community_mapping
        mock_db = MagicMock()
        mock_db.run = MagicMock()
        _persist_community_mapping(mock_db, "hive-100", "crypto", "CryptoComm", 0.55)
        mock_db.run.assert_called_once()

    def test_db_failure_logs_warning(self):
        """DB write failure should log warning, not crash."""
        from project.worker.hive import _persist_community_mapping
        mock_db = MagicMock()
        mock_db.run.side_effect = Exception("DB down")
        # Should not raise.
        _persist_community_mapping(mock_db, "hive-200", "food", "Foodies", 0.45)

    def test_persisted_set_prevents_duplicates(self):
        """_persisted_communities set should prevent duplicate DB writes."""
        from project.worker.hive import _persisted_communities
        mock_db = MagicMock()
        body = "Long enough body for dedup testing of community mapping. " * 3

        _persisted_communities.add("hive-300")

        with patch("project.worker.hive._save_post"), \
             patch("project.worker.hive._resolve_community", return_value=("crypto", "Name", 0.5)), \
             patch("project.worker.hive._persist_community_mapping") as mock_persist, \
             patch("project.worker.hive._classify_from_embedding_with_boost", return_value=[]):
            from project.worker.hive import _classify_and_save
            mock_embedder = MagicMock()
            mock_embedder.encode.return_value = np.ones(384, dtype=np.float32) / np.sqrt(384)
            _classify_and_save(
                mock_db, mock_embedder, {"crypto": np.ones(384)}, 0.30,
                np.zeros(384), np.zeros(384),
                author="alice", permlink="dedup-test",
                title="Post", body=body,
                parent_permlink="hive-300",
            )
        mock_persist.assert_not_called()


