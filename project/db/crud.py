import asyncio
import functools
import hashlib
import json as _json
import logging
import math
import re
from datetime import datetime as _dt, timedelta, timezone as _tz

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import cache as _cache
from .models import Category, Post, PostReport

logger = logging.getLogger(__name__)

_RETRY_MAX = 3
_RETRY_BACKOFF = 0.5  # seconds, doubles each attempt
_RETRYABLE_CODES = {"08000", "08003", "08006", "40001", "40P01"}  # connection / deadlock


def retry_transient(fn):
    """Retry async CRUD functions on transient DB errors (connection loss, deadlock)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(_RETRY_MAX):
            try:
                return await fn(*args, **kwargs)
            except (OperationalError, DBAPIError) as exc:
                pgcode = getattr(getattr(exc, "orig", None), "pgcode", None) or ""
                if pgcode[:5] not in _RETRYABLE_CODES and not exc.connection_invalidated:
                    raise
                last_exc = exc
                wait = _RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "transient DB error in %s (attempt %d/%d, pgcode=%s): %s — retrying in %.1fs",
                    fn.__name__, attempt + 1, _RETRY_MAX, pgcode, exc, wait,
                )
                await asyncio.sleep(wait)
        raise last_exc
    return wrapper


# ── Categories ─────────────────────────────────────────────────────────────────

async def upsert_category(session: AsyncSession, name: str) -> Category:
    await session.execute(
        insert(Category).values(name=name).on_conflict_do_nothing(index_elements=["name"])
    )
    result = await session.execute(select(Category).where(Category.name == name))
    return result.scalars().first()


async def seed_category_tree(session: AsyncSession, tree: dict[str, list[str]]) -> None:
    for parent_name, children in tree.items():
        parent = await upsert_category(session, parent_name)
        if parent.parent_id is not None:
            await session.execute(
                text("UPDATE categories SET parent_id = NULL WHERE id = :id"),
                {"id": parent.id},
            )
        await session.flush()
        for child_name in children:
            if child_name == parent_name:
                continue
            await session.execute(
                insert(Category)
                .values(name=child_name, parent_id=parent.id)
                .on_conflict_do_nothing(index_elements=["name"])
            )
            await session.execute(
                text(
                    "UPDATE categories SET parent_id = :pid "
                    "WHERE name = :name AND parent_id IS NULL"
                ),
                {"pid": parent.id, "name": child_name},
            )
    await session.commit()
    logger.info("category tree seeded: %d parents, %d leaves",
                len(tree), sum(len(v) for v in tree.values()))


async def get_category_tree(session: AsyncSession) -> list[dict]:
    """Fetch the full 2-level category tree in a single query."""
    rows = await session.execute(
        text(
            "SELECT c.id, c.name, c.parent_id, p.name AS parent_name "
            "FROM categories c "
            "LEFT JOIN categories p ON c.parent_id = p.id "
            "ORDER BY COALESCE(p.name, c.name), c.parent_id NULLS FIRST, c.name"
        )
    )
    parents: dict[str, dict] = {}
    for row in rows.mappings():
        if row["parent_id"] is None:
            parents[row["name"]] = {"id": row["id"], "name": row["name"], "children": []}
        else:
            parent = parents.get(row["parent_name"])
            if parent:
                parent["children"].append({"id": row["id"], "name": row["name"]})
    return list(parents.values())


async def _get_cached_category_tree(session: AsyncSession) -> list[dict]:
    """Category tree with 86400s in-process cache (changes only on deploy)."""
    cached = _cache.get("category_tree_internal")
    if cached is not None:
        return cached
    tree = await get_category_tree(session)
    _cache.put("category_tree_internal", tree, ttl=86400)
    return tree


# ── Posts ─────────────────────────────────────────────────────────────────────

@retry_transient
async def existing_author_permlinks(
    session: AsyncSession, pairs: list[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Return the subset of (author, permlink) pairs that already exist."""
    if not pairs:
        return set()
    # Build VALUES list for index-friendly join.
    values_clauses = []
    params = {}
    for i, (author, permlink) in enumerate(pairs):
        values_clauses.append(f"(:a{i}, :p{i})")
        params[f"a{i}"] = author
        params[f"p{i}"] = permlink
    values_sql = ", ".join(values_clauses)
    query = text(f"""
        SELECT p.author, p.permlink
        FROM posts p
        INNER JOIN (VALUES {values_sql}) AS v(author, permlink)
          ON p.author = v.author AND p.permlink = v.permlink
    """)
    rows = (await session.execute(query, params)).fetchall()
    return {(r[0], r[1]) for r in rows}


@retry_transient
async def create_post(session: AsyncSession, data: dict) -> Post:
    # Resolve category names to IDs — batch fetch existing, upsert only missing.
    cat_names = list(set(data.get("categories", [])))
    cat_ids: list[int] = []
    if cat_names:
        existing_cats = await session.execute(
            text("SELECT id, name FROM categories WHERE name = ANY(:names)"),
            {"names": cat_names},
        )
        existing_map = {r[1]: r[0] for r in existing_cats.fetchall()}
        for name in cat_names:
            if name not in existing_map:
                cat = await upsert_category(session, name)
                existing_map[name] = cat.id
        cat_ids = list(existing_map.values())

    lang_codes = data.get("languages", [])

    # Check for existing post (upsert on author+permlink).
    existing = await session.execute(
        select(Post).where(
            Post.author == data["author"],
            Post.permlink == data["permlink"],
        )
    )
    post = existing.scalars().first()
    is_new = post is None
    if post:
        post.sentiment = data.get("sentiment")
        post.sentiment_score = data.get("sentiment_score")
        if "community_id" in data:
            post.community_id = data["community_id"]
        if "primary_language" in data:
            post.primary_language = data["primary_language"]
        if "is_nsfw" in data:
            post.is_nsfw = data["is_nsfw"]
        post.category_ids = cat_ids
        post.language_codes = lang_codes
    else:
        post = Post(
            author=data["author"],
            permlink=data["permlink"],
            created=data.get("created"),
            sentiment=data.get("sentiment"),
            sentiment_score=data.get("sentiment_score"),
            community_id=data.get("community_id"),
            primary_language=data.get("primary_language"),
            is_nsfw=data.get("is_nsfw", False),
            category_ids=cat_ids,
            language_codes=lang_codes,
        )
        session.add(post)

    await session.commit()

    # Increment community post_count for new inserts only.
    if is_new and post.community_id:
        await session.execute(
            text(
                "UPDATE community_mappings SET post_count = post_count + 1 "
                "WHERE community_id = :cid"
            ),
            {"cid": post.community_id},
        )
        await session.commit()

    logger.info("saved post permlink=%s langs=%s sentiment=%s categories=%s",
                data["permlink"], data.get("languages", []), data.get("sentiment"),
                data.get("categories", []))
    return post


@retry_transient
async def get_post_by_permlink(
    session: AsyncSession, author: str, permlink: str
) -> dict | None:
    """Return a single post with its categories."""
    rows = await session.execute(
        text(
            """
            SELECT p.id, p.author, p.permlink, p.created,
                   p.sentiment, p.sentiment_score, p.community_id,
                   p.primary_language, p.is_nsfw, cm.community_name,
                   p.category_ids, p.language_codes
            FROM posts p
            LEFT JOIN community_mappings cm ON cm.community_id = p.community_id
            WHERE p.author = :author AND p.permlink = :pl
            """
        ),
        {"author": author, "pl": permlink},
    )
    row = rows.mappings().first()
    if not row:
        return None
    post = dict(row)

    posts = await _attach_categories_and_languages(session, [post], [post["id"]])
    return posts[0]


# ── Centroids (pgvector) ──────────────────────────────────────────────────────

async def get_centroids(session: AsyncSession) -> dict[str, list[float]]:
    rows = await session.execute(
        text("SELECT category_name, CAST(centroid AS text) FROM category_centroids")
    )
    centroids: dict[str, list[float]] = {}
    for name, vec_str in rows.fetchall():
        try:
            centroids[name] = [float(x) for x in vec_str.strip("[]").split(",")]
        except (ValueError, AttributeError) as exc:
            logger.error("Corrupted centroid for %s, skipping: %s", name, exc)
    return centroids


async def save_centroids(
    session: AsyncSession, centroids: dict[str, list[float]], metadata: dict
) -> None:
    for cat, vec in centroids.items():
        await session.execute(
            text(
                """
                INSERT INTO category_centroids
                    (category_name, centroid, post_count, llm_model, embedding_model)
                VALUES
                    (:cat, CAST(:vec AS vector), :count, :llm, :emb)
                ON CONFLICT (category_name) DO UPDATE SET
                    centroid        = CAST(EXCLUDED.centroid AS vector),
                    post_count      = EXCLUDED.post_count,
                    llm_model       = EXCLUDED.llm_model,
                    embedding_model = EXCLUDED.embedding_model
                """
            ),
            {
                "cat": cat,
                "vec": "[" + ",".join(f"{v:.8f}" for v in vec) + "]",
                "count": metadata.get("posts_labeled", 0),
                "llm": metadata.get("llm_model", ""),
                "emb": metadata.get("embedding_model", ""),
            },
        )
    await session.commit()
    logger.info("saved %d centroids to pgvector", len(centroids))


# ── Stream cursors ────────────────────────────────────────────────────────────

@retry_transient
async def get_cursor(session: AsyncSession, key: str) -> int | None:
    row = await session.execute(
        text("SELECT block_num FROM stream_cursors WHERE key = :key"),
        {"key": key},
    )
    result = row.fetchone()
    return result[0] if result else None


@retry_transient
async def set_cursor(session: AsyncSession, key: str, block_num: int) -> None:
    await session.execute(
        text(
            """
            INSERT INTO stream_cursors (key, block_num, updated_at)
            VALUES (:key, :block_num, NOW())
            ON CONFLICT (key) DO UPDATE SET
                block_num  = EXCLUDED.block_num,
                updated_at = NOW()
            """
        ),
        {"key": key, "block_num": block_num},
    )
    await session.commit()


# ── Batch helpers ─────────────────────────────────────────────────────────────

async def _attach_categories_and_languages(
    session: AsyncSession, posts: list[dict], post_ids: list[int]
) -> list[dict]:
    """Resolve category IDs to names via in-memory tree, attach languages from row."""
    tree = await _get_cached_category_tree(session)
    id_to_name: dict[int, str] = {}
    for parent in tree:
        id_to_name[parent["id"]] = parent["name"]
        for child in parent.get("children", []):
            id_to_name[child["id"]] = child["name"]

    for post in posts:
        post["categories"] = [
            id_to_name[cid] for cid in post.get("category_ids", []) if cid in id_to_name
        ]
        post["languages"] = post.get("language_codes", [])

    return posts


async def _resolve_category_ids(session: AsyncSession, names: list[str]) -> list[int]:
    """Map category names to IDs, expanding parents to all children."""
    tree = await _get_cached_category_tree(session)
    name_to_id: dict[str, int] = {}
    parent_children: dict[int, list[int]] = {}
    parent_ids: set[int] = set()
    for parent in tree:
        name_to_id[parent["name"]] = parent["id"]
        parent_ids.add(parent["id"])
        parent_children[parent["id"]] = [c["id"] for c in parent.get("children", [])]
        for child in parent.get("children", []):
            name_to_id[child["name"]] = child["id"]
    ids: set[int] = set()
    for name in names:
        cid = name_to_id.get(name)
        if cid is None:
            continue
        if cid in parent_ids:
            ids.update(parent_children.get(cid, []))
        else:
            ids.add(cid)
    return list(ids)


# ── Browse & discovery ────────────────────────────────────────────────────────


def _browse_count_cache_key(categories, languages, sentiment, community=None, communities=None, authors=None, include_nsfw=False, nsfw_only=False, max_age=None):
    raw = _json.dumps({"c": sorted(categories or []),
                       "l": sorted(languages or []),
                       "s": sentiment,
                       "m": community,
                       "ms": sorted(communities) if communities else None,
                       "a": sorted(authors) if authors else None,
                       "nsfw": include_nsfw,
                       "nsfw_only": nsfw_only,
                       "age": max_age}, sort_keys=True)
    return f"browse_count:{hashlib.sha256(raw.encode()).hexdigest()}"

@retry_transient
async def browse_posts(
    session: AsyncSession,
    categories: list[str] | None = None,
    languages: list[str] | None = None,
    sentiment: str | None = None,
    community: str | None = None,
    communities: list[str] | None = None,
    authors: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
    cursor: str | None = None,
    include_nsfw: bool = False,
    nsfw_only: bool = False,
    max_age: str | None = None,
    sort: str | None = None,
) -> dict:
    """Browse all posts with optional filters.

    Supports two pagination modes:
    - **cursor** (preferred): pass the ``next_cursor`` value from the previous
      response.  Uses keyset pagination — O(1) regardless of page depth.
    - **offset** (legacy): OFFSET-based, degrades at high page numbers.

    Returns ``{"posts": [...], "next_cursor": "..." | null}``.
    """
    # Validate sort parameter.
    sort_order = "newest"
    if sort and sort in ("newest", "oldest"):
        sort_order = sort

    conditions = ["p.category_ids != '{}'"]
    if nsfw_only:
        conditions.append("p.is_nsfw = true")
    elif not include_nsfw:
        conditions.append("p.is_nsfw = false")
    params: dict = {"lim": limit}

    # max_age filter: restrict to posts newer than cutoff.
    if max_age:
        m = re.fullmatch(r"(\d+)([hd])", max_age)
        if m:
            value, unit = int(m.group(1)), m.group(2)
            valid = (unit == "h" and 1 <= value <= 24) or (unit == "d" and 1 <= value <= 7)
            if valid:
                delta = timedelta(hours=value) if unit == "h" else timedelta(days=value)
                params["age_cutoff"] = _dt.now(_tz.utc) - delta
                conditions.append("p.created > :age_cutoff")

    # Cursor-based keyset pagination takes priority over offset.
    use_cursor = False
    if cursor:
        try:
            ts_str, id_str = cursor.rsplit("_", 1)
            params["cursor_created"] = _dt.fromtimestamp(float(ts_str), tz=_tz.utc)
            params["cursor_id"] = int(id_str)
            if sort_order == "oldest":
                conditions.append("(p.created, p.id) > (:cursor_created, :cursor_id)")
            else:
                conditions.append("(p.created, p.id) < (:cursor_created, :cursor_id)")
            use_cursor = True
        except (ValueError, TypeError) as exc:
            logger.debug("Malformed browse cursor %r: %s — falling back to offset", cursor, exc)

    if not use_cursor:
        params["off"] = offset

    if languages:
        conditions.append("p.language_codes && CAST(:languages AS text[])")
        params["languages"] = languages
    if sentiment:
        conditions.append("p.sentiment = :sent")
        params["sent"] = sentiment
    # Track lateral-join candidates separately from regular conditions.
    # For 2–50 values, lateral join uses per-value index scans (fast);
    # for >50 values, = ANY() lets PG pick incremental sort (also fast).
    _LATERAL_THRESHOLD = 50
    use_lateral = None  # None | "authors" | "communities"

    if communities:
        if 2 <= len(communities) <= _LATERAL_THRESHOLD:
            use_lateral = "communities"
            params["lateral_values"] = communities
        else:
            conditions.append("p.community_id = ANY(CAST(:communities AS text[]))")
            params["communities"] = communities
    elif community:
        conditions.append("p.community_id = :community")
        params["community"] = community

    if authors:
        if not use_lateral and 2 <= len(authors) <= _LATERAL_THRESHOLD:
            use_lateral = "authors"
            params["lateral_values"] = authors
        else:
            conditions.append("p.author = ANY(CAST(:authors AS text[]))")
            params["authors"] = authors

    if categories:
        resolved_cat_ids = await _resolve_category_ids(session, categories)
        if resolved_cat_ids:
            conditions.append("p.category_ids && CAST(:cat_ids AS int[])")
            params["cat_ids"] = resolved_cat_ids
        else:
            # No valid categories resolved — return empty result.
            return {"posts": [], "next_cursor": None, "total": 0}

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    # Count query always uses = ANY() (no ordering needed, bitmap scan is fine).
    count_conditions = list(conditions)
    count_params_extra: dict = {}
    if use_lateral == "authors":
        count_conditions.append("p.author = ANY(CAST(:authors AS text[]))")
        count_params_extra["authors"] = params["lateral_values"]
    elif use_lateral == "communities":
        count_conditions.append("p.community_id = ANY(CAST(:communities AS text[]))")
        count_params_extra["communities"] = params["lateral_values"]

    # Filtered total count — cached with 30s TTL, keyed by filter combination.
    # Skip entirely on cursor pages (UI already has the count from page 1).
    if use_cursor:
        total = None
    else:
        count_key = _browse_count_cache_key(categories, languages, sentiment, community, communities, authors, include_nsfw, nsfw_only, max_age=max_age)
        total = _cache.get(count_key)
        if total is None:
            has_filters = any([categories, languages, sentiment, community, communities, authors, max_age])
            count_conds_no_cursor = [c for c in count_conditions if ":cursor_created" not in c]
            count_where = "WHERE " + " AND ".join(count_conds_no_cursor) if count_conds_no_cursor else ""
            count_params = {k: v for k, v in params.items() if k not in ("lim", "off", "cursor_created", "cursor_id", "lateral_values")}
            count_params.update(count_params_extra)

            if not has_filters and not include_nsfw and not nsfw_only:
                # Unfiltered base case: use pg_class.reltuples (O(1) approximate).
                approx_row = await session.execute(
                    text("SELECT CAST(reltuples AS bigint) FROM pg_class WHERE relname = 'posts'")
                )
                total = approx_row.scalar()
                if total is None or total <= 0:
                    fallback = await session.execute(
                        text(f"SELECT COUNT(*) FROM posts p {count_where}"),
                        count_params,
                    )
                    total = fallback.scalar()
            else:
                # Filtered: on large tables use planner estimate (O(1)) instead
                # of exact COUNT(*) which scans millions of rows.
                approx_row = await session.execute(
                    text("SELECT CAST(reltuples AS bigint) FROM pg_class WHERE relname = 'posts'")
                )
                table_size = approx_row.scalar() or 0
                if table_size > 500_000:
                    explain_row = await session.execute(
                        text(f"EXPLAIN SELECT 1 FROM posts p {count_where}"),
                        count_params,
                    )
                    plan_line = explain_row.scalar() or ""
                    m = re.search(r"rows=(\d+)", plan_line)
                    total = int(m.group(1)) if m else 0
                else:
                    count_rows = await session.execute(
                        text(f"SELECT COUNT(*) FROM posts p {count_where}"),
                        count_params,
                    )
                    total = count_rows.scalar()
            _cache.put(count_key, total, ttl=300)

    offset_clause = "" if use_cursor else "OFFSET :off"
    order_dir = "ASC" if sort_order == "oldest" else "DESC"

    if use_lateral:
        # Lateral join: per-value index scan → merge top-N.
        # Community name join is outside the final LIMIT (1 scan, not N).
        if use_lateral == "authors":
            lateral_col = "p.author"
        else:
            lateral_col = "p.community_id"
        rows = await session.execute(
            text(
                f"""
                SELECT sub2.*, cm.community_name
                FROM (
                    SELECT sub.*
                    FROM unnest(CAST(:lateral_values AS text[])) AS _lv(val)
                    CROSS JOIN LATERAL (
                        SELECT p.id, p.author, p.permlink, p.created,
                               p.sentiment, p.sentiment_score, p.community_id,
                               p.primary_language, p.is_nsfw,
                               p.category_ids, p.language_codes
                        FROM posts p
                        {where + " AND " if where else "WHERE "}{lateral_col} = _lv.val
                        ORDER BY p.created {order_dir}, p.id {order_dir}
                        LIMIT :lim
                    ) sub
                    ORDER BY sub.created {order_dir}, sub.id {order_dir}
                    LIMIT :lim {offset_clause}
                ) sub2
                LEFT JOIN community_mappings cm ON cm.community_id = sub2.community_id
                """
            ),
            {k: v for k, v in params.items() if k != "lateral_values" or True},
        )
    else:
        rows = await session.execute(
            text(
                f"""
                SELECT p.id, p.author, p.permlink, p.created,
                       p.sentiment, p.sentiment_score, p.community_id,
                       p.primary_language, p.is_nsfw, cm.community_name,
                       p.category_ids, p.language_codes
                FROM posts p
                LEFT JOIN community_mappings cm ON cm.community_id = p.community_id
                {where}
                ORDER BY p.created {order_dir}, p.id {order_dir}
                LIMIT :lim {offset_clause}
                """
            ),
            params,
        )
    posts = [dict(r) for r in rows.mappings()]

    if posts:
        post_ids = [p["id"] for p in posts]
        posts = await _attach_categories_and_languages(session, posts, post_ids)

    # Build next_cursor from last post's (created, id) using epoch timestamp (URL-safe).
    next_cursor = None
    if posts and len(posts) == limit:
        last = posts[-1]
        if last["created"]:
            ts = last["created"].timestamp()
            next_cursor = f"{ts}_{last['id']}"

    return {"posts": posts, "next_cursor": next_cursor, "total": total}


@retry_transient
async def get_available_languages(session: AsyncSession) -> list[dict]:
    """Get distinct languages with post counts."""
    rows = await session.execute(
        text(
            "SELECT lang AS language, COUNT(*) AS count "
            "FROM posts, unnest(language_codes) AS lang "
            "WHERE language_codes != '{}' "
            "GROUP BY lang ORDER BY count DESC"
        )
    )
    return [dict(r) for r in rows.mappings()]


@retry_transient
async def get_seo_eligible_language_counts(session: AsyncSession) -> list[dict]:
    """Per-language counts of **SEO-eligible** posts — classified
    (``category_ids <> '{}'``) and non-NSFW — as ``{"language", "count"}`` dicts
    ordered by count descending.

    This is the same eligibility gate ``get_recent_posts_for_seo`` applies, so a
    language's count here is exactly the pool of posts that can populate its
    server-rendered ``/lang/{lang}`` recent-post primer. The ``/lang`` sitemap
    threshold and the thin-page ``noindex`` floor both read it (proposal 106) to
    keep below-floor language pages — whose primer is the page's only unique
    server text (``intro=''``) — out of the index and off the advertised crawl
    set. Distinct from ``get_available_languages`` (which counts *all* posts in a
    language, NSFW/unclassified included, for the filter UI). Cached 1h: language
    volumes shift slowly and this is a full-table aggregate."""
    async def _compute() -> list[dict]:
        rows = await session.execute(
            text(
                "SELECT lang AS language, COUNT(*) AS count "
                "FROM posts, unnest(language_codes) AS lang "
                "WHERE category_ids != '{}' AND is_nsfw = false "
                "GROUP BY lang ORDER BY count DESC"
            )
        )
        return [dict(r) for r in rows.mappings()]

    return await _cache.get_or_compute("seo_lang_counts", 3600, _compute)


@retry_transient
async def get_overview_stats(session: AsyncSession) -> dict:
    """Get overview statistics using fast approximations."""
    # O(1) approximate row count from pg_class (autovacuum keeps this fresh).
    row = await session.execute(
        text("SELECT CAST(reltuples AS bigint) FROM pg_class WHERE relname = 'posts'")
    )
    total = row.scalar()
    if total is None or total < 0:
        # Fallback: reltuples is -1 when never analyzed (e.g. test DB).
        fallback = await session.execute(text("SELECT COUNT(*) FROM posts"))
        total = fallback.scalar() or 0

    # Language count from cached languages list (avoids full table scan).
    langs = _cache.get("languages")
    if langs is not None:
        lang_count = len(langs.get("languages", []))
    else:
        # Fallback: fast distinct count if languages cache cold.
        lang_row = await session.execute(
            text(
                "SELECT COUNT(DISTINCT unnest) FROM ("
                "  SELECT unnest(language_codes) FROM posts WHERE language_codes != '{}'"
                ") t"
            )
        )
        lang_count = lang_row.scalar() or 0

    return {"total_posts": total, "languages": lang_count}


# ── Blacklist sweep ──────────────────────────────────────────────────────────

@retry_transient
async def get_distinct_authors(
    session: AsyncSession, limit: int = 10_000, offset: int = 0
) -> list[str]:
    """Return distinct authors that have posts in the DB, paginated."""
    rows = await session.execute(
        text("SELECT DISTINCT author FROM posts ORDER BY author LIMIT :lim OFFSET :off"),
        {"lim": limit, "off": offset},
    )
    return [r[0] for r in rows.fetchall()]


async def get_nsfw_author_permlinks(
    session: AsyncSession, pairs: list[tuple[str, str]],
) -> set[tuple[str, str]]:
    """Return the subset of (author, permlink) pairs flagged is_nsfw in our DB.

    Used to scrub NSFW posts from public crawler-facing surfaces (e.g. the
    sitemap) by reusing the same NSFW logic the worker already applies, rather
    than re-deriving it from HAFSQL json_metadata at read time.
    """
    if not pairs:
        return set()
    authors = [a for a, _ in pairs]
    permlinks = [p for _, p in pairs]
    rows = await session.execute(
        text(
            "SELECT p.author, p.permlink FROM posts p "
            "JOIN unnest(CAST(:authors AS text[]), CAST(:permlinks AS text[])) "
            "  AS k(author, permlink) "
            "  ON p.author = k.author AND p.permlink = k.permlink "
            "WHERE p.is_nsfw = true"
        ),
        {"authors": authors, "permlinks": permlinks},
    )
    return {(r[0], r[1]) for r in rows.fetchall()}


async def get_recently_active_authors(
    session: AsyncSession, days: int = 60, limit: int = 1000
) -> list[tuple[str, _dt]]:
    """Return (author, last_post_created) for authors with a classified post
    in the last `days` days, ordered by most-recent-post DESC, capped at `limit`.

    Used for the sitemap — author profile pages are a unique aggregation
    surface (not duplicate content from other UIs), worth indexing.
    """
    rows = await session.execute(
        text(
            "SELECT author, MAX(created) AS last_created FROM posts "
            "WHERE category_ids != '{}' "
            "AND created >= NOW() - make_interval(days => :days) "
            "GROUP BY author ORDER BY last_created DESC LIMIT :lim"
        ),
        {"days": days, "lim": limit},
    )
    return [(r[0], r[1]) for r in rows.fetchall()]


@retry_transient
async def get_author_summary(session: AsyncSession, author: str) -> dict | None:
    """Aggregate stats for the author profile page (proposals 096 + 098).

    Canonical home of the author aggregation — both the server-rendered
    ``/@author`` summary (098) and the post-page author mini-card (096) consume
    this single function rather than building two near-duplicate queries.

    Returns::

        {
            "total_posts": int,
            "top_categories": [{"id": str, "name": str, "count": int}],  # up to 3
            "top_languages": [{"code": str, "count": int}],              # up to 2
            "top_community": {"id": str, "name": str, "count": int} | None,
            "first_seen": datetime,
            "last_seen": datetime,
        }

    Returns ``None`` if the author has zero classified posts. The result is
    cached in-process for 6h — author stats change slowly and these are
    background SEO fetches, not user-blocking. Only real authors (>=1 post) are
    cached, so the cache stays bounded by O(active authors), not by every
    random username a crawler probes. All queries filter on the indexed
    ``author`` column, so cost is sub-10ms even for prolific authors.
    """
    cache_key = f"author_summary:{author}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    totals = await session.execute(
        text(
            "SELECT COUNT(*) AS total, MIN(created) AS first_seen, MAX(created) AS last_seen "
            "FROM posts WHERE author = :author AND category_ids != '{}'"
        ),
        {"author": author},
    )
    trow = totals.mappings().first()
    total = int(trow["total"]) if trow else 0
    if total == 0:
        return None

    # Floor: a category must cover ≥5% of the author's classified posts to
    # qualify for the top-3 summary. Without this, one stray match (e.g. a
    # single HiveFest giveaway post hitting the "contests" centroid) can sit
    # next to dominant categories with 10×+ the volume. `max(1, ...)` keeps
    # small/new authors visible — at low totals every match still counts.
    cat_floor = max(1, math.ceil(total * 0.05))
    cat_rows = await session.execute(
        text(
            "SELECT c.name AS name, t.n AS count "
            "FROM ("
            "  SELECT cid, COUNT(*) AS n "
            "  FROM posts, unnest(category_ids) AS cid "
            "  WHERE author = :author AND category_ids != '{}' "
            "  GROUP BY cid HAVING COUNT(*) >= :floor "
            "  ORDER BY n DESC LIMIT 3"
            ") t JOIN categories c ON c.id = t.cid "
            "ORDER BY t.n DESC"
        ),
        {"author": author, "floor": cat_floor},
    )
    # Category name doubles as the /c/{slug} route key — id == name (slug).
    top_categories = [
        {"id": r["name"], "name": r["name"], "count": int(r["count"])}
        for r in cat_rows.mappings()
    ]

    lang_rows = await session.execute(
        text(
            "SELECT lang AS code, COUNT(*) AS count "
            "FROM posts, unnest(language_codes) AS lang "
            "WHERE author = :author AND language_codes != '{}' "
            "GROUP BY lang ORDER BY count DESC LIMIT 2"
        ),
        {"author": author},
    )
    top_languages = [
        {"code": r["code"], "count": int(r["count"])} for r in lang_rows.mappings()
    ]

    comm_row = await session.execute(
        text(
            "SELECT p.community_id AS id, cm.community_name AS name, COUNT(*) AS count "
            "FROM posts p "
            "LEFT JOIN community_mappings cm ON cm.community_id = p.community_id "
            "WHERE p.author = :author AND p.community_id IS NOT NULL "
            "GROUP BY p.community_id, cm.community_name "
            "ORDER BY count DESC LIMIT 1"
        ),
        {"author": author},
    )
    crow = comm_row.mappings().first()
    top_community = (
        {"id": crow["id"], "name": crow["name"] or crow["id"], "count": int(crow["count"])}
        if crow
        else None
    )

    summary = {
        "total_posts": total,
        "top_categories": top_categories,
        "top_languages": top_languages,
        "top_community": top_community,
        "first_seen": trow["first_seen"],
        "last_seen": trow["last_seen"],
    }
    _cache.put(cache_key, summary, ttl=21600)
    return summary


@retry_transient
async def get_recent_posts_for_seo(
    session: AsyncSession,
    *,
    category: str | None = None,
    language: str | None = None,
    community: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """Recent classified posts (with titles + plain-text excerpts) for the
    server-rendered SEO lists on ``/``, ``/c/{cat}``, ``/lang/{lang}``,
    ``/community/{id}`` (proposal 100, Phase 1).

    Returns up to ``limit`` posts matching the optional filter, newest first.
    Each entry is ``{"author", "permlink", "title", "excerpt", "created"}``.

    Two-step (PG → HAFSQL): our ``posts`` table holds
    author/permlink/created/category_ids/language_codes/community_id but not
    titles/bodies, so we read the recent matching permlinks from PG then
    look titles + bodies up in HAFSQL. NSFW posts are excluded (these are public
    crawler-facing surfaces). Excerpts are ``clean_post_body`` output capped at
    280 chars.

    Cached for 5 minutes (a double-checked per-key lock collapses a cold-cache
    crawler stampede into a single PG+HAFSQL round-trip instead of N parallel
    ones) — fresh enough that the homepage isn't stale, long enough that crawler
    traffic doesn't hit HAFSQL on every request.

    **Error contract (two layers).** This function does *not* self-degrade — a
    HAFSQL failure propagates to the caller. The route-layer ``_safe_recent_posts``
    wrapper (``ui.py``) is what absorbs it and degrades the surface to "no
    recent-posts primer". Only a genuinely empty result (no matching posts in PG,
    or PG rows whose titles HAFSQL can't supply) is cached as ``[]`` — so a
    "no posts" surface doesn't re-query every crawl, while a HAFSQL incident is
    never masked as an (cached) empty page.
    """
    cache_key = f"recent_seo:{category or ''}:{language or ''}:{community or ''}:{limit}"

    async def _compute() -> list[dict]:
        conditions = ["category_ids != '{}'", "is_nsfw = false"]
        params: dict = {"lim": limit}
        if language:
            conditions.append("language_codes && CAST(:langs AS text[])")
            params["langs"] = [language]
        if community:
            conditions.append("community_id = :community")
            params["community"] = community
        if category:
            cat_ids = await _resolve_category_ids(session, [category])
            if not cat_ids:
                return []
            conditions.append("category_ids && CAST(:cat_ids AS int[])")
            params["cat_ids"] = cat_ids

        where = "WHERE " + " AND ".join(conditions)
        rows = await session.execute(
            text(
                f"SELECT author, permlink, created FROM posts {where} "
                "ORDER BY created DESC, id DESC LIMIT :lim"
            ),
            params,
        )
        meta = [(r["author"], r["permlink"], r["created"]) for r in rows.mappings()]
        if not meta:
            return []

        from ..hafsql import get_posts_titles_and_excerpts
        from ..text import clean_post_body
        info = await asyncio.to_thread(
            get_posts_titles_and_excerpts, [(a, p) for a, p, _ in meta]
        )
        result: list[dict] = []
        for author, permlink, created in meta:
            entry = info.get((author, permlink))
            if not entry or not entry.get("title"):
                continue
            excerpt = clean_post_body(entry.get("body") or "")[:280].rstrip()
            result.append({
                "author": author,
                "permlink": permlink,
                "title": entry["title"],
                "excerpt": excerpt,
                "created": created,
            })
        return result

    return await _cache.get_or_compute(cache_key, 300, _compute)


@retry_transient
async def delete_posts_by_author(session: AsyncSession, author: str) -> int:
    """Delete all posts for a blacklisted author. Returns count deleted."""
    # Collect community post counts before deleting (for post_count decrement).
    comm_rows = await session.execute(
        text(
            "SELECT community_id, COUNT(*) AS cnt FROM posts "
            "WHERE author = :author AND community_id IS NOT NULL "
            "GROUP BY community_id"
        ),
        {"author": author},
    )
    comm_counts = comm_rows.fetchall()

    result = await session.execute(
        text("DELETE FROM posts WHERE author = :author"),
        {"author": author},
    )
    await session.commit()

    # Decrement community post_counts.
    for cid, cnt in comm_counts:
        await session.execute(
            text(
                "UPDATE community_mappings SET post_count = post_count - :cnt "
                "WHERE community_id = :cid"
            ),
            {"cid": cid, "cnt": cnt},
        )
    if comm_counts:
        await session.commit()

    return result.rowcount


# ── Communities ──────────────────────────────────────────────────────────────


@retry_transient
async def get_available_communities(session: AsyncSession) -> list[dict]:
    """Get communities that have posts, with post counts and display names."""
    rows = await session.execute(
        text(
            "SELECT community_id AS id, "
            "       community_name AS name, "
            "       category_slug AS category, "
            "       post_count "
            "FROM community_mappings "
            "WHERE post_count > 0 "
            "ORDER BY post_count DESC"
        )
    )
    return [dict(r) for r in rows.mappings()]


_COMMUNITY_NAME_TTL = 3600        # display names change rarely
_COMMUNITY_NAME_MISS_TTL = 300    # shorter, so a freshly-mapped community shows up


@retry_transient
async def get_community_name(
    session: AsyncSession, community_id: str
) -> str | None:
    """Return the worker-denormalized display name for a community, or None if
    there's no mapping row. Used by the ``/community/{id}`` SEO page heading
    (proposal 100); the caller falls back to the bare ``hive-NNNNNN`` id.

    Cached in-process (keyed by ``community_id``) so crawler traffic to a
    community page doesn't run a PG lookup on every load. An unmapped community
    is cached as ``""`` (a sentinel, since ``cache.get`` can't represent a cached
    ``None``) with a shorter TTL so a community the worker maps later picks up its
    name within minutes rather than an hour."""
    cache_key = f"community_name:{community_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached or None  # "" sentinel → unmapped
    row = await session.execute(
        text("SELECT community_name FROM community_mappings WHERE community_id = :cid"),
        {"cid": community_id},
    )
    name = row.scalar() or None
    _cache.put(
        cache_key,
        name or "",
        ttl=_COMMUNITY_NAME_TTL if name else _COMMUNITY_NAME_MISS_TTL,
    )
    return name


@retry_transient
async def upsert_community_mapping(
    session: AsyncSession,
    community_id: str,
    category_slug: str | None,
    community_name: str,
    score: float,
) -> None:
    """Persist a community-to-category mapping (worker calls this)."""
    await session.execute(
        text(
            """
            INSERT INTO community_mappings
                (community_id, category_slug, community_name, score, updated_at)
            VALUES (:cid, :cat, :name, :score, NOW())
            ON CONFLICT (community_id) DO UPDATE SET
                category_slug  = EXCLUDED.category_slug,
                community_name = EXCLUDED.community_name,
                score          = EXCLUDED.score,
                updated_at     = NOW()
            """
        ),
        {"cid": community_id, "cat": category_slug, "name": community_name, "score": score},
    )
    await session.commit()


@retry_transient
async def get_suggested_communities(
    session: AsyncSession, categories: list[str],
) -> list[dict]:
    """Get communities whose mapped category matches any of the given slugs.

    Reads directly from community_mappings (post_count denormalized).
    Returns up to 10 results sorted by post_count descending.
    """
    if not categories:
        return []
    rows = await session.execute(
        text(
            "SELECT community_id AS id, "
            "       community_name AS name, "
            "       category_slug AS category, "
            "       post_count "
            "FROM community_mappings "
            "WHERE category_slug = ANY(CAST(:cats AS text[])) "
            "ORDER BY post_count DESC "
            "LIMIT 10"
        ),
        {"cats": categories},
    )
    return [dict(r) for r in rows.mappings()]


# ── Post Reports ─────────────────────────────────────────────────────────────


@retry_transient
async def create_post_report(
    session: AsyncSession,
    post_id: int,
    reporter: str,
    reason: str,
    signature: str,
    message: str,
) -> dict:
    """Insert a misclassification report. Returns the created report as a dict.

    Raises sqlalchemy.exc.IntegrityError on duplicate (post_id, reporter).
    """
    report = PostReport(
        post_id=post_id,
        reporter=reporter,
        reason=reason,
        signature=signature,
        message=message,
    )
    session.add(report)
    await session.flush()
    result = {
        "id": report.id,
        "post_id": report.post_id,
        "reporter": report.reporter,
        "reason": report.reason,
        "created_at": report.created_at,
    }
    await session.commit()
    return result


@retry_transient
async def list_post_reports(
    session: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    post_author: str | None = None,
    post_permlink: str | None = None,
    reporter: str | None = None,
) -> dict:
    """List misclassification reports with pagination and optional filters."""
    conditions = []
    params: dict = {"lim": limit, "off": offset}

    if post_author:
        conditions.append("p.author = :post_author")
        params["post_author"] = post_author
    if post_permlink:
        conditions.append("p.permlink = :post_permlink")
        params["post_permlink"] = post_permlink
    if reporter:
        conditions.append("r.reporter = :reporter")
        params["reporter"] = reporter

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    count_row = await session.execute(
        text(
            f"SELECT COUNT(*) FROM post_reports r "
            f"JOIN posts p ON p.id = r.post_id {where}"
        ),
        params,
    )
    total = count_row.scalar()

    rows = await session.execute(
        text(
            f"""
            SELECT r.id, r.reporter, r.reason, r.created_at,
                   r.post_id, p.author AS post_author, p.permlink AS post_permlink,
                   p.category_ids
            FROM post_reports r
            JOIN posts p ON p.id = r.post_id
            {where}
            ORDER BY r.created_at DESC
            LIMIT :lim OFFSET :off
            """
        ),
        params,
    )
    raw_rows = rows.mappings().fetchall()

    # Resolve category IDs to names via in-memory tree.
    tree = await _get_cached_category_tree(session)
    id_to_name: dict[int, str] = {}
    for parent in tree:
        id_to_name[parent["id"]] = parent["name"]
        for child in parent.get("children", []):
            id_to_name[child["id"]] = child["name"]

    reports = []
    for row in raw_rows:
        cat_names = [id_to_name[cid] for cid in (row["category_ids"] or []) if cid in id_to_name]
        reports.append({
            "id": row["id"],
            "reporter": row["reporter"],
            "reason": row["reason"],
            "created_at": row["created_at"],
            "post": {
                "author": row["post_author"],
                "permlink": row["post_permlink"],
                "categories": cat_names,
            },
        })

    return {"reports": reports, "total": total, "limit": limit, "offset": offset}


