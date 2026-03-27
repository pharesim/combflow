"""Tests for worker blacklist module — is_blacklisted, cache, sweep."""
import json
import threading
import time
from unittest.mock import patch, MagicMock

from project.worker.blacklist import (
    is_blacklisted, check_authors, sweep_thread,
    _cache, _cache_lock,
)


class TestIsBlacklisted:
    def setup_method(self):
        with _cache_lock:
            _cache.clear()

    def test_not_blacklisted_empty_array(self):
        """Empty JSON array means not blacklisted."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("project.worker.blacklist.urlopen", return_value=mock_resp):
            assert is_blacklisted("gooduser") is False

    def test_blacklisted_non_empty_array(self):
        """Non-empty JSON array means blacklisted."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([{"name": "spam"}]).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("project.worker.blacklist.urlopen", return_value=mock_resp):
            assert is_blacklisted("spammer") is True

    def test_non_json_response_fails_open(self):
        """HTML error page (non-JSON) should fail open (not blacklisted)."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"<html>Error</html>"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("project.worker.blacklist.urlopen", return_value=mock_resp):
            assert is_blacklisted("user") is False

    def test_network_timeout_fails_open(self):
        """Network error should fail open."""
        from urllib.error import URLError
        with patch("project.worker.blacklist.urlopen", side_effect=URLError("timeout")):
            assert is_blacklisted("user") is False

    def test_cache_hit_on_second_call(self):
        """Second call for same author uses cache, not API."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("project.worker.blacklist.urlopen", return_value=mock_resp) as mock_url:
            is_blacklisted("cached")
            is_blacklisted("cached")
            assert mock_url.call_count == 1

    def test_cache_eviction_fifo(self):
        """Cache evicts oldest (FIFO) entries when exceeding max size."""
        small_max = 10
        now = time.monotonic()
        with _cache_lock:
            for i in range(small_max):
                _cache[f"user{i}"] = (False, now)
                _cache.move_to_end(f"user{i}")

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("project.worker.blacklist.urlopen", return_value=mock_resp),
            patch("project.worker.blacklist._MAX_CACHE", small_max),
        ):
            is_blacklisted("overflow_user")

        with _cache_lock:
            assert len(_cache) <= small_max
            assert "overflow_user" in _cache
            assert "user0" not in _cache


class TestCheckAuthors:
    def setup_method(self):
        with _cache_lock:
            _cache.clear()

    def test_returns_blacklisted_subset(self):
        """check_authors returns only blacklisted authors."""
        def fake_blacklist(author):
            return author == "bad"

        with patch("project.worker.blacklist.is_blacklisted", side_effect=fake_blacklist):
            result = check_authors(["good", "bad", "neutral"])
        assert result == {"bad"}


class TestSweepThread:
    def setup_method(self):
        with _cache_lock:
            _cache.clear()

    def test_stop_event_exits_immediately(self):
        """If stop_event is already set, sweep_thread returns on initial wait."""
        mock_db = MagicMock()
        stop = threading.Event()
        stop.set()
        sweep_thread(mock_db, stop)

    def test_sweep_checks_authors_and_deletes(self):
        """Sweep checks each DB author and deletes posts of blacklisted ones."""
        import project.worker.bridge as bridge_mod

        mock_db = MagicMock()
        stop = threading.Event()
        checked = []

        def track_blacklist(author):
            checked.append(author)
            return author == "spammer"

        get_calls = [0]
        def mock_get(db, limit=10_000, offset=0):
            get_calls[0] += 1
            return ["gooduser", "spammer"] if get_calls[0] == 1 else []

        mock_delete = MagicMock(return_value=2)

        # Monkey-patch bridge module functions (sweep_thread imports them locally).
        orig_get = bridge_mod._get_distinct_authors
        orig_del = bridge_mod._delete_posts_by_author
        bridge_mod._get_distinct_authors = mock_get
        bridge_mod._delete_posts_by_author = mock_delete

        wait_calls = [0]
        def mock_wait(timeout=None):
            wait_calls[0] += 1
            if wait_calls[0] == 1:
                return False  # skip initial 60s delay
            stop.set()
            return True  # exit after first sweep
        stop.wait = mock_wait

        try:
            with (
                patch("project.worker.blacklist.is_blacklisted", side_effect=track_blacklist),
                patch("project.worker.blacklist.time.sleep"),
            ):
                sweep_thread(mock_db, stop)
        finally:
            bridge_mod._get_distinct_authors = orig_get
            bridge_mod._delete_posts_by_author = orig_del

        assert "gooduser" in checked
        assert "spammer" in checked
        mock_delete.assert_called_once_with(mock_db, "spammer")
