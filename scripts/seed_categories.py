#!/usr/bin/env python3
"""
seed_categories.py — Bootstrap the CombFlow semantic classifier.

Fetches recent posts from HAFSQL (public Hive SQL mirror), classifies each
with a local Ollama LLM, then computes per-category embedding centroids.

Pipeline (parallelised):
  1. FETCHER thread: query HAFSQL for recent posts (reputation >= 20)
  2. CLASSIFIER thread: classify each post with Ollama as they arrive
  3. Main thread: compute centroids (mean of normalised embeddings)
  4. Upload centroids to the running CombFlow API (or save locally)

Interrupt safely with Ctrl-C — progress checkpointed to seeds/checkpoint.json.
Resume with --resume.

Prerequisites:
  pip install torch --index-url https://download.pytorch.org/whl/cu121
  pip install -r scripts/requirements.txt
  ollama pull llama3.1:8b

Usage:
  python scripts/seed_categories.py --posts 3000
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue

import numpy as np
import requests

try:
    import ollama as _ollama
except ImportError:
    print("ERROR: ollama package not found.\n  pip install ollama", file=sys.stderr)
    sys.exit(1)

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    print("ERROR: sentence-transformers not found.\n  pip install sentence-transformers", file=sys.stderr)
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent
SEEDS_DIR = REPO_ROOT / "seeds"
CENTROIDS_FILE = SEEDS_DIR / "centroids.json"
CHECKPOINT_FILE = SEEDS_DIR / "checkpoint.json"

# Import from the project package (no heavy dependencies).
sys.path.insert(0, str(REPO_ROOT))
from project.categories import LEAF_CATEGORIES  # noqa: E402
from project.hafsql import _raw_rep_to_score  # noqa: E402
from project.text import clean_post_body  # noqa: E402

# ── Hardware presets ───────────────────────────────────────────────────────────
HARDWARE_MODELS: dict[str, tuple[str, str]] = {
    "8gb":   ("llama3.2:3b",       "~2 GB  — 8 GB VRAM"),
    "16gb":  ("llama3.1:8b",       "~5 GB  — 16 GB VRAM  ← default"),
    "24gb":  ("mistral-nemo:12b",  "~7 GB  — 24 GB VRAM"),
    "40gb":  ("llama3.1:70b",      "~38 GB — 40 GB VRAM"),
    "80gb":  ("llama3.3:70b",      "~43 GB — 80 GB VRAM"),
}
DEFAULT_HARDWARE = "16gb"

# ── Defaults ──────────────────────────────────────────────────────────────────
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
SIMILARITY_THRESHOLD = 0.45
MIN_AUTHOR_REPUTATION = 20.0

# HAFSQL — direct SQL access to Hive chain data.
HAFSQL_DSN = (
    "host=hafsql-sql.mahdiyari.info port=5432 "
    "dbname=haf_block_log user=hafsql_public password=hafsql_public "
    "connect_timeout=10"
)

CLASSIFY_PROMPT = """\
Classify this post by its ACTUAL TOPIC. Ignore the platform it was posted on.

CRITICAL: These posts come from Hive (a blockchain platform). Do NOT classify as "crypto" or "hive" \
unless the post is genuinely ABOUT cryptocurrency/blockchain technology or Hive platform governance. \
A cooking post published on Hive is "food", not "crypto". A travel post by someone who earns HBD is "travel", not "crypto".

Available categories: {categories}

{category_hints}
Rules:
- Return ONLY a valid JSON array, e.g. ["food", "travel"]
- Return [] if none of the categories clearly apply
- Classify by what the post TEACHES, DISCUSSES, or SHOWS — not by where it was posted
- Multiple categories are fine for posts that clearly cover several topics
- When a post could fit multiple similar categories, prefer the most specific one

Post title: {title}
Post body (excerpt):
{body}

Categories JSON:"""

# ── Category disambiguation hints (p026) ────────────────────────────────────
# One-line scope + exclusions for categories prone to semantic overlap.

CATEGORY_HINTS: dict[str, str] = {
    # ── technology ──
    "crypto":        "Cryptocurrency, blockchain technology, DeFi, tokens, mining, NFTs, "
                     "NFT trading, token swaps, crypto trading, blockchain gaming assets. "
                     "The post must be primarily ABOUT crypto technology or crypto markets. "
                     "Any trading of digital tokens, NFTs, or blockchain-based assets is crypto, NOT finance. "
                     "NOT stock market investing or personal finance (-> finance). "
                     "NOT posts that merely mention Hive/HBD rewards or crypto earnings in passing. "
                     "NOT automated bot/token tracker posts unless analyzing crypto markets.",
    "programming":   "Software development, writing code, DevOps, APIs, databases. "
                     "NOT using software tools, math, chess, or general tech.",
    "ai":            "Artificial intelligence, machine learning, LLMs, neural networks. "
                     "NOT general programming or sci-fi about AI.",
    "cybersecurity": "Hacking, infosec, privacy, encryption, penetration testing, data breaches. "
                     "NOT general programming or using a VPN.",
    # ── creative ──
    "photography":   "Photography as craft: composition, gear, photo walks, editing techniques, "
                     "photo challenges, landscape/portrait/street photography showcases. "
                     "NOT travel posts that happen to include photos (-> travel). "
                     "NOT nature posts with wildlife photos (-> nature unless photography is the focus). "
                     "NOT digital art or illustration (-> art).",
    "art":           "Visual art: drawing, painting, illustration, digital art, sculpture, "
                     "webcomics, comics, pixel art, NFT art, sketch journals. "
                     "The post showcases or discusses original visual artwork. "
                     "NOT photography (-> photography). NOT crafts/woodworking (-> diy-crafts). "
                     "NOT video production or film (-> video, movies-tv).",
    "music":         "Music creation, production, covers, instruments, music reviews, "
                     "music theory, concerts, playlists, open mic performances. "
                     "NOT a post that merely mentions a song in passing.",
    "writing":       "Creative writing, fiction, poetry, short stories, writing craft, "
                     "comedy, satire, humor pieces. "
                     "The post IS the creative work or discusses the writing process. "
                     "NOT long-form opinion on politics (-> politics). "
                     "NOT a personal blog about travel (-> travel).",
    "video":         "Video creation, vlogging, YouTube, 3Speak, video editing, streaming, "
                     "video production, content creation tips for video. "
                     "The post must be ABOUT making or sharing original video content. "
                     "NOT movie reviews or TV show discussion (-> movies-tv).",
    "diy-crafts":    "Hands-on making: woodworking, sewing, knitting, 3D printing, "
                     "home repair projects. NOT cooking (-> food). "
                     "NOT gardening (-> gardening). NOT digital art (-> art).",
    # ── lifestyle ──
    "travel":        "Travel experiences, destination guides, trip reports, backpacking, "
                     "tourism, cultural exploration. "
                     "NOT photography technique posts shot while traveling (-> photography).",
    "food":          "Cooking, recipes, restaurant reviews, food photography, cuisine, "
                     "food culture, meal prep, food diaries. "
                     "NOT growing food in a garden (-> gardening). "
                     "NOT food preservation as part of self-sufficient living (-> homesteading).",
    "fashion":       "Fashion, style, clothing, outfits, streetwear, accessories, beauty. "
                     "NOT diy-crafts unless the post is about sewing/making clothes as craft.",
    "homesteading":  "Off-grid living, self-sufficiency, small-scale farming, raising livestock, "
                     "food preservation (canning, fermenting), rural life, permaculture. "
                     "NOT backyard gardening as a hobby (-> gardening). "
                     "NOT cooking with homegrown ingredients (-> food). "
                     "NOT woodworking or home repair (-> diy-crafts).",
    "gardening":     "Home gardening, houseplants, landscaping, flower beds, growing vegetables, "
                     "composting, urban farming, balcony gardens, harvest posts. "
                     "NOT large-scale farming or livestock (-> homesteading). "
                     "NOT wild nature or hiking through forests (-> nature). "
                     "NOT cooking with homegrown ingredients (-> food).",
    "pets":          "Pet care, dogs, cats, aquariums, pet stories, animal companionship. "
                     "NOT wildlife or nature photography (-> nature).",
    # ── science-education ──
    "nature":        "Wildlife, environment, ecology, conservation, climate, "
                     "nature photography focused on the natural world. "
                     "NOT gardening or farming (-> gardening, homesteading). "
                     "NOT outdoor recreation as sport (-> outdoor-sports).",
    "science":       "Scientific research, experiments, discoveries, STEM topics, space, astronomy. "
                     "NOT health/medicine advice (-> health-fitness). NOT nature photography (-> nature).",
    "education":     "Teaching, learning, curricula, academic institutions, study methods, "
                     "tutorials, educational content. "
                     "NOT crypto/Hive how-to guides (-> crypto or hive). "
                     "NOT cooking tutorials (-> food).",
    "health-fitness":"Physical health, medicine, nutrition, wellness, chronic illness, "
                     "gym workouts, yoga, bodybuilding, exercise routines, fitness goals. "
                     "Both medical/wellness topics AND exercise/training belong here. "
                     "NOT hiking, cycling, running races (-> outdoor-sports). "
                     "NOT meditation or spiritual healing (-> spirituality). "
                     "NOT scientific research papers (-> science).",
    # ── society ──
    "politics":      "Government, policy, elections, legislation, law, political commentary. "
                     "NOT abstract ethics or thought experiments (-> philosophy).",
    "philosophy":    "Philosophy, ethics, epistemology, thought experiments, "
                     "psychology, mental health, therapy, cognitive science, self-reflection. "
                     "Formal or informal inquiry into mind, meaning, and human experience. "
                     "NOT casual self-help advice (-> education). "
                     "NOT spiritual practices or energy healing (-> spirituality).",
    "history":       "Historical events, eras, historical figures, archaeology, military history, "
                     "cultural history, historical analysis. "
                     "The post must be ABOUT the past, not just mention historical context. "
                     "NOT current politics informed by history (-> politics).",
    "social-issues": "Social justice, inequality, human rights, cultural commentary, "
                     "religious discussion, faith, theology, interfaith dialogue. "
                     "Societal topics including organized religion and belief systems. "
                     "NOT spiritual practices or personal mindfulness (-> spirituality). "
                     "NOT abstract philosophy or ethics (-> philosophy).",
    # ── finance-business ──
    "finance":       "Personal finance, investing, stock markets, banking, real estate, "
                     "macroeconomics, monetary policy, inflation, GDP, trade. "
                     "All traditional finance and economic analysis. "
                     "NOT cryptocurrency or DeFi (-> crypto). "
                     "NOT startups or business building (-> entrepreneurship). "
                     "NOT precious metal stacking or coin collecting (-> precious-metals).",
    "entrepreneurship": "Startups, business building, marketing, small business, founders. "
                     "NOT personal investing or macroeconomics (-> finance).",
    "precious-metals":"Gold, silver, bullion, coin collecting, precious metal stacking, "
                     "numismatics, minting, unboxing bullion, #silvergoldstackers. "
                     "Physical metals as collectibles or stores of value. "
                     "NOT gold/silver ETFs or commodity futures trading (-> finance). "
                     "NOT crypto tokens pegged to gold (-> crypto). "
                     "NOT jewelry making (-> diy-crafts).",
    # ── entertainment ──
    "gaming":        "Video games, tabletop games, esports, Splinterlands, game reviews, gameplay. "
                     "NOT watching movies or TV (-> movies-tv).",
    "movies-tv":     "Movie reviews, TV shows, film analysis, series recommendations, "
                     "cinema, documentaries, anime, manga, animation discussion. "
                     "Watching and discussing screen media as a viewer. "
                     "NOT video creation or vlogging (-> video). "
                     "NOT reading books (-> books).",
    "books":         "Book reviews, reading lists, literary discussion, book clubs, "
                     "author spotlights, reading challenges. "
                     "NOT the craft of writing fiction or poetry (-> writing). "
                     "NOT watching adaptations (-> movies-tv).",
    # ── sports ──
    "sports":        "Competitive sports: football, soccer, basketball, cricket, tennis, "
                     "boxing, MMA, martial arts, Formula 1, NASCAR, motorsports, wrestling. "
                     "Organized competitive events, leagues, matches, fight cards. "
                     "NOT recreational outdoor activities (-> outdoor-sports). "
                     "NOT gym workouts or personal fitness (-> health-fitness).",
    "outdoor-sports":"Hiking, climbing, cycling, running, trail running, surfing, skiing, kayaking. "
                     "Recreational activities done outdoors in nature or on roads/trails. "
                     "NOT competitive league sports (-> sports). "
                     "NOT gym workouts or yoga (-> health-fitness).",
    # ── community ──
    "hive":          "Hive blockchain platform meta: witnesses, governance, DHF proposals, "
                     "platform updates, HBD savings. NOT posts merely published on Hive. "
                     "Ignore greetings like 'hello hivers/hivians', Hive footers, community tags, "
                     "and reward mentions — these appear on ALL Hive posts regardless of topic.",
    "contests":      "Challenges, giveaways, competitions, contest posts, raffles. "
                     "NOT competitive sports (-> sports) or general gaming (-> gaming).",
    "spirituality":  "Meditation, mindfulness, astrology, tarot, energy healing, chakras, "
                     "spiritual awakening, yoga philosophy, reiki, crystal healing. "
                     "Personal spiritual practice and exploration. "
                     "NOT organized religion or theology (-> social-issues). "
                     "NOT abstract philosophy of mind (-> philosophy). "
                     "NOT physical yoga as exercise (-> health-fitness).",
}

# ── Negative examples for common misclassifications (p026) ───────────────────

NEGATIVE_EXAMPLES: dict[str, list[str]] = {
    "hive": [
        "A daily diary post about meals (-> food, NOT hive)",
        "A photography post shared on Hive (-> photography, NOT hive)",
        "A post that starts with 'Hello Hivers!' then talks about travel (-> travel, NOT hive)",
        "A post mentioning Hive rewards in the footer but about cooking (-> food, NOT hive)",
        "A Splinterlands or Rising Star gameplay post (-> gaming, NOT hive)",
        "A post in a Hive community about the author's hobby (-> classify by hobby topic, NOT hive)",
    ],
    "writing": [
        "A long-form opinion about politics (-> politics, NOT writing)",
        "A personal blog about travel experiences (-> travel, NOT writing)",
        "A philosophical reflection essay (-> philosophy, NOT writing)",
        "A personal diary entry about daily life (-> classify by actual topic, NOT writing)",
    ],
    "crypto": [
        "A post about stock market investing (-> finance, NOT crypto)",
        "A post about Hive witness voting (-> hive, NOT crypto)",
        "A post about cooking that mentions earning HBD (-> food, NOT crypto)",
        "A non-English blog post published on the Hive blockchain (-> classify by actual topic, NOT crypto)",
        "A daily automated token stats/tracker post (-> hive, NOT crypto unless about crypto markets)",
        "A Hive witness update about node infrastructure (-> hive, NOT crypto)",
    ],
    "finance": [
        "A post about NFT card trading or blockchain game assets (-> crypto, NOT finance)",
        "A post about swapping tokens on a DEX (-> crypto, NOT finance)",
        "A crypto market analysis or price prediction (-> crypto, NOT finance)",
        "A post about HP, powering up, staking, or delegating Hive (-> crypto or hive, NOT finance)",
        "A post about HBD savings interest or converting HBD (-> crypto or hive, NOT finance)",
        "A post about Hive-Engine token prices or market cap (-> crypto, NOT finance)",
        "A post about stacking silver bars (-> precious-metals, NOT finance)",
        "A post about gold coin unboxing (-> precious-metals, NOT finance)",
    ],
    "precious-metals": [
        "A post about gold ETFs or commodity futures (-> finance, NOT precious-metals)",
        "A post about gold-backed crypto tokens (-> crypto, NOT precious-metals)",
        "A jewelry-making tutorial using silver wire (-> diy-crafts, NOT precious-metals)",
    ],
    "programming": [
        "An automated daily token tracker/stats post (-> crypto or hive, NOT programming)",
        "A bot-generated curator report or token update (-> hive, NOT programming)",
        "A post about using a software tool without discussing code (-> classify by topic, NOT programming)",
    ],
    "video": [
        "A movie review or film discussion (-> movies-tv, NOT video)",
        "A webcomic that casually references a movie (-> art, NOT video)",
        "A post about watching a TV series (-> movies-tv, NOT video)",
        "A photography post that includes a short clip (-> photography, NOT video)",
    ],
    "gardening": [
        "A post about canning vegetables from the farm (-> homesteading, NOT gardening)",
        "A post about cooking with homegrown herbs (-> food, NOT gardening)",
        "A nature walk through a forest (-> nature, NOT gardening)",
        "A post about raising chickens alongside a garden (-> homesteading, NOT gardening)",
    ],
    "homesteading": [
        "A post about growing tomatoes on a balcony (-> gardening, NOT homesteading)",
        "A woodworking project for a shed (-> diy-crafts, NOT homesteading)",
        "A recipe using fresh farm eggs (-> food, NOT homesteading)",
    ],
    "spirituality": [
        "A post about the history of Buddhism (-> history or social-issues, NOT spirituality)",
        "A gym yoga class workout (-> health-fitness, NOT spirituality)",
        "A philosophical essay on consciousness (-> philosophy, NOT spirituality)",
        "A post about church community events (-> social-issues, NOT spirituality)",
    ],
    "health-fitness": [
        "A hiking trip report (-> outdoor-sports, NOT health-fitness)",
        "A meditation practice guide (-> spirituality, NOT health-fitness)",
        "A competitive marathon race recap (-> outdoor-sports, NOT health-fitness)",
    ],
    "sports": [
        "A personal gym workout log (-> health-fitness, NOT sports)",
        "A weekend hiking trip (-> outdoor-sports, NOT sports)",
        "A post about watching a sports documentary (-> movies-tv, NOT sports)",
    ],
    "philosophy": [
        "Self-help advice about productivity (-> education, NOT philosophy)",
        "A tarot reading or crystal healing post (-> spirituality, NOT philosophy)",
        "A post about relationship drama (-> social-issues, NOT philosophy)",
    ],
}


def _build_hint_block() -> str:
    """Format CATEGORY_HINTS + NEGATIVE_EXAMPLES into a prompt block."""
    lines = ["Category definitions (use these to disambiguate):"]
    for cat, hint in CATEGORY_HINTS.items():
        lines.append(f"- {cat}: {hint}")
    lines.append("")
    lines.append("Common mistakes to avoid:")
    for cat, examples in NEGATIVE_EXAMPLES.items():
        for ex in examples:
            lines.append(f"- {ex}")
    return "\n".join(lines)

logging.basicConfig(format="%(levelname)s  %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── Tag hints for stratified sampling ────────────────────────────────────────
# Maps under-represented categories to Hive tags likely to surface relevant posts.

TAG_HINTS: dict[str, list[str]] = {
    # technology
    "cybersecurity": ["cybersecurity", "infosec", "hacking", "security", "privacy", "encryption"],
    "ai": ["ai", "artificial-intelligence", "machine-learning", "deeplearning", "chatgpt", "llm"],
    # creative
    "diy-crafts": ["diy", "crafts", "handmade", "woodworking", "knitting", "crochet", "maker"],
    "fashion": ["fashion", "style", "clothing", "outfit", "streetwear", "mensfashion"],
    # lifestyle
    "homesteading": ["homesteading", "offgrid", "selfsufficiency", "permaculture",
                     "foodpreservation", "canning", "livestock", "smallfarm"],
    "gardening": ["garden", "gardening", "hivegarden", "plants", "growing", "harvest",
                  "houseplants", "composting", "urbangarden"],
    # science-education
    "health-fitness": ["health", "medicine", "wellness", "fitness", "gym", "workout",
                       "yoga", "bodybuilding", "weightloss", "exercise"],
    "science": ["science", "space", "astronomy", "physics", "biology", "chemistry", "nasa"],
    # society
    "social-issues": ["socialissues", "humanrights", "equality", "religion", "faith",
                      "christianity", "islam", "church", "theology"],
    "politics": ["politics", "government", "law", "legal", "policy", "legislation", "democracy"],
    # finance-business
    "finance": ["finance", "investing", "realestate", "stocks", "trading",
                "personalfinance", "banking", "economics", "economy", "inflation"],
    "entrepreneurship": ["entrepreneur", "startup", "business", "marketing", "smallbusiness",
                         "founder", "saas", "hustle"],
    "precious-metals": ["silvergoldstackers", "silver", "gold", "bullion", "stacking",
                        "preciousmetals", "numismatics", "coins", "minting"],
    # entertainment
    "books": ["books", "reading", "bookreview", "literature", "fiction", "nonfiction"],
    # sports
    "sports": ["football", "soccer", "basketball", "baseball", "cricket", "tennis",
               "mma", "boxing", "wrestling", "formula1", "f1", "nascar", "motorsports"],
    "outdoor-sports": ["hiking", "climbing", "cycling", "running", "trail", "marathon",
                       "triathlon", "surfing", "skiing"],
    # community
    "hive": ["witness", "witnesses", "dhf", "hive-governance", "hivefest", "hivepower",
             "hive-dev", "hiveengine", "proposal", "hardfork", "hbd-stabilizer"],
    "contests": ["contest", "challenge", "giveaway", "competition", "raffle", "prizes"],
    "spirituality": ["spirituality", "meditation", "mindfulness", "astrology", "tarot",
                     "chakra", "reiki", "crystalhealing", "spiritual"],
    # nature
    "nature": ["nature", "wildlife", "environment", "climate", "conservation",
               "sustainability", "ecology", "birdwatching"],
}

# ── API helpers ──────────────────────────────────────────────────────────────

# ── Fetcher thread (HAFSQL) ─────────────────────────────────────────────────

def fetcher_thread(
    n_posts: int,
    post_queue: Queue,
    min_reputation: float,
    stop_event: threading.Event,
    status: dict,
):
    """Fetch recent posts from HAFSQL and push them into post_queue."""
    import psycopg2
    import psycopg2.extras

    fetched = 0
    batch_size = 200
    offset = 0
    conn = None
    retries = 0
    max_retries = 5

    log.info("[FETCH] Using HAFSQL — fetching up to %d recent posts ...", n_posts)

    def _connect():
        nonlocal conn
        try:
            if conn:
                conn.close()
        except Exception:
            pass
        conn = psycopg2.connect(HAFSQL_DSN)
        conn.autocommit = True

    try:
        _connect()
    except Exception as exc:
        log.error("[FETCH] Cannot connect to HAFSQL: %s", exc)
        post_queue.put(None)
        status["fetch_done"] = True
        return

    while fetched < n_posts and not stop_event.is_set():
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT c.author, c.permlink, c.title, c.body, c.created,
                       c.json_metadata,
                       r.reputation
                FROM hafsql.comments c
                LEFT JOIN hafsql.reputations r ON c.author = r.account_name
                WHERE c.parent_author = ''
                  AND LENGTH(c.body) >= 80
                ORDER BY c.created DESC
                LIMIT %s OFFSET %s
                """,
                (batch_size, offset),
            )
            rows = cur.fetchall()
            cur.close()
            retries = 0
        except Exception as exc:
            retries += 1
            log.warning("[FETCH] HAFSQL query failed (attempt %d/%d): %s", retries, max_retries, exc)
            if retries >= max_retries:
                log.error("[FETCH] Max retries reached — stopping")
                break
            time.sleep(3)
            try:
                _connect()
            except Exception:
                pass
            continue

        if not rows:
            log.info("[FETCH] No more posts from HAFSQL")
            break

        for row in rows:
            if stop_event.is_set() or fetched >= n_posts:
                break

            raw_rep = int(row.get("reputation") or 0)
            rep_score = _raw_rep_to_score(raw_rep)
            if rep_score < min_reputation:
                continue

            body = (row.get("body") or "").strip()
            if body.lstrip().startswith("@@"):
                continue

            post_queue.put({
                "author": row["author"],
                "permlink": row["permlink"],
                "title": (row.get("title") or "").strip(),
                "body": body[:1500],
                "timestamp": str(row.get("created", "")),
            })
            fetched += 1

        offset += batch_size
        status["fetched"] = fetched
        log.info("[FETCH] %d/%d posts fetched", fetched, n_posts)

    try:
        conn.close()
    except Exception:
        pass

    post_queue.put(None)
    status["fetch_done"] = True
    log.info("[FETCH] Done — %d posts queued for classification", fetched)


# ── Targeted fetcher for stratified sampling ─────────────────────────────────

def fetch_targeted(
    weak_categories: list[str],
    min_per_category: int,
    current_counts: dict[str, int],
    post_queue: Queue,
    min_reputation: float,
    stop_event: threading.Event,
    status: dict,
    seen_keys: set[str],
):
    """Fetch posts for under-represented categories using tag-based HAFSQL queries."""
    import psycopg2
    import psycopg2.extras

    conn = None
    try:
        conn = psycopg2.connect(HAFSQL_DSN)
        conn.autocommit = True
    except Exception as exc:
        log.error("[STRATIFY] Cannot connect to HAFSQL: %s", exc)
        return

    total_added = 0
    for cat in weak_categories:
        if stop_event.is_set():
            break

        tags = TAG_HINTS.get(cat)
        if not tags:
            log.warning("[STRATIFY] No tag hints for '%s' — skipping", cat)
            continue

        need = max(0, min_per_category - current_counts.get(cat, 0))
        # Fetch more than needed since not all will classify into the target category
        fetch_limit = need * 10

        log.info("[STRATIFY] '%s': have %d, need %d — fetching up to %d posts via tags %s",
                 cat, current_counts.get(cat, 0), min_per_category, fetch_limit, tags[:3])

        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT c.author, c.permlink, c.title, c.body, c.created,
                       c.json_metadata,
                       r.reputation
                FROM hafsql.comments c
                LEFT JOIN hafsql.reputations r ON c.author = r.account_name
                WHERE c.parent_author = ''
                  AND LENGTH(c.body) >= 80
                  AND c.json_metadata::jsonb->'tags' ?| %s
                ORDER BY c.created DESC
                LIMIT %s
                """,
                (tags, fetch_limit),
            )
            rows = cur.fetchall()
            cur.close()
        except Exception as exc:
            log.warning("[STRATIFY] Query failed for '%s': %s", cat, exc)
            continue

        added = 0
        for row in rows:
            if stop_event.is_set():
                break

            key = f"{row['author']}/{row['permlink']}"
            if key in seen_keys:
                continue

            raw_rep = int(row.get("reputation") or 0)
            rep_score = _raw_rep_to_score(raw_rep)
            if rep_score < min_reputation:
                continue

            body = (row.get("body") or "").strip()
            if body.lstrip().startswith("@@"):
                continue

            seen_keys.add(key)
            post_queue.put({
                "author": row["author"],
                "permlink": row["permlink"],
                "title": (row.get("title") or "").strip(),
                "body": body[:1500],
                "timestamp": str(row.get("created", "")),
            })
            added += 1

        total_added += added
        status["fetched"] = status.get("fetched", 0) + added
        log.info("[STRATIFY] '%s': queued %d posts from %d results", cat, added, len(rows))

    try:
        conn.close()
    except Exception:
        pass

    log.info("[STRATIFY] Done — queued %d targeted posts across %d categories",
             total_added, len(weak_categories))


# ── Classifier thread ────────────────────────────────────────────────────────

def classifier_thread(
    post_queue: Queue,
    labeled_list: list,
    all_posts: list,
    model: str,
    categories: list[str],
    checkpoint_every: int,
    stop_event: threading.Event,
    status: dict,
    lock: threading.Lock,
    seen_keys: set[str] | None = None,
    ensemble_models: list[str] | None = None,
):
    """Pull posts from queue and classify with LLM."""
    classified = 0
    skipped = 0
    _seen = seen_keys or set()

    while not stop_event.is_set():
        try:
            post = post_queue.get(timeout=2)
        except Empty:
            if status.get("fetch_done"):
                break
            continue

        if post is None:
            break

        key = f"{post['author']}/{post['permlink']}"
        with lock:
            if key not in _seen:
                all_posts.append(post)
                _seen.add(key)

        if ensemble_models:
            cats = classify_post_ensemble(post, ensemble_models, categories)
        else:
            cats = classify_post(post, model, categories)
        classified += 1

        if cats:
            with lock:
                labeled_list.append({**post, "categories": cats})
        else:
            skipped += 1

        status["classified"] = classified
        status["labeled"] = len(labeled_list)
        status["skipped"] = skipped

        if classified % 20 == 0:
            log.info(
                "[CLASSIFY] %d done — labeled=%d  no-match=%d  queue=%d",
                classified, len(labeled_list), skipped, post_queue.qsize(),
            )

        if classified % checkpoint_every == 0:
            with lock:
                _save_checkpoint(all_posts, labeled_list)

    log.info("[CLASSIFY] Done — %d classified, %d labeled, %d no-match",
             classified, len(labeled_list), skipped)


# ── Hive boilerplate stripping ────────────────────────────────────────────────
import re

_HIVE_BOILERPLATE_RE = re.compile(
    r"(?i)"
    r"(?:hello|hey|hi|greetings|dear)\s+(?:hiver|hivian|hive\s*friend|hive\s*family|hive\s*communit)\w*[!.,]*\s*"
    r"|(?:thanks?\s+(?:for|to)\s+(?:reading|visiting|stopping\s+by).*)"
    r"|(?:follow\s+me\s+on\s+hive.*)"
    r"|(?:posted\s+(?:via|using|on|from)\s+\w+.*)"
    r"|(?:earn\w*\s+(?:hive|hbd|crypto|token)\w*\s+(?:by|when|if|for)\b.*)"
    r"|(?:upvote|reblog|share)\s+(?:if|this|for).*"
)

def _strip_hive_boilerplate(text: str) -> str:
    """Remove Hive-platform greetings, footers, and crypto reward mentions
    so the LLM classifies by actual content, not platform noise."""
    return _HIVE_BOILERPLATE_RE.sub(" ", text).strip()


# ── LLM classification ────────────────────────────────────────────────────────

def classify_post(post: dict, model: str, categories: list[str]) -> list[str]:
    clean_body = _strip_hive_boilerplate(clean_post_body(post["body"]))
    prompt = CLASSIFY_PROMPT.format(
        categories=", ".join(categories),
        category_hints=_build_hint_block(),
        title=post["title"] or "(no title)",
        body=clean_body[:800],
    )
    try:
        resp = _ollama.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0},
        )
        text = resp.message.content.strip()
        start, end = text.find("["), text.rfind("]") + 1
        if start == -1 or end == 0:
            return []
        found = json.loads(text[start:end])
        return [c for c in found if c in categories]
    except Exception as exc:
        log.debug("classify failed %s/%s: %s", post.get("author"), post.get("permlink"), exc)
        return []


def classify_post_ensemble(
    post: dict, models: list[str], categories: list[str],
) -> list[str]:
    """Classify with multiple models, return intersection (agreed-upon categories)."""
    results: list[set[str]] = []
    for model in models:
        cats = classify_post(post, model, categories)
        results.append(set(cats))

    if not results:
        return []

    if len(results) == 1:
        return list(results[0])

    # Intersection: categories agreed upon by all models
    agreed = results[0]
    for s in results[1:]:
        agreed &= s

    # Fallback: if intersection is empty but majority agrees, use majority vote
    if not agreed and len(results) >= 2:
        from collections import Counter
        all_cats = [c for s in results for c in s]
        majority_threshold = len(results) / 2
        counts = Counter(all_cats)
        agreed = {c for c, n in counts.items() if n > majority_threshold}

    return sorted(agreed)


# ── Validation ───────────────────────────────────────────────────────────────

def validate_centroids(
    labeled: list[dict],
    embedder: SentenceTransformer,
    centroids: dict[str, list[float]],
    holdout_ratio: float = 0.2,
) -> dict:
    """Hold out a fraction of labeled posts and validate centroid classification.

    Returns a dict with per-category precision/recall and overall accuracy.
    """
    import random

    shuffled = labeled.copy()
    random.shuffle(shuffled)
    split = int(len(shuffled) * (1 - holdout_ratio))
    holdout = shuffled[split:]

    if not holdout:
        log.warning("[VALIDATE] No holdout posts — skipping validation")
        return {}

    # Build centroid matrix
    cat_names = sorted(centroids.keys())
    centroid_matrix = np.array([centroids[c] for c in cat_names])

    # Per-category stats
    tp: dict[str, int] = {c: 0 for c in cat_names}
    fp: dict[str, int] = {c: 0 for c in cat_names}
    fn: dict[str, int] = {c: 0 for c in cat_names}

    for post in holdout:
        llm_cats = set(c for c in post["categories"] if c in centroids)
        if not llm_cats:
            continue

        # Embed and classify via cosine similarity (same logic as worker)
        clean_body = clean_post_body(post.get("body", ""))
        title = post.get("title", "")
        text = f"{title} {clean_body}".strip()[:2000]
        emb = embedder.encode([text], normalize_embeddings=True)[0]

        sims = centroid_matrix @ emb
        best_idx = int(np.argmax(sims))
        best_score = float(sims[best_idx])

        # Assign top categories within 0.03 of best (mirrors worker logic)
        predicted = set()
        if best_score >= 0.30:
            for i, score in enumerate(sims):
                if score >= best_score - 0.03 and len(predicted) < 3:
                    predicted.add(cat_names[i])

        for c in cat_names:
            if c in predicted and c in llm_cats:
                tp[c] += 1
            elif c in predicted and c not in llm_cats:
                fp[c] += 1
            elif c not in predicted and c in llm_cats:
                fn[c] += 1

    # Compute metrics
    results: dict[str, dict] = {}
    for c in cat_names:
        precision = tp[c] / (tp[c] + fp[c]) if (tp[c] + fp[c]) > 0 else 0.0
        recall = tp[c] / (tp[c] + fn[c]) if (tp[c] + fn[c]) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        results[c] = {"precision": precision, "recall": recall, "f1": f1,
                       "tp": tp[c], "fp": fp[c], "fn": fn[c]}

    total_tp = sum(tp.values())
    total_fp = sum(fp.values())
    total_fn = sum(fn.values())
    macro_p = np.mean([r["precision"] for r in results.values()])
    macro_r = np.mean([r["recall"] for r in results.values()])
    macro_f1 = np.mean([r["f1"] for r in results.values()])

    return {
        "holdout_size": len(holdout),
        "per_category": results,
        "micro_precision": total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 0,
        "micro_recall": total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 0,
        "macro_precision": float(macro_p),
        "macro_recall": float(macro_r),
        "macro_f1": float(macro_f1),
    }


def _print_validation(val: dict, categories: list[str]) -> None:
    """Pretty-print validation results."""
    print()
    print("=" * 70)
    print("  CombFlow — Centroid Quality Validation")
    print("=" * 70)
    print(f"  Holdout posts    : {val['holdout_size']}")
    print(f"  Micro precision  : {val['micro_precision']:.1%}")
    print(f"  Micro recall     : {val['micro_recall']:.1%}")
    print(f"  Macro F1         : {val['macro_f1']:.1%}")
    print("=" * 70)

    per_cat = val["per_category"]
    # Sort by F1 ascending to highlight weak categories first
    print(f"\n  {'Category':<20} {'Prec':>6} {'Recall':>6} {'F1':>6}  {'TP':>4} {'FP':>4} {'FN':>4}")
    print(f"  {'-'*20} {'-'*6} {'-'*6} {'-'*6}  {'-'*4} {'-'*4} {'-'*4}")
    for cat in sorted(per_cat, key=lambda c: per_cat[c]["f1"]):
        r = per_cat[cat]
        if r["tp"] == 0 and r["fp"] == 0 and r["fn"] == 0:
            continue
        print(f"  {cat:<20} {r['precision']:>5.0%} {r['recall']:>5.0%} {r['f1']:>5.0%}"
              f"  {r['tp']:>4} {r['fp']:>4} {r['fn']:>4}")

    # Flag weak categories
    weak = [c for c, r in per_cat.items() if r["f1"] < 0.20 and (r["tp"] + r["fn"]) > 0]
    if weak:
        print(f"\n  Low-quality centroids (F1 < 20%): {', '.join(weak)}")
        print("  → Consider more training posts or category tree changes for these")
    print()


# ── Centroids ─────────────────────────────────────────────────────────────────

def compute_centroids(
    labeled: list[dict],
    embedder: SentenceTransformer,
    categories: list[str],
    min_posts: int,
    secondary_weight: float = 0.3,
    prune_pct: int = 20,
) -> dict[str, list[float]]:
    # p026 Change 2: weighted — primary=1.0, secondary=0.3
    by_cat: dict[str, list[tuple[str, float]]] = {c: [] for c in categories}
    for p in labeled:
        clean_body = clean_post_body(p.get("body", ""))
        title = p.get("title", "")
        text = f"{title} {clean_body}".strip()[:2000]
        for i, c in enumerate(p["categories"]):
            if c in by_cat:
                weight = 1.0 if i == 0 else secondary_weight
                by_cat[c].append((text, weight))

    centroids: dict[str, list[float]] = {}
    for cat, entries in by_cat.items():
        if len(entries) < min_posts:
            log.warning("'%s': %d posts (need %d) — skipping", cat, len(entries), min_posts)
            continue
        texts = [t for t, w in entries]
        weights = np.array([w for t, w in entries], dtype=np.float32)
        embs = embedder.encode(texts, normalize_embeddings=True, show_progress_bar=False)

        # Weighted mean centroid
        centroid = (embs * weights[:, None]).sum(axis=0)
        centroid /= np.linalg.norm(centroid)

        # p026 Change 3: outlier pruning — drop bottom percentile and recompute
        if prune_pct > 0 and len(entries) >= 20:
            sims = embs @ centroid
            threshold = np.percentile(sims, prune_pct)
            mask = sims >= threshold
            kept = int(mask.sum())
            if kept >= min_posts:
                w_masked = weights[mask]
                centroid = (embs[mask] * w_masked[:, None]).sum(axis=0)
                centroid /= np.linalg.norm(centroid)
                log.info("'%-16s  posts=%-4d  pruned=%d  centroid OK",
                         cat + "'", len(entries), len(entries) - kept)
            else:
                log.info("'%-16s  posts=%-4d  centroid OK (skip prune, too few)",
                         cat + "'", len(entries))
        else:
            log.info("'%-16s  posts=%-4d  centroid OK", cat + "'", len(entries))

        centroids[cat] = centroid.tolist()

    return centroids


def refine_centroids(
    labeled: list[dict],
    embedder: SentenceTransformer,
    centroids: dict[str, list[float]],
    categories: list[str],
    min_posts: int,
    secondary_weight: float = 0.3,
    rounds: int = 2,
    sim_floor: float = 0.15,
) -> dict[str, list[float]]:
    """Iterative centroid refinement: remove posts that don't match their assigned
    centroid, then recompute. This cleans up LLM mislabels that pollute centroids."""

    current = {c: np.array(v) for c, v in centroids.items()}

    # Pre-compute all embeddings once
    texts = []
    for p in labeled:
        clean_body = clean_post_body(p.get("body", ""))
        title = p.get("title", "")
        texts.append(f"{title} {clean_body}".strip()[:2000])
    log.info("[REFINE] Encoding %d posts ...", len(texts))
    all_embs = embedder.encode(texts, normalize_embeddings=True, show_progress_bar=True)

    for rnd in range(rounds):
        by_cat: dict[str, list[tuple[np.ndarray, float]]] = {c: [] for c in current}
        removed_total = 0

        for idx, p in enumerate(labeled):
            emb = all_embs[idx]
            for i, c in enumerate(p["categories"]):
                if c not in current:
                    continue
                sim = float(emb @ current[c])
                if sim < sim_floor:
                    removed_total += 1
                    continue
                weight = 1.0 if i == 0 else secondary_weight
                by_cat[c].append((emb, weight))

        log.info("[REFINE] round %d: removed %d low-similarity labels (floor=%.2f)",
                 rnd + 1, removed_total, sim_floor)

        for cat, entries in by_cat.items():
            if len(entries) < min_posts:
                continue
            embs = np.array([e for e, w in entries])
            weights = np.array([w for e, w in entries], dtype=np.float32)
            centroid = (embs * weights[:, None]).sum(axis=0)
            centroid /= np.linalg.norm(centroid)
            current[cat] = centroid

    return {c: v.tolist() for c, v in current.items()}


def adjust_contrastive(
    centroids: dict[str, list[float]], alpha: float = 0.1, k: int = 2,
) -> dict[str, list[float]]:
    """p026 Change 5: push each centroid away from its nearest neighbors."""
    cats = list(centroids.keys())
    if len(cats) <= k:
        return centroids

    vecs = np.array([centroids[c] for c in cats])
    # Pairwise cosine similarity (vectors already normalized)
    sims = vecs @ vecs.T
    np.fill_diagonal(sims, -1)  # exclude self

    adjusted: dict[str, list[float]] = {}
    for i, cat in enumerate(cats):
        neighbors = np.argsort(sims[i])[-k:]
        repulsion = sum(vecs[i] - vecs[j] for j in neighbors)
        new_vec = vecs[i] + alpha * repulsion
        new_vec /= np.linalg.norm(new_vec)
        # Log how much the centroid moved
        shift = float(1.0 - np.dot(vecs[i], new_vec))
        adjusted[cat] = new_vec.tolist()
        if shift > 0.001:
            nn_cats = [cats[j] for j in neighbors]
            log.info("'%-16s  contrastive shift=%.4f  away from %s",
                     cat + "'", shift, ", ".join(nn_cats))

    return adjusted


# ── Checkpoint ────────────────────────────────────────────────────────────────

def _backup_file(path: Path) -> None:
    """Create a timestamped backup before overwriting."""
    if path.exists():
        ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        backup = path.with_suffix(f".{ts}{path.suffix}")
        backup.write_bytes(path.read_bytes())
        log.info("[BACKUP] %s -> %s", path.name, backup.name)


def _save_checkpoint(posts: list[dict], labeled: list[dict]) -> None:
    SEEDS_DIR.mkdir(exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps({
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "posts": posts,
        "labeled": labeled,
    }, indent=2))
    log.info("[CKPT] saved (%d posts, %d labeled)", len(posts), len(labeled))


def _load_checkpoint() -> tuple[list[dict], list[dict]]:
    if not CHECKPOINT_FILE.exists():
        return [], []
    data = json.loads(CHECKPOINT_FILE.read_text())
    log.info(
        "Resuming: %d posts, %d labeled",
        len(data.get("posts", [])), len(data.get("labeled", [])),
    )
    return data.get("posts", []), data.get("labeled", [])


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    hw = p.add_mutually_exclusive_group()
    hw.add_argument("--hardware", choices=list(HARDWARE_MODELS), default=DEFAULT_HARDWARE, metavar="TIER")
    hw.add_argument("--model", metavar="NAME", help="Override Ollama model directly")

    p.add_argument("--posts", type=int, default=3000, metavar="N")
    p.add_argument("--min-per-category", type=int, default=5, metavar="N")
    p.add_argument("--threshold", type=float, default=SIMILARITY_THRESHOLD)
    p.add_argument("--embedding-model", default=EMBEDDING_MODEL)
    p.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    p.add_argument("--incremental", action="store_true",
                   help="Fetch only new posts (skip checkpointed), merge into existing labels")
    p.add_argument("--stratify", action="store_true",
                   help="After broad fetch, do targeted fetches for under-represented categories")
    p.add_argument("--ensemble", nargs="*", metavar="MODEL",
                   help="Multi-model ensemble labeling (intersection). "
                        "No args = default pair; or specify models explicitly")
    p.add_argument("--report", action="store_true",
                   help="Print category coverage report from checkpoint and exit")
    p.add_argument("--validate", action="store_true",
                   help="After computing centroids, run holdout validation and report accuracy")
    p.add_argument("--checkpoint-every", type=int, default=50, metavar="N")
    p.add_argument("--min-reputation", type=float, default=MIN_AUTHOR_REPUTATION)
    # p026 centroid quality flags
    p.add_argument("--secondary-weight", type=float, default=0.3, metavar="W",
                   help="Weight for secondary category labels in centroid computation; "
                        "primary=1.0, secondary=W (p026, default 0.3)")
    p.add_argument("--prune-pct", type=int, default=20, metavar="PCT",
                   help="Drop bottom PCT%% outlier posts per category before centroid computation (p026)")
    p.add_argument("--contrastive", type=float, default=0.1, metavar="ALPHA",
                   help="Contrastive centroid adjustment strength; 0 to disable (p026)")
    p.add_argument("--refine", type=int, default=0, metavar="ROUNDS",
                   help="Iterative centroid refinement rounds — removes mislabeled posts by "
                        "embedding similarity, then recomputes centroids. 2 rounds recommended.")
    p.add_argument("--refine-floor", type=float, default=0.15, metavar="SIM",
                   help="Minimum cosine similarity to keep a post for its labeled category (default 0.15)")
    return p.parse_args()


def _print_summary(counts: dict, categories: list[str], min_posts: int) -> None:
    total = sum(counts.values())
    print()
    print(f"  {'Category':<20} {'Posts':>6}  Status")
    print(f"  {'-'*20}  {'-'*6}  {'-'*18}")
    for cat in categories:
        n = counts.get(cat, 0)
        if n == 0:
            continue
        status = "OK" if n >= min_posts else f"skip (need {min_posts})"
        print(f"  {cat:<20} {n:>6}  {status}")
    print(f"\n  Total labeled: {total}\n")


def _print_report(categories: list[str], min_posts: int) -> None:
    """Load checkpoint and print a detailed category coverage report."""
    if not CHECKPOINT_FILE.exists():
        print("No checkpoint found. Run a seed pass first.", file=sys.stderr)
        sys.exit(1)

    all_posts, labeled = _load_checkpoint()
    counts: dict[str, int] = {}
    for p in labeled:
        for c in p["categories"]:
            counts[c] = counts.get(c, 0) + 1

    total_labels = sum(counts.values())
    cats_with_labels = sum(1 for c in categories if counts.get(c, 0) > 0)

    print()
    print("=" * 70)
    print("  CombFlow — Category Coverage Report")
    print("=" * 70)
    print(f"  Checkpoint       : {CHECKPOINT_FILE}")
    print(f"  Total posts      : {len(all_posts)}")
    print(f"  Labeled posts    : {len(labeled)}")
    print(f"  Total labels     : {total_labels}  (posts can have multiple)")
    print(f"  Categories       : {cats_with_labels}/{len(categories)} have labels")
    print(f"  Min per category : {min_posts}")
    print("=" * 70)

    # Sorted by count descending
    print(f"\n  {'Category':<20} {'Posts':>6}  {'%':>5}  Status")
    print(f"  {'-'*20}  {'-'*6}  {'-'*5}  {'-'*30}")
    for cat in sorted(categories, key=lambda c: counts.get(c, 0), reverse=True):
        n = counts.get(cat, 0)
        pct = (n / total_labels * 100) if total_labels else 0
        if n == 0:
            status = "MISSING"
        elif n < min_posts:
            hints = TAG_HINTS.get(cat)
            hint_str = f"  tags: {', '.join(hints[:3])}" if hints else "  (no tag hints)"
            status = f"WEAK (need {min_posts}){hint_str}"
        else:
            status = "OK"
        print(f"  {cat:<20} {n:>6}  {pct:>4.1f}%  {status}")

    # Summary sections
    missing = [c for c in categories if counts.get(c, 0) == 0]
    weak = [c for c in categories if 0 < counts.get(c, 0) < min_posts]
    no_hints = [c for c in (missing + weak) if c not in TAG_HINTS]

    if missing:
        print(f"\n  Missing ({len(missing)}): {', '.join(missing)}")
    if weak:
        print(f"  Weak ({len(weak)}): {', '.join(weak)}")
    if no_hints:
        print(f"  No tag hints ({len(no_hints)}): {', '.join(no_hints)}")
        print("  → Add entries to TAG_HINTS in seed_categories.py for better stratification")

    print()
    if missing or weak:
        print("  Suggestion: run with --stratify to target under-represented categories")
        print(f"    python scripts/seed_categories.py --resume --stratify --posts {len(all_posts)}")
    else:
        print("  All categories meet the minimum threshold!")
    print()


def main() -> None:
    args = parse_args()
    model = args.model or HARDWARE_MODELS[args.hardware][0]
    categories = LEAF_CATEGORIES

    # ── Report mode (no API key needed) ──────────────────────────────────────
    if args.report:
        _print_report(categories, args.min_per_category)
        return

    # ── Resolve ensemble models ──────────────────────────────────────────────
    ensemble_models: list[str] | None = None
    if args.ensemble is not None:
        if args.ensemble:
            ensemble_models = args.ensemble
        else:
            # Default pair: primary model + next tier up
            ensemble_models = [model, "mistral-nemo:12b"] if model != "mistral-nemo:12b" \
                else [model, "llama3.1:8b"]
        log.info("Ensemble mode: %s", ensemble_models)

    print("=" * 62)
    print("  CombFlow — category centroid seeding")
    print("=" * 62)
    print(f"  LLM model        : {model}")
    if ensemble_models:
        print(f"  Ensemble models  : {', '.join(ensemble_models)}")
    print(f"  Embedding model  : {args.embedding_model}")
    print(f"  Posts target     : {args.posts}")
    print(f"  Leaf categories  : {len(categories)}")
    print(f"  Min reputation   : {args.min_reputation}")
    print(f"  Min per category : {args.min_per_category}")
    print(f"  Data source      : HAFSQL")
    if args.resume or args.incremental:
        print("  Resume           : from checkpoint")
    if args.incremental:
        print("  Incremental      : fetch new posts only, merge with existing")
    if args.stratify:
        print("  Stratify         : targeted fetch for weak categories")
    if args.validate:
        print("  Validate         : holdout validation after centroid computation")
    print("=" * 62)
    print()

    SEEDS_DIR.mkdir(exist_ok=True)

    # ── Check for resume / cached posts ───────────────────────────────────────
    all_posts: list[dict] = []
    labeled: list[dict] = []

    if (args.resume or args.incremental) and CHECKPOINT_FILE.exists():
        all_posts, labeled = _load_checkpoint()

    already_classified = {f"{p['author']}/{p['permlink']}" for p in labeled}

    unclassified_cached = [
        p for p in all_posts
        if f"{p['author']}/{p['permlink']}" not in already_classified
    ]
    if args.incremental:
        # Incremental: always fetch args.posts NEW posts on top of existing cache
        remaining_to_fetch = args.posts
    else:
        remaining_to_fetch = max(0, args.posts - len(all_posts))

    log.info("Cached: %d posts (%d labeled, %d to classify). Need to fetch: %d more.",
             len(all_posts), len(labeled), len(unclassified_cached), remaining_to_fetch)

    # ── Set up parallel pipeline ──────────────────────────────────────────────
    post_queue: Queue = Queue(maxsize=100)
    stop_event = threading.Event()
    lock = threading.Lock()
    status: dict = {"fetched": 0, "classified": 0, "labeled": 0, "skipped": 0,
                    "fetch_done": False}

    # Track posts already in all_posts to avoid duplicates when resuming.
    seen_keys = {f"{p['author']}/{p['permlink']}" for p in all_posts}

    # Pre-fill queue with unclassified cached posts (non-blocking to avoid
    # deadlock — Queue maxsize=100 but there may be more cached posts).
    prefill_overflow: list[dict] = []
    for p in unclassified_cached:
        try:
            post_queue.put_nowait(p)
        except Exception:
            prefill_overflow.append(p)

    fetch_t = None
    need_fetch = remaining_to_fetch > 0
    if need_fetch:
        fetch_t = threading.Thread(
            target=fetcher_thread,
            args=(remaining_to_fetch, post_queue,
                  args.min_reputation, stop_event, status),
            daemon=True,
        )
        fetch_t.start()

    classify_t = threading.Thread(
        target=classifier_thread,
        args=(post_queue, labeled, all_posts, model, categories,
              args.checkpoint_every, stop_event, status, lock, seen_keys,
              ensemble_models),
        daemon=True,
    )
    classify_t.start()

    # Drain any overflow from pre-fill (now safe — classifier is consuming).
    for p in prefill_overflow:
        post_queue.put(p)
    del prefill_overflow

    # Signal end-of-input if no fetcher thread is producing more posts.
    if not need_fetch:
        post_queue.put(None)
        status["fetch_done"] = True

    # ── Wait with progress reporting ──────────────────────────────────────────
    try:
        while classify_t.is_alive():
            classify_t.join(timeout=30)
            if classify_t.is_alive():
                log.info(
                    "[STATUS] fetched=%d  classified=%d  labeled=%d  skipped=%d  queue=%d",
                    status["fetched"],
                    status["classified"],
                    status["labeled"],
                    status["skipped"],
                    post_queue.qsize(),
                )
    except KeyboardInterrupt:
        log.warning("Interrupted — saving checkpoint ...")
        stop_event.set()
        if fetch_t:
            fetch_t.join(timeout=5)
        classify_t.join(timeout=5)
        with lock:
            _save_checkpoint(all_posts, labeled)
        log.info("Resume with:  python scripts/seed_categories.py --resume")
        sys.exit(0)

    if fetch_t:
        fetch_t.join(timeout=10)

    _save_checkpoint(all_posts, labeled)

    log.info("Pipeline complete: %d posts, %d labeled", len(all_posts), len(labeled))

    counts: dict[str, int] = {}
    for p in labeled:
        for c in p["categories"]:
            counts[c] = counts.get(c, 0) + 1
    _print_summary(counts, categories, args.min_per_category)

    # ── Stratified sampling pass ─────────────────────────────────────────────
    if args.stratify:
        weak_cats = [c for c in categories if counts.get(c, 0) < args.min_per_category]
        if weak_cats:
            log.info("[STRATIFY] %d categories below threshold — starting targeted fetch",
                     len(weak_cats))

            seen_keys = {f"{p['author']}/{p['permlink']}" for p in all_posts}
            strat_queue: Queue = Queue(maxsize=100)

            fetch_targeted(
                weak_cats, args.min_per_category, counts,
                strat_queue, args.min_reputation, stop_event, status, seen_keys,
            )
            strat_queue.put(None)
            status["fetch_done"] = True

            # Classify the targeted posts
            strat_classify = threading.Thread(
                target=classifier_thread,
                args=(strat_queue, labeled, all_posts, model, categories,
                      args.checkpoint_every, stop_event, status, lock, seen_keys,
                      ensemble_models),
                daemon=True,
            )
            strat_classify.start()

            try:
                strat_classify.join()
            except KeyboardInterrupt:
                log.warning("Interrupted during stratify — saving checkpoint ...")
                stop_event.set()
                strat_classify.join(timeout=5)
                with lock:
                    _save_checkpoint(all_posts, labeled)
                sys.exit(0)

            _save_checkpoint(all_posts, labeled)

            # Recount
            counts = {}
            for p in labeled:
                for c in p["categories"]:
                    counts[c] = counts.get(c, 0) + 1

            log.info("[STRATIFY] After targeted pass: %d posts, %d labeled",
                     len(all_posts), len(labeled))
            _print_summary(counts, categories, args.min_per_category)
        else:
            log.info("[STRATIFY] All categories meet minimum threshold — no targeted fetch needed")

    # ── Compute centroids ─────────────────────────────────────────────────────
    log.info("Loading embedding model %s ...", args.embedding_model)
    embedder = SentenceTransformer(args.embedding_model)
    centroids = compute_centroids(
        labeled, embedder, categories, args.min_per_category,
        secondary_weight=args.secondary_weight, prune_pct=args.prune_pct,
    )

    if not centroids:
        log.error("No centroids computed — try --posts %d or --min-per-category %d",
                  args.posts * 2, max(1, args.min_per_category // 2))
        sys.exit(1)

    # Iterative refinement: remove mislabeled posts by embedding similarity
    if args.refine > 0:
        log.info("Refining centroids (%d rounds, floor=%.2f) ...", args.refine, args.refine_floor)
        centroids = refine_centroids(
            labeled, embedder, centroids, categories, args.min_per_category,
            secondary_weight=args.secondary_weight,
            rounds=args.refine, sim_floor=args.refine_floor,
        )

    # p026 Change 5: contrastive centroid adjustment
    if args.contrastive > 0:
        log.info("Applying contrastive adjustment (alpha=%.2f) ...", args.contrastive)
        centroids = adjust_contrastive(centroids, alpha=args.contrastive)

    # ── Validation ────────────────────────────────────────────────────────────
    if args.validate:
        log.info("Running holdout validation ...")
        val = validate_centroids(labeled, embedder, centroids)
        if val:
            _print_validation(val, categories)

    # ── Save + upload ─────────────────────────────────────────────────────────
    metadata = {
        "llm_model": model,
        "embedding_model": args.embedding_model,
        "similarity_threshold": args.threshold,
        "posts_fetched": len(all_posts),
        "posts_labeled": len(labeled),
        "category_counts": counts,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if ensemble_models:
        metadata["ensemble_models"] = ensemble_models
    _backup_file(CHECKPOINT_FILE)
    _backup_file(CENTROIDS_FILE)
    CENTROIDS_FILE.write_text(json.dumps({"metadata": metadata, "centroids": centroids}, indent=2))
    log.info("Saved %d centroids -> %s", len(centroids), CENTROIDS_FILE)

    print(f"\nGenerated centroids for: {', '.join(centroids)}")

    print("Run './deploy.sh up' to rebuild with new seeds baked in.")


if __name__ == "__main__":
    main()
