"""Tests for worker backfill module — _backfill_thread logic."""
import threading
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import psycopg2
import psycopg2.extras
import pytest

from project.worker.backfill import _backfill_thread


def _mock_conn(rows_batches):
    """Create a mock psycopg2 connection that returns rows in sequence."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.side_effect = rows_batches
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn


def _row(author="alice", permlink="p1", reputation=1_000_000_000,
         created=None):
    """Create a test row dict matching HAFSQL query output."""
    return {
        "author": author, "permlink": permlink, "title": "T", "body": "B",
        "created": created or datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
        "json_metadata": "{}", "parent_permlink": None,
        "reputation": reputation,
    }


_COMMON_PATCHES = [
    "project.worker.backfill.touch_heartbeat",
    "project.worker.backfill._classify_and_save",
    "project.worker.backfill._existing_author_permlinks",
    "project.worker.backfill._set_cursor",
    "project.worker.backfill._get_cursor",
    "project.worker.backfill.is_blacklisted",
    "project.worker.backfill.build_dsn",
    "project.worker.backfill.time.sleep",
]


class TestBackfillThread:
    @patch("psycopg2.connect")
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks", return_value=set())
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor", return_value=None)
    @patch("project.worker.backfill.is_blacklisted", return_value=False)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_first_run_processes_posts(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """First run (no saved frontier) processes posts and sets cursor."""
        mock_connect.return_value = _mock_conn([[_row()], []])
        stop = threading.Event()
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)
        mock_classify.assert_called_once()
        mock_set_cursor.assert_called()

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks")
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor", return_value=None)
    @patch("project.worker.backfill.is_blacklisted", return_value=False)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_skips_existing_posts(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """Posts already in DB are skipped."""
        mock_existing.return_value = {("alice", "p1")}
        mock_connect.return_value = _mock_conn([[_row()], []])
        stop = threading.Event()
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)
        mock_classify.assert_not_called()

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks", return_value=set())
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor", return_value=None)
    @patch("project.worker.backfill.is_blacklisted", return_value=True)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_skips_blacklisted_authors(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """Blacklisted authors are skipped."""
        mock_connect.return_value = _mock_conn([[_row(author="spammer")], []])
        stop = threading.Event()
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)
        mock_classify.assert_not_called()

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks", return_value=set())
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor", return_value=None)
    @patch("project.worker.backfill.is_blacklisted", return_value=False)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_skips_low_reputation(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """Posts from authors with low reputation are skipped."""
        mock_connect.return_value = _mock_conn([[_row(reputation=-1_000_000_000)], []])
        stop = threading.Event()
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)
        mock_classify.assert_not_called()

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks", return_value=set())
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor", return_value=None)
    @patch("project.worker.backfill.is_blacklisted", return_value=False)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_stop_event_exits_gracefully(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """Setting stop_event should exit the backfill loop."""
        stop = threading.Event()
        call_count = [0]

        def fetchall_with_stop():
            call_count[0] += 1
            if call_count[0] > 1:
                stop.set()
                return []
            return [_row()]

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.side_effect = fetchall_with_stop
        mock_conn.cursor.return_value = mock_cur
        mock_connect.return_value = mock_conn

        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)
        assert mock_classify.call_count >= 1

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks", return_value=set())
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor")
    @patch("project.worker.backfill.is_blacklisted", return_value=False)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_catchup_phase_with_frontier(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """With a saved frontier, backfill starts in catch-up mode."""
        frontier_ts = int(datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc).timestamp())
        mock_get_cursor.return_value = frontier_ts
        old_created = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        mock_connect.return_value = _mock_conn([[_row(created=old_created)], []])
        stop = threading.Event()
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)
        mock_classify.assert_called_once()
        mock_set_cursor.assert_called()

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks", return_value=set())
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor", return_value=None)
    @patch("project.worker.backfill.is_blacklisted", return_value=False)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_classify_exception_doesnt_crash(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """Exception in _classify_and_save should be caught, not crash the thread."""
        mock_classify.side_effect = Exception("classify error")
        mock_connect.return_value = _mock_conn([[_row()], []])
        stop = threading.Event()
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.build_dsn", return_value="host=test")
    def test_connection_failure_with_stop_event(self, mock_dsn, mock_connect):
        """If stop_event is set during connection retry, thread exits."""
        stop = threading.Event()
        stop.set()
        mock_connect.side_effect = Exception("connection failed")
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks", return_value=set())
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor", return_value=None)
    @patch("project.worker.backfill.is_blacklisted", return_value=False)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_cursor_saved_after_processing(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """Cursor should be saved AFTER batch processing, not before."""
        call_order = []
        mock_classify.side_effect = lambda *a, **kw: call_order.append("classify")
        mock_set_cursor.side_effect = lambda *a, **kw: call_order.append("set_cursor")

        mock_connect.return_value = _mock_conn([[_row()], []])
        stop = threading.Event()
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)

        assert "classify" in call_order
        assert "set_cursor" in call_order
        # set_cursor must come after classify
        assert call_order.index("classify") < call_order.index("set_cursor")

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks", return_value=set())
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor", return_value=None)
    @patch("project.worker.backfill.is_blacklisted", return_value=False)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_non_datetime_created_breaks_loop(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """If created is not a datetime, the batch should be skipped (break)."""
        bad_row = _row()
        bad_row["created"] = "not-a-datetime"
        mock_connect.return_value = _mock_conn([[bad_row]])
        stop = threading.Event()
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)
        # classify should not be called because we break on non-datetime
        mock_classify.assert_not_called()

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill._get_cursor", return_value=None)
    @patch("project.worker.backfill.time.sleep")
    def test_non_transient_error_raises_after_retries(
        self, mock_sleep, mock_get_cursor, mock_dsn, mock_connect,
    ):
        """Non-transient query errors should raise after 3 retries."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.side_effect = TypeError("schema mismatch")
        mock_conn.cursor.return_value = mock_cur
        mock_connect.return_value = mock_conn

        stop = MagicMock()
        stop.is_set.return_value = False
        stop.wait.return_value = False  # Don't actually wait
        with pytest.raises(TypeError, match="schema mismatch"):
            _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)
