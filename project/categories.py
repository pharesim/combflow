"""Category hierarchy for CombFlow content classification.

Two-level tree: top-level parents group related leaf categories.
Classification operates on leaf categories; subscriptions can target
either level (subscribing to a parent covers all its children).

No external dependencies — safe to import from the host seed script.
"""

# {parent_slug: [child_slugs]}
CATEGORY_TREE: dict[str, list[str]] = {
    "technology": [
        "crypto",           # blockchain, web3, DeFi
        "programming",      # absorbs devops, data-science
        "ai",
        "cybersecurity",
    ],
    "creative": [
        "photography",
        "art",              # absorbs digital-art, design
        "music",
        "writing",          # absorbs comedy
        "video",            # absorbs podcasts
        "diy-crafts",
    ],
    "lifestyle": [
        "travel",
        "food",
        "fashion",
        "homesteading",     # off-grid, farming, self-sufficiency
        "gardening",        # replaces home-garden, better Hive fit
        "pets",
    ],
    "science-education": [
        "nature",           # absorbs environment, outdoors
        "science",          # absorbs space
        "education",
        "health-fitness",   # merged health + fitness
    ],
    "society": [
        "politics",         # absorbs law
        "philosophy",       # absorbs psychology
        "history",
        "social-issues",    # absorbs religion
    ],
    "finance-business": [
        "finance",          # absorbs economics
        "entrepreneurship",
        "precious-metals",  # #silvergoldstackers, bullion, stacking
    ],
    "entertainment": [
        "gaming",           # absorbs esports, tabletop-games
        "movies-tv",        # absorbs anime-manga
        "books",
    ],
    "sports": [
        "sports",           # merged team-sports + combat-sports + motorsports
        "outdoor-sports",   # hiking, climbing, cycling, running, trail
    ],
    "community": [
        "hive",
        "contests",         # challenges, giveaways, contest posts
        "spirituality",     # meditation, mindfulness, astrology
    ],
}

# Flat list of all leaf categories (used for classification).
LEAF_CATEGORIES: list[str] = [
    cat for children in CATEGORY_TREE.values() for cat in children
]

# Parent names only.
PARENT_CATEGORIES: list[str] = list(CATEGORY_TREE.keys())

# Every slug — parents + leaves.
ALL_CATEGORIES: list[str] = PARENT_CATEGORIES + LEAF_CATEGORIES
