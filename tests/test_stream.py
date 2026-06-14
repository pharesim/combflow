"""Tests for worker stream module — _parse_op_timestamp and _process_batch."""
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call

from project.worker.stream import _parse_op_timestamp, _process_batch


# ── _parse_op_timestamp ──────────────────────────────────────────────────────

class TestParseOpTimestamp:
    def test_none_timestamp(self):
        assert _parse_op_timestamp({}) is None

    def test_empty_string_timestamp(self):
        assert _parse_op_timestamp({"timestamp": ""}) is None

    def test_iso_string_with_z(self):
        result = _parse_op_timestamp({"timestamp": "2026-03-01T12:00:00Z"})
        assert result == datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    def test_iso_string_with_offset(self):
        result = _parse_op_timestamp({"timestamp": "2026-03-01T12:00:00+00:00"})
        assert result == datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    def test_datetime_with_tzinfo(self):
        dt = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        result = _parse_op_timestamp({"timestamp": dt})
        assert result is dt

    def test_datetime_without_tzinfo(self):
        dt = datetime(2026, 3, 1, 12, 0)
        result = _parse_op_timestamp({"timestamp": dt})
        assert result.tzinfo == timezone.utc
        assert result.year == 2026

    def test_malformed_string(self):
        assert _parse_op_timestamp({"timestamp": "not-a-date"}) is None

    def test_integer_timestamp_returns_none(self):
        """Non-string, non-datetime types return None."""
        assert _parse_op_timestamp({"timestamp": 12345}) is None


# ── _process_batch ───────────────────────────────────────────────────────────

class TestProcessBatch:
    def test_empty_batch_returns_zero(self):
        assert _process_batch([], None, None, {}, 0.3, None, None, "TEST") == 0

    @patch("project.worker.stream._classify_and_save")
    @patch("project.worker.stream.get_reputations")
    @patch("project.worker.stream.check_authors")
    def test_processes_eligible_posts(self, mock_blacklist, mock_reps, mock_classify):
        mock_blacklist.return_value = set()
        mock_reps.return_value = {"alice": 50.0}
        batch = [{"author": "alice", "permlink": "p1", "title": "T", "body": "B"}]
        result = _process_batch(batch, "db", "emb", {}, 0.3, "pos", "neg", "TEST")
        assert result == 1
        mock_classify.assert_called_once()

    @patch("project.worker.stream._classify_and_save")
    @patch("project.worker.stream.get_reputations")
    @patch("project.worker.stream.check_authors")
    def test_skips_blacklisted_authors(self, mock_blacklist, mock_reps, mock_classify):
        mock_blacklist.return_value = {"spammer"}
        mock_reps.return_value = {"spammer": 50.0}
        batch = [{"author": "spammer", "permlink": "p1", "title": "T", "body": "B"}]
        result = _process_batch(batch, "db", "emb", {}, 0.3, "pos", "neg", "TEST")
        assert result == 0
        mock_classify.assert_not_called()

    @patch("project.worker.stream._classify_and_save")
    @patch("project.worker.stream.get_reputations")
    @patch("project.worker.stream.check_authors")
    def test_skips_low_reputation(self, mock_blacklist, mock_reps, mock_classify):
        mock_blacklist.return_value = set()
        mock_reps.return_value = {"lowrep": 10.0}
        batch = [{"author": "lowrep", "permlink": "p1", "title": "T", "body": "B"}]
        result = _process_batch(batch, "db", "emb", {}, 0.3, "pos", "neg", "TEST")
        assert result == 0
        mock_classify.assert_not_called()

    @patch("project.worker.stream._classify_and_save")
    @patch("project.worker.stream.get_reputations_via_api")
    @patch("project.worker.stream.get_reputations")
    @patch("project.worker.stream.check_authors")
    def test_both_rep_sources_down_skips_batch(self, mock_blacklist, mock_reps, mock_api_reps, mock_classify):
        """When both HAFSQL and Hive API are down, batch is skipped (fail-closed)."""
        mock_blacklist.return_value = set()
        mock_reps.return_value = None  # B10: HAFSQL outage sentinel
        mock_api_reps.return_value = {}  # API also unreachable
        batch = [{"author": "alice", "permlink": "p1", "title": "T", "body": "B"}]
        result = _process_batch(batch, "db", "emb", {}, 0.3, "pos", "neg", "TEST")
        assert result == 0
        mock_classify.assert_not_called()

    @patch("project.worker.stream._classify_and_save")
    @patch("project.worker.stream.get_reputations_via_api")
    @patch("project.worker.stream.get_reputations")
    @patch("project.worker.stream.check_authors")
    def test_hafsql_up_empty_does_not_fall_back(self, mock_blacklist, mock_reps, mock_api_reps, mock_classify):
        """Proposal 110 B10: an empty-but-successful HAFSQL result ({}, not None)
        means 'all new accounts', NOT an outage — so the slow API fallback must
        NOT fire, and the author falls through to the 25.0 new-account default."""
        mock_blacklist.return_value = set()
        mock_reps.return_value = {}            # HAFSQL up, no reputation rows
        batch = [{"author": "newbie", "permlink": "p1", "title": "T", "body": "B"}]
        result = _process_batch(batch, "db", "emb", {}, 0.3, "pos", "neg", "TEST")
        assert result == 1                     # classified at the 25.0 default
        mock_api_reps.assert_not_called()      # no spurious fallback

    @patch("project.worker.stream._classify_and_save")
    @patch("project.worker.stream.get_reputations_via_api")
    @patch("project.worker.stream.get_reputations")
    @patch("project.worker.stream.check_authors")
    def test_hafsql_down_api_fallback_classifies(self, mock_blacklist, mock_reps, mock_api_reps, mock_classify):
        """When HAFSQL is down but API fallback works, posts are classified."""
        mock_blacklist.return_value = set()
        mock_reps.return_value = None  # B10: HAFSQL outage sentinel triggers fallback
        mock_api_reps.return_value = {"alice": 50.0}  # API works
        batch = [{"author": "alice", "permlink": "p1", "title": "T", "body": "B"}]
        result = _process_batch(batch, "db", "emb", {}, 0.3, "pos", "neg", "TEST")
        assert result == 1
        mock_classify.assert_called_once()

    @patch("project.worker.stream._classify_and_save")
    @patch("project.worker.stream.get_reputations")
    @patch("project.worker.stream.check_authors")
    def test_classify_exception_doesnt_crash(self, mock_blacklist, mock_reps, mock_classify):
        """Exception in _classify_and_save should be caught, not propagated."""
        mock_blacklist.return_value = set()
        mock_reps.return_value = {"alice": 50.0}
        mock_classify.side_effect = Exception("classify error")
        batch = [{"author": "alice", "permlink": "p1", "title": "T", "body": "B"}]
        result = _process_batch(batch, "db", "emb", {}, 0.3, "pos", "neg", "TEST")
        assert result == 0

    @patch("project.worker.stream._classify_and_save")
    @patch("project.worker.stream.get_reputations")
    @patch("project.worker.stream.check_authors")
    def test_mixed_batch(self, mock_blacklist, mock_reps, mock_classify):
        """Batch with mix of eligible and ineligible authors."""
        mock_blacklist.return_value = {"spammer"}
        mock_reps.return_value = {"alice": 50.0, "spammer": 60.0, "lowrep": 10.0}
        batch = [
            {"author": "alice", "permlink": "p1", "title": "T", "body": "B"},
            {"author": "spammer", "permlink": "p2", "title": "T", "body": "B"},
            {"author": "lowrep", "permlink": "p3", "title": "T", "body": "B"},
        ]
        result = _process_batch(batch, "db", "emb", {}, 0.3, "pos", "neg", "TEST")
        assert result == 1  # only alice
        assert mock_classify.call_count == 1

    @patch("project.worker.stream._classify_and_save")
    @patch("project.worker.stream.get_reputations")
    @patch("project.worker.stream.check_authors")
    def test_hafsql_available_unknown_author_gets_default_rep(self, mock_blacklist, mock_reps, mock_classify):
        """When HAFSQL is reachable but author not in results, use default 25.0 rep."""
        mock_blacklist.return_value = set()
        mock_reps.return_value = {"known": 50.0}  # HAFSQL reachable (non-empty)
        batch = [{"author": "newuser", "permlink": "p1", "title": "T", "body": "B"}]
        result = _process_batch(batch, "db", "emb", {}, 0.3, "pos", "neg", "TEST")
        assert result == 1  # 25.0 >= MIN_AUTHOR_REPUTATION (20.0)
