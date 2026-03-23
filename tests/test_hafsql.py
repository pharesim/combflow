"""Unit tests for HAFSQL utility functions (no network required)."""
from unittest.mock import patch, MagicMock

import pytest

from project.hafsql import (
    _raw_rep_to_score, build_dsn, get_reputations,
    get_community, get_post_body,
    _cursor, _get_pool,
)


class TestRawRepToScore:
    def test_zero_returns_zero(self):
        assert _raw_rep_to_score(0) == 0.0

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

    def test_returns_none_on_exception(self):
        with patch("project.hafsql.requests.post", side_effect=Exception("down")):
            result = get_community("hive-174578")
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
    def test_network_timeout_returns_none(self):
        import requests
        with patch("project.hafsql.requests.post", side_effect=requests.Timeout("timeout")):
            result = get_community("hive-bad")
        assert result is None

    def test_malformed_response_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("bad json")
        with patch("project.hafsql.requests.post", return_value=mock_resp):
            result = get_community("hive-bad")
        assert result is None

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
