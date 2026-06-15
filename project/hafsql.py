"""HAFSQL client — read Hive blockchain data via public PostgreSQL.

This module wraps the public HAFSQL database maintained by @mahdiyari.
It provides fast access to post content, author reputation, and tags
without needing to store this data locally.

Connection: direct PostgreSQL (psycopg2), not HTTP — much faster than RPC.
"""

import logging
import math
import threading
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests

from . import cache
from .config import settings

logger = logging.getLogger(__name__)


# ── Degradation logging (proposal 110 B1) ──────────────────────────────────────
# The data handlers below swallow transient failures so the worker degrades
# gracefully, but at ``logger.debug`` the signal was invisible at the prod INFO
# level: a HAFSQL-only outage (where ``stream.py`` only WARNs when *both* HAFSQL
# and the API fallback fail) showed up nowhere. Promote the first failure to
# WARNING, rate-limited per operation so a sustained outage can't flood the log.
_DEGRADE_WARN_INTERVAL = 60.0  # seconds between WARNs for the same operation
_last_degrade_warn: dict[str, float] = {}
_degrade_warn_lock = threading.Lock()


def _warn_degraded(operation: str, exc: BaseException) -> None:
    """WARN that a HAFSQL/Hive-API ``operation`` degraded, rate-limited per op."""
    now = time.monotonic()
    with _degrade_warn_lock:
        last = _last_degrade_warn.get(operation, 0.0)
        if now - last < _DEGRADE_WARN_INTERVAL:
            logger.debug("hafsql %s failed (warn suppressed): %s", operation, exc)
            return
        _last_degrade_warn[operation] = now
    logger.warning("HAFSQL/Hive-API degraded — %s failed: %s", operation, exc)


def build_dsn() -> str:
    """Build the HAFSQL DSN from settings."""
    return (
        f"host={settings.hafsql_host} port={settings.hafsql_port} "
        f"dbname={settings.hafsql_db} user={settings.hafsql_user} "
        f"password={settings.hafsql_password} "
        f"connect_timeout={settings.hafsql_connect_timeout}"
    )


_pool = None
# Proposal 110 B11: guard lazy pool creation. Without it two cold threads can
# both pass the ``_pool is None`` check and each build a ThreadedConnectionPool,
# orphaning one (its minconn sockets leaked). ``_resolve_community`` already
# locks its lazy init; this one never got the same treatment.
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    pool = _pool  # single read for the fast path — a concurrent shutdown() can't
    if pool is not None and not pool.closed:  # land between two reads of _pool here
        return pool
    with _pool_lock:
        if _pool is None or _pool.closed:  # double-checked under the lock
            _pool = psycopg2.pool.ThreadedConnectionPool(2, 5, build_dsn())
        return _pool


@contextmanager
def _cursor():
    """Yield a dict cursor from the connection pool. Thread-safe."""
    pool = _get_pool()
    conn = pool.getconn()
    cur = None
    try:
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        pool.putconn(conn, close=True)
        conn = None
        raise
    finally:
        # Proposal 110 B15: close the yielded cursor unconditionally. The old
        # success-path ``cur.close()`` was skipped when the caller raised a
        # non-Operational exception, returning the connection with an open
        # cursor. Residual was near-zero (client-side RealDictCursor, rows
        # already buffered) but the close belongs in finally.
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            # Proposal 102 F6: clear any per-call ``SET statement_timeout`` before
            # returning the connection to the pool, so a leaked value can't
            # silently cap a later checkout that never SET its own. autocommit is
            # on, so RESET takes effect immediately on this session.
            try:
                with conn.cursor() as reset_cur:
                    reset_cur.execute("RESET statement_timeout")
            except Exception:
                pass  # connection may be broken; the pool will discard it
            pool.putconn(conn)


# ── Reputation ────────────────────────────────────────────────────────────────

# Reputation API fallback bounds (proposal 102 F1). The live-stream caller
# (`_process_batch`) only ever passes a single stream batch of unique authors
# (`stream._BATCH_SIZE` = 10); cap at 2× as a safety bound and halve the
# per-node timeout so one fallback pass during a Hive API outage can't fan out
# into enough slow RPCs to trip the stream watchdog (`stream._STREAM_TIMEOUT` =
# 120s). Realistic worst case is 10 × len(nodes)=3 × 2s = 60s (50% margin). NB:
# at the cap the worst case is 20 × 3 × 2 = 120s — exactly the watchdog budget,
# zero headroom — but no caller passes >10 authors. If `_BATCH_SIZE` or the node
# count grows, revisit this cap / timeout. (Was 1000 × 3 × 4s = 200 minutes.)
_API_REP_BATCH_CAP = 20
_API_REP_TIMEOUT = 2  # seconds per node


def _raw_rep_to_score(raw: int) -> float:
    """Convert a Hive raw reputation integer to its human-readable score.

    Mirrors Hive's canonical ``rep_log10`` exactly: ``9·log10(raw) − 56``,
    clamped at the 25.0 floor for positive raw below 10^9 and sign-flipped for
    downvoted (negative-reputation) accounts.

    Proposal 102 F2 replaced a previous implementation that simplified to
    ``9·log10(raw) − 29`` — a constant +27 inflation that let near-zero and
    negative-reputation accounts slip past the worker's
    ``MIN_AUTHOR_REPUTATION`` ingest gate (it mapped the 20.0 threshold back to
    raw ≈ 2.75×10^5, below virtually every real account, making the gate a
    near-no-op). Used by both the live-stream and backfill classification paths.
    """
    if raw == 0:
        return 25.0  # Hive default for new accounts with no votes
    neg = raw < 0
    raw_abs = abs(raw)
    score = max(math.log10(raw_abs) - 9.0, 0.0) * 9.0
    if neg:
        score = -score
    return round(score + 25.0, 2)



def shutdown():
    """Close the HAFSQL connection pool. Safe to call multiple times."""
    global _pool
    with _pool_lock:  # B11: serialise with _get_pool's double-checked init
        if _pool is not None and not _pool.closed:
            try:
                _pool.closeall()
            except Exception as exc:
                logger.warning("HAFSQL pool shutdown error: %s", exc)
            _pool = None


def get_reputations(authors: list[str]) -> dict[str, float] | None:
    """Batch reputation lookup.

    Returns ``{account_name: score}`` for the authors HAFSQL has rows for.
    Proposal 110 B10: returns ``None`` (not ``{}``) when the query *fails* — an
    outage — so the caller can distinguish that from a genuine "no rows" empty
    result and only then fire the slow Hive-API fallback. An empty ``authors``
    input is not an outage, so it still returns ``{}``.
    """
    if not authors:
        return {}
    try:
        with _cursor() as cur:
            cur.execute(
                "SELECT account_name, reputation FROM hafsql.reputations "
                "WHERE account_name = ANY(%s)",
                (authors,),
            )
            rows = cur.fetchall()
    except Exception as exc:
        _warn_degraded("get_reputations", exc)
        return None
    # Proposal 110 B14: convert per row so one NULL/garbage ``reputation`` skips
    # only that row instead of dropping the whole batch (the old dict-comp ran
    # under the broad ``except`` and turned a single bad value into ``{}``).
    result: dict[str, float] = {}
    for row in rows:
        raw = row.get("reputation")
        try:
            result[row["account_name"]] = _raw_rep_to_score(int(raw))
        except (TypeError, ValueError):
            logger.debug("skipping unparseable reputation %r for %s",
                         raw, row.get("account_name"))
    return result



def get_reputations_via_api(authors: list[str]) -> dict[str, float]:
    """Fallback: fetch reputations via Hive API when HAFSQL is unreachable.

    Uses bridge.get_profile (one call per author) which returns the
    pre-computed human-readable reputation score directly, unlike
    condenser_api.get_accounts which now returns reputation: 0.

    Proposal 110 B8 (decided: accepted asymmetry). The primary path
    (``get_reputations`` → ``_raw_rep_to_score``) applies Hive's canonical
    ``max(log10(raw)−9, 0)`` clamp, flooring positive-but-low accounts at 25.0;
    ``bridge.get_profile`` returns its own pre-computed score without that floor.
    So in the band where a primary score is floored up to 25.0 but the profile
    score sits below ``MIN_AUTHOR_REPUTATION``, the two paths admit/reject
    differently across a HAFSQL outage. This is left as a *conscious* residual,
    not normalised: the fallback only runs while HAFSQL is down (rare), the
    floored-positive behaviour is beem-canonical, and re-flooring the profile
    score here would wrongly lift genuinely downvoted (negative-rep) accounts
    over the gate — a worse failure than the rare differential-admission band.
    """
    if not authors:
        return {}
    result: dict[str, float] = {}
    for author in authors[:_API_REP_BATCH_CAP]:
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
                    timeout=_API_REP_TIMEOUT,
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
            # B7: bound the query — this runs synchronously on the single-threaded
            # stream/backfill path, so a slow HAFSQL query would stall the batch.
            cur.execute("SET statement_timeout = '5s'")
            # B6: ``cross_post_key`` is user-controlled json_metadata, so without
            # these filters a deleted post or a *comment* could be fed to the
            # classifier as a top-level post's topic. Restrict to live top-level
            # posts (the siblings already do).
            cur.execute(
                "SELECT body FROM hafsql.comments "
                "WHERE author = %s AND permlink = %s "
                "AND deleted = false AND parent_author = ''",
                (author, permlink),
            )
            row = cur.fetchone()
            if row:
                return row["body"]
    except Exception as exc:
        _warn_degraded("get_post_body", exc)
    return None


def get_posts_titles_and_excerpts(
    pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], dict]:
    """Batch-fetch title + body for ``(author, permlink)`` pairs from HAFSQL.

    Looks up an arbitrary set of author/permlink pairs in one round-trip —
    recent posts for the server-rendered SEO lists on ``/``, ``/c/``,
    ``/lang/``, ``/community/`` span many different authors.

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
            # B5: exclude author-deleted posts (matches the backfill/sitemap
            # deleted filters).
            cur.execute(
                "SELECT c.author, c.permlink, c.title, LEFT(c.body, 2000) AS body "
                "FROM hafsql.comments c "
                "JOIN unnest(%s::text[], %s::text[]) AS k(author, permlink) "
                "  ON c.author = k.author AND c.permlink = k.permlink "
                "WHERE c.deleted = false",
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
        _warn_degraded("get_posts_titles_and_excerpts", exc)
    return {}


def get_post_full(author: str, permlink: str) -> dict | None:
    """Fetch the full bridge.get_post response. Returns None on error/not found.

    Inlined into post-page HTML so the client can render the modal without a
    duplicate RPC call. Also feeds get_post_metadata for OG extraction.
    """
    node_errors = 0
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
            node_errors += 1
            logger.debug("post full fetch failed on %s for %s/%s: %s", node, author, permlink, exc)
            continue
    # B1: every node erroring is an outage; a not-found returns None from a
    # working node (node_errors < len) and must not warn.
    if node_errors and node_errors == len(settings.hive_api_nodes):
        _warn_degraded("get_post_full", RuntimeError(f"all {node_errors} Hive API nodes failed"))
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
            # B2: a valid discussion always contains at least the focal post,
            # so an empty ``{}`` is a bad/incomplete node response, not a real
            # "no comments". Keep failing over to the next node instead of
            # treating ``{}`` (or None) as final.
            if isinstance(thread, dict) and thread:
                break
        except Exception:
            continue  # try next node
    if not isinstance(thread, dict) or not thread:
        # B2: don't cache a single-node empty/error for 1h — let the next call
        # re-fetch once a healthy node is back. (A post that genuinely has no
        # comments still returns the focal-post entry → non-empty dict → cached
        # below as an empty children list.)
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
                -- B16: match both schemes — extract_post_metadata accepts
                -- http:// and https:// canonicals, so the sitemap must too.
                OR (json_metadata ->> 'canonical_url') ILIKE 'https://hivecomb.net/%%'
                OR (json_metadata ->> 'canonical_url') ILIKE 'http://hivecomb.net/%%'
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
    node_errors = 0
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
            node_errors += 1
            logger.debug("community lookup failed on %s for %s: %s", node, community_id, exc)
            continue
    # B1: all nodes erroring is an outage (worth a WARN — it also makes the worker
    # skip the community boost/persist for this post, B0); a not-found is a None
    # result from a working node and stays quiet.
    if node_errors and node_errors == len(settings.hive_api_nodes):
        _warn_degraded("get_community", RuntimeError(f"all {node_errors} Hive API nodes failed"))
    return None


def get_profile(account: str) -> dict | None:
    """Fetch an author's display name, bio, and reputation via Hive API
    bridge.get_profile (proposal 112).

    Returns ``{"display_name": str, "about": str, "reputation": float | None}``
    or ``None`` when every node errored (a total failure) or the account isn't
    found. Missing fields degrade to empty strings / ``None`` so a real but
    bio-less profile still returns a dict (distinct from the ``None`` failure
    sentinel — the caller caches the two differently). Mirrors ``get_community``'s
    multi-node failover; feeds the server-rendered ``/@author`` profile header.
    """
    node_errors = 0
    for node in settings.hive_api_nodes:
        try:
            resp = requests.post(
                node,
                json={
                    "jsonrpc": "2.0",
                    "method": "bridge.get_profile",
                    "params": {"account": account},
                    "id": 1,
                },
                timeout=4,
            )
            data = resp.json().get("result")
            if data is not None:
                meta = data.get("metadata")
                profile = meta.get("profile") if isinstance(meta, dict) else None
                if not isinstance(profile, dict):
                    profile = {}
                rep = data.get("reputation")
                return {
                    "display_name": profile.get("name") or "",
                    "about": profile.get("about") or "",
                    "reputation": float(rep) if isinstance(rep, (int, float)) and math.isfinite(rep) else None,
                }
        except Exception as exc:
            node_errors += 1
            logger.debug("profile lookup failed on %s for %s: %s", node, account, exc)
            continue
    # B1 (mirrored): every node erroring is an outage worth a WARN; a not-found
    # (None result from a working node) stays quiet.
    if node_errors and node_errors == len(settings.hive_api_nodes):
        _warn_degraded("get_profile", RuntimeError(f"all {node_errors} Hive API nodes failed"))
    return None

