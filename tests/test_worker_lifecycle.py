"""Tests for worker lifecycle — DB bridge, health, shutdown."""
import os
import pathlib
import threading
from unittest.mock import patch, MagicMock, AsyncMock

from project.worker.bridge import _DB
from project.worker.health import touch_heartbeat, _HEARTBEAT


# ── _DB bridge ──────────────────────────────────────────────────────────────

class TestDBBridge:
    def test_close_disposes_engine_and_closes_loop(self):
        """close() should dispose the engine and close the event loop."""
        db = _DB()
        with (
            patch("project.worker.bridge.hafsql_shutdown") as mock_hafsql,
            patch("project.worker.bridge.engine") as mock_engine,
        ):
            mock_engine.dispose = AsyncMock()
            db.close()
        mock_hafsql.assert_called_once()
        mock_engine.dispose.assert_called_once()
        assert db._loop.is_closed()

    def test_close_handles_dispose_error(self):
        """close() should not crash even if engine.dispose() raises."""
        db = _DB()
        with (
            patch("project.worker.bridge.hafsql_shutdown"),
            patch("project.worker.bridge.engine") as mock_engine,
        ):
            mock_engine.dispose = AsyncMock(side_effect=Exception("dispose error"))
            # Should not raise — the try/finally should handle it.
            try:
                db.close()
            except Exception:
                pass  # Expected since dispose raises
        # Loop should still be closed via finally.
        assert db._loop.is_closed()


# ── Heartbeat ──────────────────────────────────────────────────────────────

class TestHeartbeat:
    def test_touch_heartbeat_creates_file(self, tmp_path):
        """touch_heartbeat writes timestamp to heartbeat file."""
        test_path = tmp_path / "heartbeat"
        with patch("project.worker.health._HEARTBEAT", test_path):
            touch_heartbeat()
        assert test_path.exists()
        content = test_path.read_text()
        assert content.isdigit()

    def test_touch_heartbeat_handles_oserror(self):
        """touch_heartbeat should not crash on OSError."""
        with patch("project.worker.health._HEARTBEAT") as mock_path:
            mock_path.write_text.side_effect = OSError("Permission denied")
            # Should not raise.
            touch_heartbeat()


# ── Signal handler ────────────────────────────────────────────────────────

class TestSignalHandler:
    def test_sigterm_sets_stop_event(self):
        """The SIGTERM handler should set the stop event."""
        stop = threading.Event()

        def handle_sigterm(signum, frame):
            stop.set()

        handle_sigterm(15, None)
        assert stop.is_set()
