"""Unit tests for HAFSQL utility functions (no network required)."""
from unittest.mock import patch, MagicMock

import pytest

from project.hafsql import (
    _raw_rep_to_score, build_dsn, get_reputations,
    get_reputations_via_api, get_reputation_via_api, shutdown,
    get_community, get_post_body, get_post_metadata, get_post_titles,
    get_posts_titles_and_excerpts, get_top_comments, _parse_payout,
    _cursor, _get_pool,
)


class TestRawRepToScore:
    # Canonical pins (proposal 102 F2). The formula matches Hive's rep_log10:
    # 9·log10(raw) − 56, clamped at the 25.0 floor for positive raw below 10^9
    # and sign-flipped for downvoted accounts. These exact values are what the
    # approved fix produces — the proposal table's "79.55"/"−27.0" examples were
    # arithmetic slips (they used the un-clamped body and dropped the +25), so
    # the pins below intentionally differ from those illustrative numbers.
    @pytest.mark.parametrize("raw,expected", [
        (0, 25.0),                       # new account / no votes
        (1_000_000_000, 25.0),           # 10^9 — the canonical floor boundary
        (10 ** 8, 25.0),                 # below floor → clamped to 25.0 (not 16)
        (10 ** 15, 79.0),                # high reputation
        (500_000_000_000_000, 76.29),    # 5×10^14
        (-(10 ** 12), -2.0),             # downvoted → below the 20.0 ingest gate
        (-(10 ** 15), -29.0),            # heavily downvoted
    ])
    def test_canonical_values(self, raw, expected):
        assert _raw_rep_to_score(raw) == expected

    def test_returns_float(self):
        # The -> float contract must hold even on the clamped path.
        assert isinstance(_raw_rep_to_score(10 ** 8), float)

    def test_gate_excludes_downvoted_but_not_new_accounts(self):
        # The whole point of F2: a fresh/low account stays >= the 20.0 gate
        # while a heavily downvoted one drops below it.
        from project.worker.classify import MIN_AUTHOR_REPUTATION
        assert _raw_rep_to_score(10 ** 8) >= MIN_AUTHOR_REPUTATION
        assert _raw_rep_to_score(-(10 ** 12)) < MIN_AUTHOR_REPUTATION

    def test_monotonic(self):
        """Higher raw rep should give higher (or equal, while clamped) score."""
        scores = [_raw_rep_to_score(10 ** exp) for exp in range(7, 16)]
        for i in range(len(scores) - 1):
            assert scores[i] <= scores[i + 1]


class TestBuildDsn:
    def test_default_dsn(self):
        dsn = build_dsn()
        assert "host=hafsql-sql.mahdiyari.info" in dsn
        assert "port=5432" in dsn
        assert "dbname=haf_block_log" in dsn
        assert "user=hafsql_public" in dsn
        assert "password=hafsql_public" in dsn
        assert "connect_timeout=10" in dsn

    def test_custom_settings(self):
        with patch("project.hafsql.settings") as mock_settings:
            mock_settings.hafsql_host = "localhost"
            mock_settings.hafsql_port = 6543
            mock_settings.hafsql_db = "testdb"
            mock_settings.hafsql_user = "testuser"
            mock_settings.hafsql_password = "testpass"
            mock_settings.hafsql_connect_timeout = 5
            result = build_dsn()
        assert result == (
            "host=localhost port=6543 "
            "dbname=testdb user=testuser "
            "password=testpass "
            "connect_timeout=5"
        )


# ── get_reputations (batch, mocked DB) ──────────────────────────────────────

class TestGetReputations:
    def test_empty_list_returns_empty(self):
        assert get_reputations([]) == {}

    def test_returns_scores(self):
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            {"account_name": "alice", "reputation": 1_000_000_000},
            {"account_name": "bob", "reputation": 500_000_000_000_000},
        ]
        with patch("project.hafsql._cursor") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_cur
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = get_reputations(["alice", "bob"])
        assert "alice" in result
        assert "bob" in result
        assert result["bob"] > result["alice"]

    def test_returns_none_on_exception(self):
        # Proposal 110 B10: a query failure (outage) returns None, distinct from
        # a genuine empty-but-successful {} — the stream caller only fires the
        # slow API fallback on the None sentinel.
        with patch("project.hafsql._cursor", side_effect=Exception("down")):
            result = get_reputations(["alice"])
        assert result is None

    def test_skips_unparseable_reputation_row(self):
        # Proposal 110 B14: one NULL/garbage reputation skips only that row
        # instead of dropping the whole batch (the old dict-comp raised under the
        # broad except and returned {}).
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            {"account_name": "alice", "reputation": 1_000_000_000},
            {"account_name": "bob", "reputation": None},      # NULL
            {"account_name": "carol", "reputation": "garbage"},  # unparseable
        ]
        with patch("project.hafsql._cursor") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_cur
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = get_reputations(["alice", "bob", "carol"])
        assert set(result) == {"alice"}
        assert result["alice"] == 25.0


# ── get_community (mocked Hive API) ────────────────────────────────────────

class TestGetCommunity:
    def test_returns_title_about(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"title": "Photography Lovers", "about": "A community for photographers"}}
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_community("hive-174578")
        assert result["title"] == "Photography Lovers"
        assert result["about"] == "A community for photographers"

    def test_returns_none_on_miss(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": None}
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_community("hive-999999")
        assert result is None

    def test_handles_empty_fields(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"title": "", "about": ""}}
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_community("hive-123456")
        assert result["title"] == ""
        assert result["about"] == ""

    def test_handles_missing_fields(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"id": 1}}
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_community("hive-123456")
        assert result["title"] == ""
        assert result["about"] == ""


# ── _get_pool / _cursor context manager ─────────────────────────────────────

class TestCursorContextManager:
    def test_cursor_returns_and_releases(self):
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        with patch("project.hafsql._get_pool", return_value=mock_pool):
            with _cursor() as cur:
                assert cur is not None
        mock_pool.putconn.assert_called_once_with(mock_conn)

    def test_cursor_resets_statement_timeout_on_release(self):
        """Proposal 102 F6: a per-call statement_timeout is cleared before the
        connection returns to the pool, so it can't leak into a later checkout."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        reset_cur = MagicMock()
        ctx = mock_conn.cursor.return_value
        ctx.__enter__ = lambda s: reset_cur
        ctx.__exit__ = MagicMock(return_value=False)
        mock_pool.getconn.return_value = mock_conn
        with patch("project.hafsql._get_pool", return_value=mock_pool):
            with _cursor():
                pass
        reset_cur.execute.assert_called_once_with("RESET statement_timeout")
        mock_pool.putconn.assert_called_once_with(mock_conn)

    def test_cursor_release_survives_reset_failure(self):
        """If RESET fails (broken conn) the connection is still returned; the
        pool discards broken connections on its own."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        # Second cursor() call (the RESET) blows up; the first (yielded) is fine.
        good_cur = MagicMock()
        reset_ctx = MagicMock()
        reset_ctx.__enter__ = MagicMock(side_effect=Exception("conn broken"))
        mock_conn.cursor.side_effect = [good_cur, reset_ctx]
        mock_pool.getconn.return_value = mock_conn
        with patch("project.hafsql._get_pool", return_value=mock_pool):
            with _cursor() as cur:
                assert cur is good_cur
        mock_pool.putconn.assert_called_once_with(mock_conn)

    def test_cursor_propagates_caller_exception_unmasked(self):
        """Proposal 102 F6 path 3: a non-Operational error raised by the caller
        must propagate unmasked while the connection is still RESET and returned
        exactly once (without close=True) — the finally block must not swallow
        the original exception."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        reset_cur = MagicMock()
        ctx = mock_conn.cursor.return_value
        ctx.__enter__ = lambda s: reset_cur
        ctx.__exit__ = MagicMock(return_value=False)
        mock_pool.getconn.return_value = mock_conn
        with patch("project.hafsql._get_pool", return_value=mock_pool):
            with pytest.raises(ValueError, match="boom"):
                with _cursor():
                    raise ValueError("boom")
        reset_cur.execute.assert_called_once_with("RESET statement_timeout")
        mock_pool.putconn.assert_called_once_with(mock_conn)

    def test_cursor_releases_on_operational_error(self):
        import psycopg2
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = psycopg2.OperationalError("connection lost")
        mock_pool.getconn.return_value = mock_conn
        with patch("project.hafsql._get_pool", return_value=mock_pool):
            with pytest.raises(psycopg2.OperationalError):
                with _cursor():
                    pass
        mock_pool.putconn.assert_called_once_with(mock_conn, close=True)

    def test_cursor_closed_when_caller_raises(self):
        """Proposal 110 B15: the yielded cursor is closed in finally even when
        the caller raises a non-Operational exception (the old success-path
        ``cur.close()`` was skipped on that path)."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        yielded_cur = MagicMock()
        reset_ctx = MagicMock()  # the RESET statement_timeout cursor (2nd cursor() call)
        reset_ctx.__enter__ = MagicMock(return_value=MagicMock())
        reset_ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.side_effect = [yielded_cur, reset_ctx]
        mock_pool.getconn.return_value = mock_conn
        with patch("project.hafsql._get_pool", return_value=mock_pool):
            with pytest.raises(ValueError, match="boom"):
                with _cursor() as cur:
                    assert cur is yielded_cur
                    raise ValueError("boom")
        yielded_cur.close.assert_called_once()
        mock_pool.putconn.assert_called_once_with(mock_conn)

    def test_get_pool_creates_and_reuses(self):
        with patch("project.hafsql.psycopg2.pool.ThreadedConnectionPool") as mock_cls:
            mock_pool = MagicMock()
            mock_pool.closed = False
            mock_cls.return_value = mock_pool
            with patch("project.hafsql._pool", None):
                import project.hafsql as mod
                mod._pool = None
                p1 = _get_pool()
                p2 = _get_pool()
                assert p1 is p2
                mock_cls.assert_called_once()
                mod._pool = None  # cleanup


# ── get_community edge cases ────────────────────────────────────────────────

class TestGetCommunityEdgeCases:
    @pytest.mark.parametrize("side_effect,desc", [
        (Exception("down"), "generic-exception"),
        (None, "timeout"),  # handled below
        (None, "malformed-json"),  # handled below
    ], ids=["generic-exception", "timeout", "malformed-json"])
    def test_error_returns_none(self, side_effect, desc):
        import requests
        if desc == "timeout":
            side_effect = requests.Timeout("timeout")
        if desc == "malformed-json":
            mock_resp = MagicMock()
            mock_resp.json.side_effect = ValueError("bad json")
            with patch("project.hafsql.requests.post", return_value=mock_resp):
                assert get_community("hive-bad") is None
            return
        with patch("project.hafsql.requests.post", side_effect=side_effect):
            assert get_community("hive-bad") is None

    def test_none_title_coerced_to_empty(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"title": None, "about": None}}
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_community("hive-123456")
        assert result["title"] == ""
        assert result["about"] == ""

    def test_empty_result_dict(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {}}
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_community("hive-empty")
        assert result["title"] == ""
        assert result["about"] == ""


# ── get_post_body (mocked DB) ────────────────────────────────────────────

class TestGetPostBody:
    def test_returns_body_on_hit(self):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = {"body": "Hello world post body"}
        with patch("project.hafsql._cursor") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_cur
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = get_post_body("alice", "my-post")
        assert result == "Hello world post body"

    def test_returns_none_on_miss(self):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        with patch("project.hafsql._cursor") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_cur
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = get_post_body("nobody", "no-post")
        assert result is None

    def test_returns_none_on_exception(self):
        with patch("project.hafsql._cursor", side_effect=Exception("down")):
            result = get_post_body("alice", "my-post")
        assert result is None


# ── get_post_titles (mocked DB) ──────────────────────────────────────────

class TestGetPostTitles:
    def test_returns_title_map(self):
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            {"permlink": "p1", "title": "First"},
            {"permlink": "p2", "title": "Second"},
        ]
        with patch("project.hafsql._cursor") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_cur
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = get_post_titles("alice", ["p1", "p2"])
        assert result == {"p1": "First", "p2": "Second"}

    def test_drops_empty_titles(self):
        """HAFSQL stores '' for some rows; rendering an empty link is worse
        than skipping the row."""
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            {"permlink": "p1", "title": ""},
            {"permlink": "p2", "title": "Real"},
        ]
        with patch("project.hafsql._cursor") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_cur
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = get_post_titles("alice", ["p1", "p2"])
        assert result == {"p2": "Real"}

    def test_returns_empty_for_empty_permlinks(self):
        """Skip the query entirely on empty input."""
        with patch("project.hafsql._cursor") as mock_ctx:
            result = get_post_titles("alice", [])
        assert result == {}
        mock_ctx.assert_not_called()

    def test_returns_empty_on_exception(self):
        with patch("project.hafsql._cursor", side_effect=Exception("down")):
            result = get_post_titles("alice", ["p1"])
        assert result == {}


class TestGetPostsTitlesAndExcerpts:
    def test_returns_keyed_by_author_permlink(self):
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            {"author": "alice", "permlink": "p1", "title": "First", "body": "Body one"},
            {"author": "bob", "permlink": "p2", "title": "Second", "body": "Body two"},
        ]
        with patch("project.hafsql._cursor") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_cur
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = get_posts_titles_and_excerpts([("alice", "p1"), ("bob", "p2")])
        assert result == {
            ("alice", "p1"): {"title": "First", "body": "Body one"},
            ("bob", "p2"): {"title": "Second", "body": "Body two"},
        }

    def test_drops_empty_titles(self):
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            {"author": "alice", "permlink": "p1", "title": "", "body": "x"},
            {"author": "bob", "permlink": "p2", "title": "Real", "body": "y"},
        ]
        with patch("project.hafsql._cursor") as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: mock_cur
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = get_posts_titles_and_excerpts([("alice", "p1"), ("bob", "p2")])
        assert result == {("bob", "p2"): {"title": "Real", "body": "y"}}

    def test_returns_empty_for_empty_pairs(self):
        with patch("project.hafsql._cursor") as mock_ctx:
            result = get_posts_titles_and_excerpts([])
        assert result == {}
        mock_ctx.assert_not_called()

    def test_returns_empty_on_exception(self):
        with patch("project.hafsql._cursor", side_effect=Exception("down")):
            result = get_posts_titles_and_excerpts([("alice", "p1")])
        assert result == {}


# ── get_post_metadata (mocked HTTP) ──────────────────────────────────────

class TestGetPostMetadata:
    def test_returns_metadata_on_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "title": "My Post Title",
                "body": "Some body text here that is long enough to be useful.",
                "json_metadata": {
                    "description": "A custom description",
                    "image": ["https://example.com/img.jpg"],
                },
            }
        }
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_post_metadata("alice", "my-post")
        assert result is not None
        assert result["title"] == "My Post Title"
        assert result["description"] == "A custom description"
        assert result["image"] == "https://example.com/img.jpg"

    def test_falls_back_to_body_when_no_description(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "title": "Title",
                "body": "This is the body content of the post.",
                "json_metadata": {"image": []},
            }
        }
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_post_metadata("alice", "my-post")
        assert result is not None
        assert result["description"] == "This is the body content of the post."
        assert result["image"] == ""

    def test_returns_none_when_result_is_null(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": None}
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_post_metadata("alice", "nonexistent")
        assert result is None

    def test_returns_none_on_network_error(self):
        with patch("project.hafsql.requests.post", side_effect=Exception("timeout")):
            result = get_post_metadata("alice", "my-post")
        assert result is None

    def test_handles_string_json_metadata(self):
        import json
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "title": "Title",
                "body": "Body",
                "json_metadata": json.dumps({
                    "description": "From string meta",
                    "image": ["https://example.com/pic.png"],
                }),
            }
        }
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_post_metadata("alice", "my-post")
        assert result["description"] == "From string meta"
        assert result["image"] == "https://example.com/pic.png"

    def test_handles_empty_json_metadata(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "title": "",
                "body": "",
                "json_metadata": {},
            }
        }
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_post_metadata("alice", "my-post")
        assert result is not None
        assert result["title"] == ""
        assert result["description"] == ""
        assert result["image"] == ""


# ── get_reputations_via_api (mocked HTTP) ──────────────────────────────────

class TestGetReputationsViaApi:
    def test_empty_list_returns_empty(self):
        assert get_reputations_via_api([]) == {}

    def test_returns_scores(self):
        # bridge.get_profile returns pre-computed scores, one call per author
        responses = {
            "alice": MagicMock(json=MagicMock(return_value={"result": {"reputation": 30.5}})),
            "bob": MagicMock(json=MagicMock(return_value={"result": {"reputation": 65.2}})),
        }
        def side_effect(*args, **kwargs):
            account = kwargs.get("json", args[1] if len(args) > 1 else {}).get("params", {}).get("account")
            return responses[account]
        with patch("project.hafsql.requests.post", side_effect=side_effect):
            result = get_reputations_via_api(["alice", "bob"])
        assert result["alice"] == 30.5
        assert result["bob"] == 65.2
        assert result["bob"] > result["alice"]

    def test_failover_to_second_node(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"reputation": 45.0}}
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("first node down")
            return mock_resp
        with patch("project.hafsql.requests.post", side_effect=side_effect):
            result = get_reputations_via_api(["alice"])
        assert result["alice"] == 45.0

    def test_all_nodes_fail_returns_empty(self):
        with patch("project.hafsql.requests.post", side_effect=Exception("all down")):
            result = get_reputations_via_api(["alice"])
        assert result == {}

    def test_uses_short_timeout(self):
        """Proposal 102 F1: per-node timeout halved 4 -> 2 to stay under the
        120s stream watchdog during a Hive API outage."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"reputation": 30.0}}
        with patch("project.hafsql.requests.post", return_value=mock_resp) as mock_post:
            get_reputations_via_api(["alice"])
        assert mock_post.call_args.kwargs["timeout"] == 2

    def test_caps_author_list(self):
        """Proposal 102 F1: the author list is bounded (was 1000) so one
        fallback pass can't fan out into a watchdog-tripping number of RPCs."""
        from project.hafsql import _API_REP_BATCH_CAP
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"reputation": 42.0}}
        authors = [f"user{i}" for i in range(_API_REP_BATCH_CAP + 25)]
        with patch("project.hafsql.requests.post", return_value=mock_resp) as mock_post:
            result = get_reputations_via_api(authors)
        # Each capped author succeeds on the first node -> one POST per author.
        assert mock_post.call_count == _API_REP_BATCH_CAP
        assert len(result) == _API_REP_BATCH_CAP

    def test_cap_tracks_stream_batch_size(self):
        """The cap is derived as stream._BATCH_SIZE * 2 (proposal 102 F1). Keep
        them in lockstep so a future batch-size bump can't silently widen the
        fallback fan-out past the watchdog budget."""
        from project.hafsql import _API_REP_BATCH_CAP
        from project.worker.stream import _BATCH_SIZE
        assert _API_REP_BATCH_CAP == _BATCH_SIZE * 2


# ── get_reputation_via_api (async wrapper) ──────────────────────────────────

class TestGetReputationViaApi:
    async def test_returns_score(self):
        with patch("project.hafsql.get_reputations_via_api", return_value={"alice": 55.0}):
            result = await get_reputation_via_api("alice")
        assert result == 55.0

    async def test_returns_none_on_miss(self):
        with patch("project.hafsql.get_reputations_via_api", return_value={}):
            result = await get_reputation_via_api("nobody")
        assert result is None


# ── shutdown ────────────────────────────────────────────────────────────────

class TestShutdown:
    def test_shutdown_idempotent(self):
        """Calling shutdown multiple times should not raise."""
        import project.hafsql as mod
        old_pool = mod._pool
        try:
            mod._pool = MagicMock()
            mod._pool.closed = False
            shutdown()
            assert mod._pool is None
            # Second call should be safe.
            shutdown()
        finally:
            mod._pool = old_pool

    def test_shutdown_with_no_pool(self):
        """shutdown() when pool is None should not raise."""
        import project.hafsql as mod
        old_pool = mod._pool
        try:
            mod._pool = None
            shutdown()
        finally:
            mod._pool = old_pool


# ── _parse_payout ─────────────────────────────────────────────────────────

class TestParsePayout:
    def test_numeric_payout(self):
        assert _parse_payout({"payout": 3.5}) == 3.5

    def test_string_values_summed(self):
        assert _parse_payout(
            {"pending_payout_value": "1.500 HBD", "total_payout_value": "2.000 HBD"}
        ) == 3.5

    def test_missing_returns_zero(self):
        assert _parse_payout({}) == 0.0

    def test_malformed_string_ignored(self):
        assert _parse_payout({"pending_payout_value": "garbage"}) == 0.0


# ── get_top_comments (proposal 095, mocked Hive API) ─────────────────────────

class TestGetTopComments:
    def _resp(self, thread):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": thread}
        return mock_resp

    def test_extracts_direct_children_sorted_by_payout(self):
        thread = {
            # the focal post itself — excluded (its parent isn't the focal post)
            "alice/post1": {"author": "alice", "permlink": "post1",
                            "parent_author": "", "parent_permlink": "topic",
                            "body": "the post", "stats": {}},
            "bob/c1": {"author": "bob", "permlink": "c1", "parent_author": "alice",
                       "parent_permlink": "post1", "body": "top comment",
                       "created": "2026-01-01", "payout": 5.0, "children": 1, "stats": {}},
            "carol/c2": {"author": "carol", "permlink": "c2", "parent_author": "alice",
                         "parent_permlink": "post1", "body": "second", "created": "2026-01-02",
                         "payout": 2.0, "children": 0, "stats": {}},
            # muted/gray — dropped even with the highest payout
            "dave/c3": {"author": "dave", "permlink": "c3", "parent_author": "alice",
                        "parent_permlink": "post1", "body": "spam", "created": "2026-01-03",
                        "payout": 99.0, "children": 0, "stats": {"gray": True}},
            # nested reply — not a direct child, excluded
            "eve/r1": {"author": "eve", "permlink": "r1", "parent_author": "bob",
                       "parent_permlink": "c1", "body": "nested", "created": "2026-01-04",
                       "payout": 50.0, "children": 0, "stats": {}},
        }
        with patch("project.hafsql.requests.post", return_value=self._resp(thread)):
            result = get_top_comments("alice", "post1", limit=10)
        assert [c["author"] for c in result] == ["bob", "carol"]
        assert result[0]["payout"] == 5.0
        assert result[0]["children"] == 1

    def test_respects_limit(self):
        thread = {
            f"u{i}/c{i}": {"author": f"u{i}", "permlink": f"c{i}", "parent_author": "alice",
                           "parent_permlink": "lim", "body": f"c{i}",
                           "created": f"2026-01-0{i}", "payout": float(i),
                           "children": 0, "stats": {}}
            for i in range(1, 6)
        }
        with patch("project.hafsql.requests.post", return_value=self._resp(thread)):
            result = get_top_comments("alice", "lim", limit=2)
        assert len(result) == 2
        assert result[0]["author"] == "u5"   # highest payout first

    def test_hidden_comment_dropped(self):
        thread = {
            "x/c": {"author": "x", "permlink": "c", "parent_author": "alice",
                    "parent_permlink": "hid", "body": "hidden", "created": "2026-01-01",
                    "payout": 1.0, "children": 0, "stats": {"hide": True}},
        }
        with patch("project.hafsql.requests.post", return_value=self._resp(thread)):
            assert get_top_comments("alice", "hid") == []

    def test_returns_empty_when_no_result(self):
        with patch("project.hafsql.requests.post", return_value=self._resp(None)):
            assert get_top_comments("alice", "none-post") == []

    def test_returns_empty_on_network_error(self):
        with patch("project.hafsql.requests.post", side_effect=Exception("down")):
            assert get_top_comments("alice", "err-post") == []


# ── Proposal 110 additions ──────────────────────────────────────────────────

class TestGetTopCommentsFailover:
    """B2: an empty/error node response must fail over and must not be cached."""

    def _resp(self, result):
        m = MagicMock()
        m.json.return_value = {"result": result}
        return m

    def test_empty_dict_falls_over_to_next_node(self):
        from project import cache
        cache.clear()
        good = {
            "alice/p": {"author": "alice", "permlink": "p", "parent_author": "",
                        "parent_permlink": "t", "body": "post", "stats": {}},
            "bob/c": {"author": "bob", "permlink": "c", "parent_author": "alice",
                      "parent_permlink": "p", "body": "comment", "created": "2026-01-01",
                      "payout": 1.0, "children": 0, "stats": {}},
        }
        # First node returns {} (bad/incomplete), second returns the real thread.
        with patch("project.hafsql.requests.post",
                   side_effect=[self._resp({}), self._resp(good)]) as mock_post:
            result = get_top_comments("alice", "p")
        assert [c["author"] for c in result] == ["bob"]
        assert mock_post.call_count == 2  # failed over instead of stopping at {}

    def test_empty_response_not_cached(self):
        from project import cache
        cache.clear()
        # All nodes return {} → [] returned but NOT cached, so a later healthy
        # call re-fetches rather than serving a stale empty list for 1h.
        with patch("project.hafsql.requests.post", return_value=self._resp({})):
            assert get_top_comments("alice", "nope") == []
        assert cache.get("top_comments:alice/nope") is None

    def test_childless_post_is_cached(self):
        from project import cache
        cache.clear()
        # A real post with no replies still returns the focal-post entry → a
        # non-empty dict → an empty children list that IS cached.
        thread = {"alice/p": {"author": "alice", "permlink": "p", "parent_author": "",
                              "parent_permlink": "t", "body": "post", "stats": {}}}
        with patch("project.hafsql.requests.post", return_value=self._resp(thread)):
            assert get_top_comments("alice", "p") == []
        assert cache.get("top_comments:alice/p") == []

    def test_truthy_non_dict_result_falls_over(self):
        from project import cache
        cache.clear()
        good = {
            "alice/p": {"author": "alice", "permlink": "p", "parent_author": "",
                        "parent_permlink": "t", "body": "post", "stats": {}},
            "bob/c": {"author": "bob", "permlink": "c", "parent_author": "alice",
                      "parent_permlink": "p", "body": "comment", "created": "2026-01-01",
                      "payout": 1.0, "children": 0, "stats": {}},
        }
        # A malformed node response (truthy but not a dict — e.g. a list) must
        # fail over to the next node, not be treated as a thread.
        with patch("project.hafsql.requests.post",
                   side_effect=[self._resp(["junk"]), self._resp(good)]) as mock_post:
            result = get_top_comments("alice", "p")
        assert [c["author"] for c in result] == ["bob"]
        assert mock_post.call_count == 2


class TestQueryGuards:
    """B5/B6/B7/B16: SQL guards (deleted/parent filters, timeouts, both schemes)."""

    def _capture(self):
        executed = []
        mock_cur = MagicMock()
        mock_cur.execute.side_effect = lambda sql, params=None: executed.append(sql)
        mock_cur.fetchone.return_value = None
        mock_cur.fetchall.return_value = []
        ctx = MagicMock()
        ctx.__enter__ = lambda s: mock_cur
        ctx.__exit__ = MagicMock(return_value=False)
        return executed, patch("project.hafsql._cursor", return_value=ctx)

    def _joined(self, executed):
        return " ".join(" ".join(s.split()) for s in executed)

    def test_get_post_body_filters_and_timeout(self):
        executed, p = self._capture()
        with p:
            get_post_body("alice", "p")
        joined = self._joined(executed)
        assert "deleted = false" in joined          # B6
        assert "parent_author = ''" in joined        # B6
        # B7: the timeout must be SET *before* the SELECT, or it's ineffective.
        set_idx = next(i for i, s in enumerate(executed) if "statement_timeout" in s)
        select_idx = next(i for i, s in enumerate(executed) if "SELECT" in s.upper())
        assert set_idx < select_idx

    def test_get_post_titles_excludes_deleted(self):
        executed, p = self._capture()
        with p:
            get_post_titles("alice", ["p1"])
        assert "deleted = false" in self._joined(executed)  # B5

    def test_get_posts_titles_and_excerpts_excludes_deleted(self):
        executed, p = self._capture()
        with p:
            get_posts_titles_and_excerpts([("alice", "p1")])
        assert "c.deleted = false" in self._joined(executed)  # B5

    def test_get_hivecomb_posts_matches_both_schemes(self):
        from project.hafsql import get_hivecomb_posts
        executed, p = self._capture()
        with p:
            get_hivecomb_posts(10)
        joined = " ".join(executed)
        assert "https://hivecomb.net/" in joined     # B16
        assert "http://hivecomb.net/" in joined      # B16


class TestPoolInitRace:
    """B11: concurrent cold _get_pool() calls build exactly one pool."""

    def test_concurrent_init_builds_one_pool(self):
        import threading
        import time as _time
        import project.hafsql as mod
        old = mod._pool
        construct_count = [0]

        def _slow_pool(*a, **k):
            construct_count[0] += 1
            _time.sleep(0.05)  # widen the race window
            m = MagicMock()
            m.closed = False
            return m

        try:
            mod._pool = None
            with patch("project.hafsql.psycopg2.pool.ThreadedConnectionPool",
                       side_effect=_slow_pool):
                threads = [threading.Thread(target=_get_pool) for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            assert construct_count[0] == 1
        finally:
            mod._pool = old


class TestWarnDegraded:
    """B1: HAFSQL/API degradation is WARNed once per interval, then suppressed."""

    def test_rate_limited_warning(self, caplog):
        import logging
        import project.hafsql as mod
        with mod._degrade_warn_lock:
            mod._last_degrade_warn.clear()
        with caplog.at_level(logging.WARNING, logger="project.hafsql"):
            mod._warn_degraded("op", RuntimeError("x"))
            mod._warn_degraded("op", RuntimeError("x"))  # within interval → suppressed
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
