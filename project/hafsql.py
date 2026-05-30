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

from . import cache
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



def shutdown():
    """Close the HAFSQL connection pool. Safe to call multiple times."""
    global _pool
    if _pool is not None and not _pool.closed:
        try:
            _pool.closeall()
        except Exception as exc:
            logger.warning("HAFSQL pool shutdown error: %s", exc)
        _pool = None


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



def get_reputations_via_api(authors: list[str]) -> dict[str, float]:
    """Fallback: fetch reputations via Hive API when HAFSQL is unreachable.

    Uses bridge.get_profile (one call per author) which returns the
    pre-computed human-readable reputation score directly, unlike
    condenser_api.get_accounts which now returns reputation: 0.
    """
    if not authors:
        return {}
    result: dict[str, float] = {}
    for author in authors[:1000]:
        for node in settings.hive_api_nodes:
            try:
                resp = requests.post(
                    node,
                    json={
                        "jsonrpc": "2.0",
                        "method": "bridge.get_profile",
                        "params": {"account": author},
                        "id": 1,
                    },
                    timeout=4,
                )
                data = resp.json()
                profile = data.get("result")
                if profile and "reputation" in profile:
                    result[author] = float(profile["reputation"])
                break  # success on this node, move to next author
            except Exception:
                continue  # try next node
    return result


async def get_reputation_via_api(username: str) -> float | None:
    """Fetch a single user's reputation via Hive API. Returns score or None on failure."""
    import asyncio
    reps = await asyncio.to_thread(get_reputations_via_api, [username])
    return reps.get(username)


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


def get_post_titles(author: str, permlinks: list[str]) -> dict[str, str]:
    """Fetch titles for an author's permlinks from HAFSQL.

    Returns a {permlink: title} dict. Permlinks the source has no row for are
    absent from the result; empty/None titles are dropped. Returns {} on
    HAFSQL error so callers can degrade to no title list rather than failing.
    Used to enrich the server-rendered ``/@author`` recent-posts list — the
    text Google indexes when prerender intermittently times out.
    """
    if not permlinks:
        return {}
    try:
        with _cursor() as cur:
            cur.execute("SET statement_timeout = '5s'")
            cur.execute(
                "SELECT permlink, title FROM hafsql.comments "
                "WHERE author = %s AND permlink = ANY(%s)",
                (author, list(permlinks)),
            )
            return {
                r["permlink"]: r["title"]
                for r in cur.fetchall()
                if r["title"]
            }
    except Exception as exc:
        logger.debug("hafsql post titles lookup failed for %s: %s", author, exc)
    return {}


def get_posts_titles_and_excerpts(
    pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], dict]:
    """Batch-fetch title + body for ``(author, permlink)`` pairs from HAFSQL.

    Unlike ``get_post_titles`` (single author, many permlinks) this looks up an
    arbitrary set of author/permlink pairs in one round-trip — recent posts for
    the server-rendered SEO lists on ``/``, ``/c/``, ``/lang/``, ``/community/``
    span many different authors.

    Returns ``{(author, permlink): {"title": str, "body": str}}``. The body is
    truncated to 2000 chars in SQL (the caller only needs a short plain-text
    excerpt — no point pulling multi-megabyte post bodies across the wire).
    Pairs the source has no row for are absent from the result; empty/None
    titles are dropped. Returns ``{}`` on HAFSQL error so callers degrade to no
    list rather than failing.
    """
    if not pairs:
        return {}
    authors = [a for a, _ in pairs]
    permlinks = [p for _, p in pairs]
    try:
        with _cursor() as cur:
            cur.execute("SET statement_timeout = '5s'")
            # unnest the two arrays in lockstep into (author, permlink) rows,
            # then join — a composite-key lookup without a giant OR/IN list.
            cur.execute(
                "SELECT c.author, c.permlink, c.title, LEFT(c.body, 2000) AS body "
                "FROM hafsql.comments c "
                "JOIN unnest(%s::text[], %s::text[]) AS k(author, permlink) "
                "  ON c.author = k.author AND c.permlink = k.permlink",
                (authors, permlinks),
            )
            return {
                (r["author"], r["permlink"]): {
                    "title": r["title"] or "",
                    "body": r["body"] or "",
                }
                for r in cur.fetchall()
                if r["title"]
            }
    except Exception as exc:
        logger.debug("hafsql batch titles+excerpts lookup failed: %s", exc)
    return {}


def get_post_full(author: str, permlink: str) -> dict | None:
    """Fetch the full bridge.get_post response. Returns None on error/not found.

    Inlined into post-page HTML so the client can render the modal without a
    duplicate RPC call. Also feeds get_post_metadata for OG extraction.
    """
    for node in settings.hive_api_nodes:
        try:
            resp = requests.post(
                node,
                json={
                    "jsonrpc": "2.0",
                    "method": "bridge.get_post",
                    "params": {"author": author, "permlink": permlink},
                    "id": 1,
                },
                timeout=4,
            )
            result = resp.json().get("result")
            if result is not None:
                return result
        except Exception as exc:
            logger.debug("post full fetch failed on %s for %s/%s: %s", node, author, permlink, exc)
            continue
    return None


def extract_post_metadata(data: dict) -> dict:
    """Extract OG / canonical fields from a bridge.get_post response dict."""
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

    # Publisher-declared canonical (json_metadata.canonical_url).
    canonical_url = ""
    if isinstance(meta, dict):
        cu = meta.get("canonical_url")
        if isinstance(cu, str) and cu.startswith(("https://", "http://")):
            canonical_url = cu

    # Publishing app (json_metadata.app) — first slash-segment only.
    # Used to derive a canonical URL when canonical_url is absent.
    app = ""
    if isinstance(meta, dict):
        a = meta.get("app")
        if isinstance(a, str):
            app = a.split("/", 1)[0].strip().lower()

    # Cross-post markers — peakd/ecency set these when a post republishes
    # someone else's content. The original is the rightful canonical.
    original_author = ""
    original_permlink = ""
    if isinstance(meta, dict):
        oa = meta.get("original_author")
        op = meta.get("original_permlink")
        if isinstance(oa, str) and isinstance(op, str) and oa and op:
            original_author = oa
            original_permlink = op

    # Replies/comments have a non-empty parent_author at the top level of
    # the bridge.get_post response (alongside title/body, not in json_metadata).
    parent_author = data.get("parent_author") or ""

    return {
        "title": title,
        "description": description,
        "image": image,
        "canonical_url": canonical_url,
        "app": app,
        "original_author": original_author,
        "original_permlink": original_permlink,
        "parent_author": parent_author,
    }


def get_post_metadata(author: str, permlink: str) -> dict | None:
    """Convenience: get_post_full + extract_post_metadata. Returns None on error."""
    data = get_post_full(author, permlink)
    if data is None:
        return None
    return extract_post_metadata(data)


def _parse_payout(comment: dict) -> float:
    """Best-effort total payout for a bridge comment object.

    bridge.get_discussion sometimes exposes a numeric ``payout`` field; when
    it doesn't, sum the HBD value strings (e.g. "1.234 HBD"). Returns 0.0 when
    nothing parseable is present (zero-payout comments are common and fine).
    """
    val = comment.get("payout")
    if isinstance(val, (int, float)):
        return float(val)
    total = 0.0
    for key in ("pending_payout_value", "total_payout_value", "curator_payout_value"):
        raw = comment.get(key)
        if isinstance(raw, str):
            try:
                total += float(raw.split()[0])
            except (ValueError, IndexError):
                pass
    return total


def get_top_comments(author: str, permlink: str, limit: int = 10) -> list[dict]:
    """Fetch the top direct replies to a post, sorted by payout DESC (proposal 095).

    Uses ``bridge.get_discussion`` which returns the full thread keyed by
    ``"author/permlink"`` strings — sort order is NOT guaranteed by any field,
    so we explicitly select the *direct* children of the focal post
    (``parent_author``/``parent_permlink`` match) and sort them deterministically
    by payout, then created, then permlink. Muted/hidden comments (``stats.hide``
    or ``stats.gray``) are dropped so we don't surface spam for SEO.

    Cached per-post for 1h (comments evolve slowly). Returns a list of
    ``{author, permlink, body, created, payout, children}``; empty list on
    error / not found.
    """
    cache_key = f"top_comments:{author}/{permlink}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    thread = None
    for node in settings.hive_api_nodes:
        try:
            resp = requests.post(
                node,
                json={
                    "jsonrpc": "2.0",
                    "method": "bridge.get_discussion",
                    "params": {"author": author, "permlink": permlink},
                    "id": 1,
                },
                timeout=4,
            )
            thread = resp.json().get("result")
            if thread is not None:
                break
        except Exception:
            continue  # try next node
    if not isinstance(thread, dict):
        return []

    children: list[dict] = []
    for comment in thread.values():
        if not isinstance(comment, dict):
            continue
        if (
            comment.get("parent_author") != author
            or comment.get("parent_permlink") != permlink
        ):
            continue
        stats = comment.get("stats") or {}
        if stats.get("hide") or stats.get("gray"):
            continue
        children.append({
            "author": comment.get("author") or "",
            "permlink": comment.get("permlink") or "",
            "body": comment.get("body") or "",
            "created": comment.get("created") or "",
            "payout": _parse_payout(comment),
            "children": int(comment.get("children") or 0),
        })

    children.sort(key=lambda c: (-c["payout"], c["created"], c["permlink"]))
    result = children[:limit]
    cache.put(cache_key, result, ttl=3600)
    return result


def get_hivecomb_posts(limit: int = 1000) -> list[tuple]:
    """Return (author, permlink, created) for top-level posts published via HiveComb.

    Identifies posts by json_metadata.app starting with 'hivecomb' or
    canonical_url pointing to hivecomb.net. Other UIs win the Google canonical
    battle for posts originally published elsewhere, so the sitemap only lists
    posts where we're the rightful canonical.
    """
    # Don't swallow failures — empty list here would get cached by the warm
    # task and serve a bad sitemap for 24h. Let exceptions propagate so the
    # warm task skips the cache write and retries on the next interval.
    # (SET, not SET LOCAL: autocommit mode means no transaction for SET LOCAL
    # to bind to. Session-scoped SET is what we actually want here.)
    with _cursor() as cur:
        cur.execute("SET statement_timeout = '30s'")
        cur.execute(
            """
            SELECT author, permlink, created
            FROM hafsql.comments
            WHERE parent_author = ''
              AND deleted = false
              AND created >= NOW() - INTERVAL '180 days'
              AND (
                (json_metadata ->> 'app') ILIKE 'hivecomb%%'
                OR (json_metadata ->> 'canonical_url') ILIKE 'https://hivecomb.net/%%'
              )
            ORDER BY created DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [(row["author"], row["permlink"], row["created"]) for row in cur.fetchall()]


def get_community(community_id: str) -> dict | None:
    """Fetch community title and about via Hive API bridge.get_community.

    Returns {"title": ..., "about": ...} or None on error/not found.
    """
    for node in settings.hive_api_nodes:
        try:
            resp = requests.post(
                node,
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
            logger.debug("community lookup failed on %s for %s: %s", node, community_id, exc)
            continue
    return None

