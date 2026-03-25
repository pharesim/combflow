"""Unit tests for HAFSQL utility functions (no network required)."""
from unittest.mock import patch, MagicMock

import pytest

from project.hafsql import (
    _raw_rep_to_score, build_dsn, get_reputations,
    get_reputations_via_api, get_reputation_via_api, shutdown,
    get_community, get_post_body, get_post_metadata,
    _cursor, _get_pool,
)


class TestRawRepToScore:
    def test_zero_returns_default(self):
        assert _raw_rep_to_score(0) == 25.0

    def test_positive_rep(self):
        result = _raw_rep_to_score(1_000_000_000)
        assert 25 < result < 80

    def test_very_high_rep(self):
        result = _raw_rep_to_score(500_000_000_000_000)
        assert result > 60

    def test_negative_rep(self):
        result = _raw_rep_to_score(-1_000_000_000)
        assert result < 25

    def test_small_positive(self):
        result = _raw_rep_to_score(10_000_000)
        assert result > 0

    def test_monotonic(self):
        """Higher raw rep should give higher score."""
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

    def test_returns_empty_on_exception(self):
        with patch("project.hafsql._cursor", side_effect=Exception("down")):
            result = get_reputations(["alice"])
        assert result == {}


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
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": [
                {"name": "alice", "reputation": 1_000_000_000},
                {"name": "bob", "reputation": 500_000_000_000_000},
            ]
        }
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_reputations_via_api(["alice", "bob"])
        assert "alice" in result
        assert "bob" in result
        assert result["bob"] > result["alice"]

    def test_failover_to_second_node(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": [{"name": "alice", "reputation": 1_000_000_000}]
        }
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("first node down")
            return mock_resp
        with patch("project.hafsql.requests.post", side_effect=side_effect):
            result = get_reputations_via_api(["alice"])
        assert "alice" in result

    def test_all_nodes_fail_returns_empty(self):
        with patch("project.hafsql.requests.post", side_effect=Exception("all down")):
            result = get_reputations_via_api(["alice"])
        assert result == {}


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
