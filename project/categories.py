"""Category hierarchy for CombFlow content classification.

Two-level tree: top-level parents group related leaf categories.
Classification operates on leaf categories; subscriptions can target
either level (subscribing to a parent covers all its children).

No external dependencies — safe to import from the host seed script.
"""

# {parent_slug: [child_slugs]}
CATEGORY_TREE: dict[str, list[str]] = {
    "technology": [
        "crypto",           # absorbs web3
        "programming",      # absorbs devops, data-science
        "ai",
        "cybersecurity",
        "gaming",           # absorbs esports, tabletop-games
    ],
    "creative": [
        "photography",
        "art",              # absorbs digital-art, design
        "music",
        "writing",          # absorbs comedy
        "video",            # absorbs podcasts
        "diy-crafts",       # merged diy + crafts
    ],
    "lifestyle": [
        "travel",
        "food",
        "fashion",
        "home-garden",
        "parenting",
        "pets",
    ],
    "science-education": [
        "nature",           # absorbs environment, outdoors
        "science",          # absorbs space
        "education",
        "health",           # renamed from medicine
        "psychology",       # absorbs relationships
    ],
    "society": [
        "politics",         # absorbs law
        "philosophy",
        "history",
        "religion",
        "social-issues",
    ],
    "finance-business": [
        "finance",
        "economics",        # restored from p016 merge
        "entrepreneurship", # restored from p016 merge
    ],
    "entertainment": [
        "movies-tv",
        "anime-manga",
        "books",
    ],
    "sports": [             # renamed from sports-outdoors (p025)
        "team-sports",      # football, basketball, soccer, cricket
        "combat-sports",    # boxing, MMA, wrestling, martial arts
        "motorsports",      # F1, rally, NASCAR, karting
        "outdoor-sports",   # hiking, climbing, cycling, running, trail
        "fitness",          # moved from lifestyle
    ],
    "community": [
        "hive",
        "introductions",    # #introduceyourself posts
        "contests",         # challenges, giveaways, contest posts
        "charity",          # restored from p016 merge
        "local-communities",# restored from p016 merge
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
