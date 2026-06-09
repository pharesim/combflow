"""Tests for worker backfill module — _backfill_thread logic."""
import threading
from datetime import datetime, timedelta, timezone
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
        """Posts from authors with low reputation are skipped.

        Uses a heavily downvoted raw rep (-10^12 → score -2.0 under the
        canonical formula): -10^9 now maps to the 25.0 floor (proposal 102 F2),
        which would pass the gate.
        """
        mock_connect.return_value = _mock_conn([[_row(reputation=-1_000_000_000_000)], []])
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

    @patch("psycopg2.connect")
    @patch("project.worker.backfill._BACKFILL_BATCH", 2)
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks", return_value=set())
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor", return_value=None)
    @patch("project.worker.backfill.is_blacklisted", return_value=False)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_keyset_pagination_no_same_second_skip(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """Proposal 102 F8: when LIMIT bisects a same-second group, the keyset
        tuple cursor must carry (created, author, permlink) so the rest of the
        second is fetched next round instead of being skipped forever.

        The mock HONORS the keyset WHERE/LIMIT params (it filters a candidate
        corpus exactly like Postgres would), so the "no post dropped" assertion
        genuinely fails on a revert to a bare `created < cursor` filter — under
        that revert the same-second tail (alice/a here) is excluded forever.
        """
        T = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        older = T - timedelta(seconds=5)
        # A 3-post same-second group at T — bisected by the patched LIMIT=2 —
        # plus two older posts. ORDER BY created/author/permlink DESC means the
        # batch-1 tail is bob/b; the old bare-timestamp code would then skip
        # alice/a (created == T is not < T).
        corpus = [
            _row(author="carol", permlink="c", created=T),
            _row(author="bob", permlink="b", created=T),
            _row(author="alice", permlink="a", created=T),
            _row(author="zed", permlink="z", created=older),
            _row(author="yan", permlink="y", created=older),
        ]
        key = lambda r: (r["created"], r["author"], r["permlink"])

        executed = []
        state = {"batch": []}

        def run_query(sql, params=None):
            executed.append((sql, params))
            cur_created, cur_author, cur_permlink, limit = params
            bound = (cur_created, cur_author, cur_permlink)
            matches = sorted((r for r in corpus if key(r) < bound), key=key, reverse=True)
            state["batch"] = matches[:limit]

        mock_cur = MagicMock()
        mock_cur.execute.side_effect = run_query
        mock_cur.fetchall.side_effect = lambda: state["batch"]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_connect.return_value = mock_conn

        stop = threading.Event()
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)

        # No post is dropped — all five distinct posts are classified.
        classified = {
            (c.kwargs["author"], c.kwargs["permlink"])
            for c in mock_classify.call_args_list
        }
        assert classified == {
            ("carol", "c"), ("bob", "b"), ("alice", "a"),
            ("zed", "z"), ("yan", "y"),
        }

        # Lock the load-bearing SQL: the row-value comparison and the 3-column
        # DESC ordering (an ORDER-BY revert or an AND-chain rewrite must fail).
        sql0 = " ".join(executed[0][0].split())
        assert "(c.created, c.author, c.permlink) < (%s, %s, %s)" in sql0
        assert "ORDER BY c.created DESC, c.author DESC, c.permlink DESC" in sql0
        # First query seeds empty tiebreakers (== `created < NOW`).
        assert executed[0][1][1] == ""
        assert executed[0][1][2] == ""
        # Second query advances to the batch-1 tail's full keyset tuple, not the
        # bare timestamp — this is what stops alice/a from being skipped.
        assert executed[1][1][:3] == (T, "bob", "b")

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks", return_value=set())
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor", return_value=None)
    @patch("project.worker.backfill.is_blacklisted", return_value=False)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_persists_cursor_as_int_timestamp(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """Proposal 102 F8: the persisted frontier stays a plain int epoch
        (stream_cursors.block_num is an Integer column) — the keyset tuple lives
        only in memory, so no schema migration is needed."""
        T = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_connect.return_value = _mock_conn([[_row(created=T)], []])
        stop = threading.Event()
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)
        # _set_cursor(db, key, value) — value must be the int epoch, never a
        # tuple/datetime/JSON (which the integer column could not hold).
        value = mock_set_cursor.call_args.args[2]
        assert value == int(T.timestamp())
        assert isinstance(value, int)

    @patch("psycopg2.connect")
    @patch("project.worker.backfill.touch_heartbeat")
    @patch("project.worker.backfill._classify_and_save")
    @patch("project.worker.backfill._existing_author_permlinks", return_value=set())
    @patch("project.worker.backfill._set_cursor")
    @patch("project.worker.backfill._get_cursor")
    @patch("project.worker.backfill.is_blacklisted", return_value=False)
    @patch("project.worker.backfill.build_dsn", return_value="host=test dbname=test")
    @patch("project.worker.backfill.time.sleep")
    def test_catchup_reads_legacy_int_and_seeds_empty_tiebreakers(
        self, mock_sleep, mock_dsn, mock_blacklist, mock_get_cursor,
        mock_set_cursor, mock_existing, mock_classify, mock_heartbeat, mock_connect,
    ):
        """Proposal 102 F8 lock: a saved legacy-int frontier is read as the
        catch-up boundary, while the first query still seeds empty (NOW, '', '')
        tiebreakers — i.e. legacy int read as (ts, '', '') needs no migration."""
        from project.worker.backfill import _CATCHUP_BATCH
        mock_get_cursor.return_value = int(
            datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()
        )
        old = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        executed = []
        mock_cur = MagicMock()
        mock_cur.fetchall.side_effect = [[_row(created=old)], []]
        mock_cur.execute.side_effect = lambda sql, params=None: executed.append((sql, params))
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_connect.return_value = mock_conn
        stop = threading.Event()
        _backfill_thread("db", "emb", {}, 0.3, "pos", "neg", stop)
        # First query seeds empty tiebreakers regardless of the persisted frontier.
        assert executed[0][1][1] == ""
        assert executed[0][1][2] == ""
        # Catch-up mode (frontier present) uses the larger catch-up batch limit.
        assert executed[0][1][3] == _CATCHUP_BATCH
