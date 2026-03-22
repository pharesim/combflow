import asyncio
import functools
import hashlib
import json as _json
import logging

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import cache as _cache
from .models import Category, Post

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
        await session.flush()
        for child_name in children:
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
    authors = [a for a, _ in pairs]
    permlinks = [p for _, p in pairs]
    rows = await session.execute(
        text(
            "SELECT author, permlink FROM posts "
            "WHERE (author, permlink) IN "
            "(SELECT unnest(CAST(:authors AS text[])), unnest(CAST(:permlinks AS text[])))"
        ),
        {"authors": authors, "permlinks": permlinks},
    )
    return {(r[0], r[1]) for r in rows.fetchall()}


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
                   cm.community_name
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

def _vec_to_pg(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


async def get_centroids(session: AsyncSession) -> dict[str, list[float]]:
    rows = await session.execute(
        text("SELECT category_name, CAST(centroid AS text) FROM category_centroids")
    )
    centroids: dict[str, list[float]] = {}
    for name, vec_str in rows.fetchall():
        centroids[name] = [float(x) for x in vec_str.strip("[]").split(",")]
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
                "vec": _vec_to_pg(vec),
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


def _browse_count_cache_key(categories, languages, sentiment, community=None, communities=None, authors=None):
    raw = _json.dumps({"c": sorted(categories or []),
                       "l": sorted(languages or []),
                       "s": sentiment,
                       "m": community,
                       "ms": sorted(communities) if communities else None,
                       "a": sorted(authors) if authors else None}, sort_keys=True)
    return f"browse_count:{hashlib.md5(raw.encode()).hexdigest()}"

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
) -> dict:
    """Browse all posts with optional filters.

    Supports two pagination modes:
    - **cursor** (preferred): pass the ``next_cursor`` value from the previous
      response.  Uses keyset pagination — O(1) regardless of page depth.
    - **offset** (legacy): OFFSET-based, degrades at high page numbers.

    Returns ``{"posts": [...], "next_cursor": "..." | null}``.
    """
    conditions = [
        "EXISTS (SELECT 1 FROM post_category WHERE post_id = p.id)",
    ]
    params: dict = {"lim": limit}

    # Cursor-based keyset pagination takes priority over offset.
    use_cursor = False
    if cursor:
        try:
            from datetime import datetime as _dt, timezone as _tz
            ts_str, id_str = cursor.rsplit("_", 1)
            params["cursor_created"] = _dt.fromtimestamp(float(ts_str), tz=_tz.utc)
            params["cursor_id"] = int(id_str)
            conditions.append("(p.created, p.id) < (:cursor_created, :cursor_id)")
            use_cursor = True
        except (ValueError, TypeError):
            pass  # malformed cursor — fall back to offset

    if not use_cursor:
        params["off"] = offset

    if languages:
        lang_placeholders = ", ".join(f":lang_{i}" for i in range(len(languages)))
        conditions.append(
            f"EXISTS (SELECT 1 FROM post_language pl_f "
            f"WHERE pl_f.post_id = p.id AND pl_f.language IN ({lang_placeholders}))"
        )
        for i, lang in enumerate(languages):
            params[f"lang_{i}"] = lang
    if sentiment:
        conditions.append("p.sentiment = :sent")
        params["sent"] = sentiment
    if communities:
        comm_placeholders = ", ".join(f":comm_{i}" for i in range(len(communities)))
        conditions.append(f"p.community_id IN ({comm_placeholders})")
        for i, cid in enumerate(communities):
            params[f"comm_{i}"] = cid
    elif community:
        conditions.append("p.community_id = :community")
        params["community"] = community

    if authors:
        author_placeholders = ", ".join(f":author_{i}" for i in range(len(authors)))
        conditions.append(f"p.author IN ({author_placeholders})")
        for i, a in enumerate(authors):
            params[f"author_{i}"] = a

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
    count_key = _browse_count_cache_key(categories, languages, sentiment, community, communities, authors)
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
                   cm.community_name
            FROM posts p
            LEFT JOIN community_mappings cm ON cm.community_id = p.community_id
            {cat_join}
            {where}
            ORDER BY p.created DESC, p.id DESC
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


