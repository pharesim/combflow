"""Tests for comment tree endpoint (proposal 002) and cache invalidation (proposal 003)."""
from unittest.mock import patch

from project.api.routes.posts import _build_comment_tree, _rate_log
from tests.conftest import jwt_headers

# ── Tree building unit tests ─────────────────────────────────────────────────

FLAT_COMMENTS = [
    {
        "author": "alice",
        "permlink": "re-bob-post-1",
        "body": "Great post!",
        "created": "2026-03-20T12:00:00",
        "parent_author": "bob",
        "parent_permlink": "my-post",
        "reputation": 52.3,
    },
    {
        "author": "bob",
        "permlink": "re-alice-re-bob-post-1",
        "body": "Thanks!",
        "created": "2026-03-20T12:05:00",
        "parent_author": "alice",
        "parent_permlink": "re-bob-post-1",
        "reputation": 67.1,
    },
    {
        "author": "carol",
        "permlink": "re-bob-post-2",
        "body": "Interesting.",
        "created": "2026-03-20T12:10:00",
        "parent_author": "bob",
        "parent_permlink": "my-post",
        "reputation": 45.0,
    },
]


def test_build_tree_basic():
    tree, hidden = _build_comment_tree(FLAT_COMMENTS, "bob", "my-post", max_depth=6)
    assert hidden == 0
    assert len(tree) == 2  # alice and carol are top-level
    alice_node = tree[0]
    assert alice_node["author"] == "alice"
    assert len(alice_node["children"]) == 1
    assert alice_node["children"][0]["author"] == "bob"
    assert tree[1]["author"] == "carol"
    assert tree[1]["children"] == []


def test_build_tree_filters_negative_rep():
    comments = FLAT_COMMENTS + [
        {
            "author": "spammer",
            "permlink": "re-spam",
            "body": "Buy my stuff!",
            "created": "2026-03-20T13:00:00",
            "parent_author": "bob",
            "parent_permlink": "my-post",
            "reputation": -2.0,
        },
    ]
    tree, hidden = _build_comment_tree(comments, "bob", "my-post", max_depth=6)
    assert hidden == 1
    authors = [n["author"] for n in tree]
    assert "spammer" not in authors


def test_build_tree_filters_zero_rep():
    comments = [
        {
            "author": "newuser",
            "permlink": "re-newuser",
            "body": "Hello",
            "created": "2026-03-20T13:00:00",
            "parent_author": "bob",
            "parent_permlink": "my-post",
            "reputation": 0.0,
        },
    ]
    tree, hidden = _build_comment_tree(comments, "bob", "my-post", max_depth=6)
    assert hidden == 1
    assert tree == []


def test_build_tree_depth_limit():
    # Chain: a -> b -> c -> d (depths 1, 2, 3, 4)
    comments = [
        {"author": "a", "permlink": "c1", "body": "1", "created": "2026-03-20T12:00:00",
         "parent_author": "bob", "parent_permlink": "root", "reputation": 50.0},
        {"author": "b", "permlink": "c2", "body": "2", "created": "2026-03-20T12:01:00",
         "parent_author": "a", "parent_permlink": "c1", "reputation": 50.0},
        {"author": "c", "permlink": "c3", "body": "3", "created": "2026-03-20T12:02:00",
         "parent_author": "b", "parent_permlink": "c2", "reputation": 50.0},
        {"author": "d", "permlink": "c4", "body": "4", "created": "2026-03-20T12:03:00",
         "parent_author": "c", "parent_permlink": "c3", "reputation": 50.0},
    ]
    tree, hidden = _build_comment_tree(comments, "bob", "root", max_depth=2)
    assert len(tree) == 1
    assert tree[0]["author"] == "a"
    assert len(tree[0]["children"]) == 1
    assert tree[0]["children"][0]["author"] == "b"
    # c is at depth 3, which exceeds max_depth=2
    assert tree[0]["children"][0]["children"] == []
    assert hidden == 2  # c and d are hidden by depth


def test_build_tree_empty():
    tree, hidden = _build_comment_tree([], "bob", "my-post", max_depth=6)
    assert tree == []
    assert hidden == 0


def test_build_tree_no_depth_field_in_output():
    tree, _ = _build_comment_tree(FLAT_COMMENTS, "bob", "my-post", max_depth=6)
    assert "_depth" not in tree[0]
    assert "_depth" not in tree[0]["children"][0]


# ── GET /api/posts/{author}/{permlink}/comments ─────────────────────────────

@patch("project.api.routes.posts.hafsql_get_comments")
async def test_get_comments_endpoint(mock_get, client):
    mock_get.return_value = FLAT_COMMENTS
    resp = await client.get("/posts/bob/my-post/comments")
    assert resp.status_code == 200
    data = resp.json()
    assert "comments" in data
    assert "hidden_count" in data
    assert len(data["comments"]) == 2
    mock_get.assert_called_once_with("bob", "my-post")


@patch("project.api.routes.posts.hafsql_get_comments")
async def test_get_comments_custom_depth(mock_get, client):
    # Chain: a -> b -> c (depths 1, 2, 3)
    mock_get.return_value = [
        {"author": "a", "permlink": "c1", "body": "1", "created": "2026-03-20T12:00:00",
         "parent_author": "bob", "parent_permlink": "root", "reputation": 50.0},
        {"author": "b", "permlink": "c2", "body": "2", "created": "2026-03-20T12:01:00",
         "parent_author": "a", "parent_permlink": "c1", "reputation": 50.0},
        {"author": "c", "permlink": "c3", "body": "3", "created": "2026-03-20T12:02:00",
         "parent_author": "b", "parent_permlink": "c2", "reputation": 50.0},
    ]
    resp = await client.get("/posts/bob/root/comments?depth=1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["comments"]) == 1
    assert data["comments"][0]["children"] == []
    assert data["hidden_count"] == 2


@patch("project.api.routes.posts.hafsql_get_comments")
async def test_get_comments_caching(mock_get, client):
    mock_get.return_value = []
    await client.get("/posts/bob/my-post/comments")
    await client.get("/posts/bob/my-post/comments")
    # Second call should hit cache, not HAFSQL.
    mock_get.assert_called_once()


@patch("project.api.routes.posts.hafsql_get_comments")
async def test_get_comments_hafsql_down(mock_get, client):
    mock_get.return_value = []
    resp = await client.get("/posts/bob/my-post/comments")
    assert resp.status_code == 200
    assert resp.json() == {"comments": [], "hidden_count": 0}


@patch("project.api.routes.posts.hafsql_get_comments")
async def test_get_comments_depth_validation(mock_get, client):
    resp = await client.get("/posts/bob/my-post/comments?depth=0")
    assert resp.status_code == 422

    resp = await client.get("/posts/bob/my-post/comments?depth=11")
    assert resp.status_code == 422


# ── DELETE /api/posts/{author}/{permlink}/comments/cache ─────────────────────

@patch("project.api.routes.posts.hafsql_get_comments")
async def test_invalidate_comment_cache(mock_get, client):
    mock_get.return_value = FLAT_COMMENTS
    # Warm cache.
    await client.get("/posts/bob/my-post/comments")
    assert mock_get.call_count == 1

    # Invalidate.
    headers = jwt_headers("alice")
    resp = await client.delete("/posts/bob/my-post/comments/cache", headers=headers)
    assert resp.status_code == 204

    # Next GET should hit HAFSQL again.
    await client.get("/posts/bob/my-post/comments")
    assert mock_get.call_count == 2


async def test_invalidate_comment_cache_requires_auth(client):
    resp = await client.delete("/posts/bob/my-post/comments/cache")
    assert resp.status_code in (401, 403)


async def test_invalidate_comment_cache_rate_limit(client):
    _rate_log.clear()
    headers = jwt_headers("ratelimituser")
    for _ in range(5):
        resp = await client.delete("/posts/bob/test/comments/cache", headers=headers)
        assert resp.status_code == 204

    resp = await client.delete("/posts/bob/test/comments/cache", headers=headers)
    assert resp.status_code == 429
    _rate_log.clear()


# ── Comment rate limit cleanup ──────────────────────────────────────────────

def test_comment_rate_log_purges_stale():
    """When _rate_log exceeds _RATE_LOG_MAX, stale entries should be purged."""
    import collections
    import time
    from project.api.routes.posts import _rate_log, _check_user_rate, _RATE_LOG_MAX
    _rate_log.clear()

    now = time.time()
    for i in range(_RATE_LOG_MAX + 1):
        _rate_log[f"stale:{i}"] = collections.deque([now - 120])

    # Trigger cleanup.
    _check_user_rate("cleanup-test-user", 100)
    assert len(_rate_log) < _RATE_LOG_MAX
    _rate_log.clear()


# ── Comment tree edge cases ─────────────────────────────────────────────────

def test_build_tree_multi_level_nesting():
    """Verify alice -> bob -> carol chain is fully preserved."""
    comments = [
        {"author": "alice", "permlink": "c1", "body": "1", "created": "t1",
         "parent_author": "root", "parent_permlink": "post", "reputation": 50.0},
        {"author": "bob", "permlink": "c2", "body": "2", "created": "t2",
         "parent_author": "alice", "parent_permlink": "c1", "reputation": 50.0},
        {"author": "carol", "permlink": "c3", "body": "3", "created": "t3",
         "parent_author": "bob", "parent_permlink": "c2", "reputation": 50.0},
    ]
    tree, hidden = _build_comment_tree(comments, "root", "post", max_depth=6)
    assert hidden == 0
    assert len(tree) == 1
    assert tree[0]["author"] == "alice"
    assert tree[0]["children"][0]["author"] == "bob"
    assert tree[0]["children"][0]["children"][0]["author"] == "carol"


def test_build_tree_orphaned_comments():
    """Comments whose parent is not in the tree should appear at root."""
    comments = [
        {"author": "alice", "permlink": "c1", "body": "1", "created": "t1",
         "parent_author": "missing", "parent_permlink": "gone", "reputation": 50.0},
    ]
    tree, hidden = _build_comment_tree(comments, "root", "post", max_depth=6)
    # Orphaned comment's parent isn't the root post either, so it ends up at root.
    assert len(tree) == 1
    assert tree[0]["author"] == "alice"


def test_build_tree_exactly_zero_rep_filtered():
    """Reputation == 0.0 should be filtered (rep <= 0 check)."""
    comments = [
        {"author": "zerorepper", "permlink": "c1", "body": "Hi", "created": "t1",
         "parent_author": "root", "parent_permlink": "post", "reputation": 0.0},
    ]
    tree, hidden = _build_comment_tree(comments, "root", "post", max_depth=6)
    assert tree == []
    assert hidden == 1


def test_build_tree_barely_positive_rep_kept():
    """Reputation 0.01 should pass the filter."""
    comments = [
        {"author": "newish", "permlink": "c1", "body": "Hi", "created": "t1",
         "parent_author": "root", "parent_permlink": "post", "reputation": 0.01},
    ]
    tree, hidden = _build_comment_tree(comments, "root", "post", max_depth=6)
    assert len(tree) == 1
    assert hidden == 0
