"""Worker health check — heartbeat file for Docker."""
import pathlib
import time

_HEARTBEAT = pathlib.Path("/tmp/worker_heartbeat")


def touch_heartbeat():
    """Update heartbeat file for Docker health check."""
    try:
        _HEARTBEAT.write_text(str(int(time.time())))
    except OSError:
        pass
