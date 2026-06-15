"""drop the redundant plain ``ix_posts_author`` index

``ix_posts_author`` (a plain btree on ``(author)``, added in migration 003) is
**fully redundant** with the ``uq_author_permlink`` UNIQUE btree
``(author, permlink)``: that constraint's leading column already serves every
bare ``author = X`` lookup. The two *other* author indexes are partial and serve
their own filtered queries — ``ix_posts_author_recent``
(``(author, created DESC) WHERE category_ids <> '{}'``, migration 008) and
``ix_posts_browse_author`` (``(author, created DESC, id DESC) WHERE is_nsfw =
false``, migration 005) — so the only full-coverage author index besides
``ix_posts_author`` is ``uq_author_permlink``.

Proposal 112 deleted ``crud.get_author_recent_posts``, the last reader that
might have justified a dedicated plain ``(author)`` index; nothing remaining
needs it. ``posts`` is a high-write table (the worker streams inserts
continuously), so a redundant index is pure write amplification + disk for no
read benefit.

No plan regression — every ``author = X`` access path falls through to
``uq_author_permlink`` with the same plan *shape* (verified by EXPLAIN on local
prod-scale data, worst-case author ``tdvtv`` ~27k posts, index dropped in a
rolled-back txn):

==================================================  ====================  ======================
query                                               with ix_posts_author  without → uq_author_permlink
==================================================  ====================  ======================
bare ``author = X``                                 Index Only Scan 12238  Index Only Scan 13266
``author = X AND language_codes <> '{}'``           Bitmap 66094           Bitmap 67122 (+1.6%)
``author = X AND community_id IS NOT NULL``          Bitmap 66037           Bitmap 67065
``DELETE FROM posts WHERE author = X``               Bitmap 66039           Bitmap 67067
==================================================  ====================  ======================

The bare lookup stays Index-Only; the filtered/delete patterns stay Bitmap Index
Scan — none falls back to a Seq Scan. ``uq_author_permlink`` is wider (it carries
``permlink``), so its bitmap-index-scan node costs marginally more, but those
queries are heap-bound (~66k total), so the delta is ~1.6%. See proposal 113.

One non-equality reader DOES regress, accepted as negligible: the daily
``blacklist.sweep_thread`` enumerates distinct authors via
``crud.get_distinct_authors`` (``SELECT DISTINCT author FROM posts ORDER BY
author LIMIT/OFFSET``). The narrow ``(author)`` index let that run as a streaming
Index-Only Scan; with only the wider ``uq_author_permlink`` left the planner
prefers a Parallel Seq Scan + HashAggregate + Sort (local prod-scale 8.85M posts /
184k distinct authors: ~279 ms → ~3.8 s per 10k-row batch, ~19 batches ≈ +70 s).
This is a once-per-day background worker job whose per-author check loop is already
rate-limited to ~25 h (``_SWEEP_RATE_LIMIT`` 0.5 s × 184k authors), so the extra
~70 s is <0.1 % of the sweep and has zero user-facing impact — far outweighed by
removing per-insert maintenance of a redundant index on a continuously-written
table.

Plain ``DROP INDEX`` (not CONCURRENTLY): unlike ``CREATE INDEX``, a drop does no
table scan — it only removes the catalog entry and unlinks the index files under
a brief ACCESS EXCLUSIVE lock, so it is fast and non-blocking in practice. (The
079/107 "CONCURRENTLY can't run inside Alembic's per-migration transaction" note
is a *create* concern; it does not apply here.) ``downgrade`` recreates the plain
``(author)`` btree.

Revision ID: 009
Revises: 008
Create Date: 2026-06-15
"""

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_posts_author", table_name="posts")


def downgrade() -> None:
    op.create_index("ix_posts_author", "posts", ["author"])
