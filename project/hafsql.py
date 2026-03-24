"""HAFSQL client — read Hive blockchain data via public PostgreSQL.

This module wraps the public HAFSQL database maintained by @mahdiyari.
It provides fast access to post content, author reputation, and tags
without needing to store this data locally.

Connection: direct PostgreSQL (psycopg2), not HTTP — much faster than RPC.
"""

import logging
import math
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
        return 25.0  # Hive default for new accounts with no votes
    neg = raw < 0
    raw_abs = abs(raw)
    leading = int(math.log10(raw_abs))
    top = raw_abs / (10 ** (leading - 3))
    score = (leading - 9) * 9 + math.log10(top) * 9
    if neg:
        score = -score
    return round(score + 25, 2)



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



def get_post_body(author: str, permlink: str) -> str | None:
    """Fetch a single post's body from HAFSQL.

    Returns the body text or None if not found / unreachable.
    """
    try:
        with _cursor() as cur:
            cur.execute(
                "SELECT body FROM hafsql.comments WHERE author = %s AND permlink = %s",
                (author, permlink),
            )
            row = cur.fetchone()
            if row:
                return row["body"]
    except Exception as exc:
        logger.debug("hafsql post body lookup failed for %s/%s: %s", author, permlink, exc)
    return None


def get_post_metadata(author: str, permlink: str) -> dict | None:
    """Fetch post title, description, and image via Hive API bridge.get_post.

    Returns {"title": ..., "description": ..., "image": ...} or None.
    """
    try:
        resp = requests.post(
            "https://api.hive.blog",
            json={
                "jsonrpc": "2.0",
                "method": "bridge.get_post",
                "params": {"author": author, "permlink": permlink},
                "id": 1,
            },
            timeout=4,
        )
        data = resp.json().get("result")
        if data is None:
            return None

        title = data.get("title") or ""

        # Description: json_metadata.description or first 160 chars of cleaned body.
        meta = data.get("json_metadata") or {}
        if isinstance(meta, str):
            import json
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}

        description = ""
        if isinstance(meta, dict):
            description = meta.get("description") or ""
        if not description:
            from .text import clean_post_body
            body = data.get("body") or ""
            description = clean_post_body(body)[:160]

        # First image from json_metadata.
        image = ""
        if isinstance(meta, dict):
            images = meta.get("image") or []
            if isinstance(images, list) and images:
                image = str(images[0])

        return {"title": title, "description": description, "image": image}
    except Exception as exc:
        logger.debug("post metadata lookup failed for %s/%s: %s", author, permlink, exc)
    return None


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
            timeout=4,
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

