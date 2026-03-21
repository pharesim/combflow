"""HAFSQL client — read Hive blockchain data via public PostgreSQL.

This module wraps the public HAFSQL database maintained by @mahdiyari.
It provides fast access to post content, author reputation, and tags
without needing to store this data locally.

Connection: direct PostgreSQL (psycopg2), not HTTP — much faster than RPC.
"""

import logging
import math
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests

from .config import settings

logger = logging.getLogger(__name__)


def build_dsn() -> str:
    """Build the HAFSQL DSN from settings."""
    return (
        f"host={settings.hafsql_host} port={settings.hafsql_port} "
        f"dbname={settings.hafsql_db} user={settings.hafsql_user} "
        f"password={settings.hafsql_password} "
        f"connect_timeout={settings.hafsql_connect_timeout}"
    )


_pool = None


def _get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(2, 5, build_dsn())
    return _pool


@contextmanager
def _cursor():
    """Yield a dict cursor from the connection pool. Thread-safe."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        cur.close()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        pool.putconn(conn, close=True)
        conn = None
        raise
    finally:
        if conn is not None:
            pool.putconn(conn)


# ── Reputation ────────────────────────────────────────────────────────────────

def _raw_rep_to_score(raw: int) -> float:
    """Convert Hive raw reputation integer to human-readable score."""
    if raw == 0:
        return 0.0
    neg = raw < 0
    raw_abs = abs(raw)
    leading = int(math.log10(raw_abs))
    top = raw_abs / (10 ** (leading - 3))
    score = (leading - 9) * 9 + math.log10(top) * 9
    if neg:
        score = -score
    return round(score + 25, 2)


def get_reputation(author: str) -> float:
    """Get author reputation score (human-readable, like PeakD shows).

    Returns 0.0 if HAFSQL is unreachable (graceful degradation).
    """
    try:
        with _cursor() as cur:
            cur.execute(
                "SELECT reputation FROM hafsql.reputations WHERE account_name = %s",
                (author,),
            )
            row = cur.fetchone()
            if row:
                return _raw_rep_to_score(int(row["reputation"]))
    except Exception as exc:
        logger.debug("hafsql reputation lookup failed for %s: %s", author, exc)
    return 0.0


def get_reputations(authors: list[str]) -> dict[str, float]:
    """Batch reputation lookup."""
    if not authors:
        return {}
    try:
        with _cursor() as cur:
            cur.execute(
                "SELECT account_name, reputation FROM hafsql.reputations "
                "WHERE account_name = ANY(%s)",
                (authors,),
            )
            return {
                row["account_name"]: _raw_rep_to_score(int(row["reputation"]))
                for row in cur.fetchall()
            }
    except Exception as exc:
        logger.debug("hafsql batch reputation failed: %s", exc)
    return {}


# ── Posting key (cached) ─────────────────────────────────────────────────────

_posting_key_cache: dict[str, tuple[str | None, float]] = {}
_POSTING_KEY_TTL = 600  # 10 minutes


def get_posting_key(username: str) -> str | None:
    """Get the primary public posting key for a Hive account.

    Cached with a 10-minute TTL. Returns None on failure.
    """
    now = time.monotonic()
    cached = _posting_key_cache.get(username)
    if cached and now - cached[1] < _POSTING_KEY_TTL:
        return cached[0]

    key = _fetch_posting_key(username)
    if key is not None:
        _posting_key_cache[username] = (key, now)
    return key


def get_comments(root_author: str, root_permlink: str) -> list[dict]:
    """Fetch all comments for a root post from HAFSQL with reputation.

    Returns a flat list of comment dicts with converted reputation scores.
    Returns [] if HAFSQL is unreachable (graceful degradation).
    """
    try:
        with _cursor() as cur:
            cur.execute(
                """
                SELECT c.author, c.permlink, c.body, c.created,
                       c.parent_author, c.parent_permlink,
                       r.reputation
                FROM hafsql.comments c
                LEFT JOIN hafsql.reputations r ON r.account_name = c.author
                WHERE c.root_author = %s AND c.root_permlink = %s
                  AND c.author != %s
                ORDER BY c.created ASC
                """,
                (root_author, root_permlink, root_author),
            )
            rows = cur.fetchall()
            return [
                {
                    "author": row["author"],
                    "permlink": row["permlink"],
                    "body": row["body"],
                    "created": row["created"].isoformat() if row["created"] else None,
                    "parent_author": row["parent_author"],
                    "parent_permlink": row["parent_permlink"],
                    "reputation": _raw_rep_to_score(int(row["reputation"]))
                    if row["reputation"]
                    else 0.0,
                }
                for row in rows
            ]
    except Exception as exc:
        logger.debug("hafsql comments lookup failed for %s/%s: %s", root_author, root_permlink, exc)
    return []


def get_community(community_id: str) -> dict | None:
    """Fetch community title and about via Hive API bridge.get_community.

    Returns {"title": ..., "about": ...} or None if unavailable.
    """
    try:
        resp = requests.post(
            "https://api.hive.blog",
            json={
                "jsonrpc": "2.0",
                "method": "bridge.get_community",
                "params": {"name": community_id},
                "id": 1,
            },
            timeout=10,
        )
        data = resp.json().get("result")
        if data is not None:
            return {
                "title": data.get("title") or "",
                "about": data.get("about") or "",
            }
    except Exception as exc:
        logger.debug("community lookup failed for %s: %s", community_id, exc)
    return None


def _fetch_posting_key(username: str) -> str | None:
    """Fetch posting key from Hive API."""
    import httpx

    nodes = [
        "https://api.hive.blog",
        "https://api.deathwing.me",
        "https://rpc.ausbit.dev",
    ]
    for node in nodes:
        try:
            resp = httpx.post(
                node,
                json={
                    "jsonrpc": "2.0",
                    "method": "condenser_api.get_accounts",
                    "params": [[username]],
                    "id": 1,
                },
                timeout=5,
            )
            result = resp.json().get("result", [])
            if result:
                keys = result[0].get("posting", {}).get("key_auths", [])
                if keys:
                    return keys[0][0]
            return None
        except Exception as exc:
            logger.debug("Hive API posting key lookup failed at %s: %s", node, exc)
            continue
    return None
