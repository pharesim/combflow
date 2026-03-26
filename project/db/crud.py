import asyncio
import functools
import hashlib
import json as _json
import logging
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
    # Check for existing post (upsert on author+permlink).
    existing = await session.execute(
        select(Post).where(
            Post.author == data["author"],
            Post.permlink == data["permlink"],
        )
    )
    post = existing.scalars().first()
    if post:
        post.sentiment = data.get("sentiment")
        post.sentiment_score = data.get("sentiment_score")
        if "community_id" in data:
            post.community_id = data["community_id"]
        if "primary_language" in data:
            post.primary_language = data["primary_language"]
        if "is_nsfw" in data:
            post.is_nsfw = data["is_nsfw"]
        # Clear old categories and languages.
        await session.execute(
            text("DELETE FROM post_category WHERE post_id = :pid"),
            {"pid": post.id},
        )
        await session.execute(
            text("DELETE FROM post_language WHERE post_id = :pid"),
            {"pid": post.id},
        )
        await session.flush()
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
        )
        session.add(post)
        await session.flush()

    # Add categories via raw SQL to avoid relationship lazy loading.
    for name in data.get("categories", []):
        cat = await upsert_category(session, name)
        await session.execute(
            text(
                "INSERT INTO post_category (post_id, category_id) "
                "VALUES (:pid, :cid) ON CONFLICT DO NOTHING"
            ),
            {"pid": post.id, "cid": cat.id},
        )

    # Add languages via junction table.
    for lang in data.get("languages", []):
        await session.execute(
            text(
                "INSERT INTO post_language (post_id, language) "
                "VALUES (:pid, :lang) ON CONFLICT DO NOTHING"
            ),
            {"pid": post.id, "lang": lang},
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
                   p.primary_language, p.is_nsfw, cm.community_name
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
    """Batch-fetch categories and languages for a list of posts (avoids N+1)."""
    cats = await session.execute(
        text(
            "SELECT pc.post_id, c.name FROM categories c "
            "JOIN post_category pc ON c.id = pc.category_id "
            "WHERE pc.post_id = ANY(:ids)"
        ),
        {"ids": post_ids},
    )
    cat_map: dict[int, list[str]] = {}
    for row in cats.fetchall():
        cat_map.setdefault(row[0], []).append(row[1])

    langs = await session.execute(
        text("SELECT post_id, language FROM post_language WHERE post_id = ANY(:ids)"),
        {"ids": post_ids},
    )
    lang_map: dict[int, list[str]] = {}
    for row in langs.fetchall():
        lang_map.setdefault(row[0], []).append(row[1])

    for post in posts:
        post["categories"] = cat_map.get(post["id"], [])
        post["languages"] = lang_map.get(post["id"], [])

    return posts


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

    conditions = [
        "EXISTS (SELECT 1 FROM post_category WHERE post_id = p.id)",
    ]
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
        conditions.append(
            "EXISTS (SELECT 1 FROM post_language pl_f "
            "WHERE pl_f.post_id = p.id AND pl_f.language = ANY(CAST(:languages AS text[])))"
        )
        params["languages"] = languages
    if sentiment:
        conditions.append("p.sentiment = :sent")
        params["sent"] = sentiment
    if communities:
        conditions.append("p.community_id = ANY(CAST(:communities AS text[]))")
        params["communities"] = communities
    elif community:
        conditions.append("p.community_id = :community")
        params["community"] = community

    if authors:
        conditions.append("p.author = ANY(CAST(:authors AS text[]))")
        params["authors"] = authors

    cat_join = ""
    if categories:
        cat_join = (
            "JOIN post_category pc_f ON p.id = pc_f.post_id "
            "JOIN categories c_f ON pc_f.category_id = c_f.id"
        )
        cat_conds = []
        for i, cat in enumerate(categories):
            cat_conds.append(f":cat_{i}")
            params[f"cat_{i}"] = cat
        cat_list = ", ".join(cat_conds)
        conditions.append(
            f"(c_f.name IN ({cat_list}) OR c_f.parent_id IN "
            f"(SELECT id FROM categories WHERE name IN ({cat_list})))"
        )

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    # Filtered total count — cached with 30s TTL, keyed by filter combination.
    count_key = _browse_count_cache_key(categories, languages, sentiment, community, communities, authors, include_nsfw, nsfw_only, max_age=max_age)
    total = _cache.get(count_key)
    if total is None:
        count_conditions = [c for c in conditions if ":cursor_created" not in c]
        count_where = "WHERE " + " AND ".join(count_conditions) if count_conditions else ""
        count_params = {k: v for k, v in params.items() if k not in ("lim", "off", "cursor_created", "cursor_id")}
        count_rows = await session.execute(
            text(f"SELECT COUNT(DISTINCT p.id) FROM posts p {cat_join} {count_where}"),
            count_params,
        )
        total = count_rows.scalar()
        _cache.put(count_key, total, ttl=30)

    offset_clause = "" if use_cursor else "OFFSET :off"

    rows = await session.execute(
        text(
            f"""
            SELECT DISTINCT p.id, p.author, p.permlink, p.created,
                   p.sentiment, p.sentiment_score, p.community_id,
                   p.primary_language, p.is_nsfw, cm.community_name
            FROM posts p
            LEFT JOIN community_mappings cm ON cm.community_id = p.community_id
            {cat_join}
            {where}
            ORDER BY p.created {"ASC" if sort_order == "oldest" else "DESC"}, p.id {"ASC" if sort_order == "oldest" else "DESC"}
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
            "SELECT language, COUNT(DISTINCT post_id) AS count "
            "FROM post_language "
            "GROUP BY language ORDER BY count DESC"
        )
    )
    return [dict(r) for r in rows.mappings()]


@retry_transient
async def get_overview_stats(session: AsyncSession) -> dict:
    """Get overview statistics."""
    row = await session.execute(
        text(
            "SELECT "
            "(SELECT COUNT(*) FROM posts) AS total_posts, "
            "(SELECT COUNT(DISTINCT language) FROM post_language) AS languages"
        )
    )
    return dict(row.mappings().first())


# ── Blacklist sweep ──────────────────────────────────────────────────────────

@retry_transient
async def get_distinct_authors(session: AsyncSession) -> list[str]:
    """Return all distinct authors that have posts in the DB."""
    rows = await session.execute(
        text("SELECT DISTINCT author FROM posts")
    )
    return [r[0] for r in rows.fetchall()]


@retry_transient
async def delete_posts_by_author(session: AsyncSession, author: str) -> int:
    """Delete all posts (and associations) for a blacklisted author. Returns count deleted."""
    rows = await session.execute(
        text("SELECT id FROM posts WHERE author = :author"),
        {"author": author},
    )
    post_ids = [r[0] for r in rows.fetchall()]
    if not post_ids:
        return 0

    await session.execute(
        text("DELETE FROM post_category WHERE post_id = ANY(:ids)"),
        {"ids": post_ids},
    )
    await session.execute(
        text("DELETE FROM post_language WHERE post_id = ANY(:ids)"),
        {"ids": post_ids},
    )
    result = await session.execute(
        text("DELETE FROM posts WHERE author = :author"),
        {"author": author},
    )
    await session.commit()
    return result.rowcount


# ── Communities ──────────────────────────────────────────────────────────────


@retry_transient
async def get_available_communities(session: AsyncSession) -> list[dict]:
    """Get communities that have posts, with post counts and display names."""
    rows = await session.execute(
        text(
            "SELECT p.community_id AS id, "
            "       COALESCE(cm.community_name, p.community_id) AS name, "
            "       cm.category_slug AS category, "
            "       COUNT(*) AS post_count "
            "FROM posts p "
            "LEFT JOIN community_mappings cm ON cm.community_id = p.community_id "
            "WHERE p.community_id IS NOT NULL "
            "GROUP BY p.community_id, cm.community_name, cm.category_slug "
            "ORDER BY post_count DESC"
        )
    )
    return [dict(r) for r in rows.mappings()]


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

    Joins community_mappings with a post count from the posts table.
    Returns up to 10 results sorted by post_count descending.
    """
    if not categories:
        return []
    cat_placeholders = ", ".join(f":cat_{i}" for i in range(len(categories)))
    params: dict = {f"cat_{i}": cat for i, cat in enumerate(categories)}
    rows = await session.execute(
        text(
            f"""
            SELECT cm.community_id AS id,
                   cm.community_name AS name,
                   cm.category_slug AS category,
                   COALESCE(pc.cnt, 0) AS post_count
            FROM community_mappings cm
            LEFT JOIN (
                SELECT community_id, COUNT(*) AS cnt
                FROM posts
                WHERE community_id IS NOT NULL
                GROUP BY community_id
            ) pc ON pc.community_id = cm.community_id
            WHERE cm.category_slug IN ({cat_placeholders})
            ORDER BY post_count DESC
            LIMIT 10
            """
        ),
        params,
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
                   r.post_id, p.author AS post_author, p.permlink AS post_permlink
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

    # Batch-fetch categories for all referenced posts.
    post_ids = list({row["post_id"] for row in raw_rows})
    pid_cat_map: dict[int, list[str]] = {}
    if post_ids:
        cat_rows = await session.execute(
            text(
                "SELECT pc.post_id, c.name FROM categories c "
                "JOIN post_category pc ON c.id = pc.category_id "
                "WHERE pc.post_id = ANY(:ids)"
            ),
            {"ids": post_ids},
        )
        for cr in cat_rows.fetchall():
            pid_cat_map.setdefault(cr[0], []).append(cr[1])

    reports = []
    for row in raw_rows:
        reports.append({
            "id": row["id"],
            "reporter": row["reporter"],
            "reason": row["reason"],
            "created_at": row["created_at"],
            "post": {
                "author": row["post_author"],
                "permlink": row["post_permlink"],
                "categories": pid_cat_map.get(row["post_id"], []),
            },
        })

    return {"reports": reports, "total": total, "limit": limit, "offset": offset}


