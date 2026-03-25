# CombFlow

> Semantic post discovery for the Hive blockchain — streams live, backfills history, classifies with AI.

CombFlow listens to the Hive blockchain, classifies posts by meaning (not just keywords), detects multiple languages and sentiment, and stores them for exploration. The **HiveComb** discovery UI lets users browse and filter posts in a hex grid, read threaded comment discussions, post comments and top-level posts, discover and join Hive communities, and post to communities — all via Hive Keychain without leaving the app.

---

## What's inside

```
┌──────────────────────────────────────────────┐
│  Hive blockchain                             │
│  ·  live blocks every 3 s                    │
│  ·  years of history (via HAFSQL)            │
└──────────────────┬───────────────────────────┘
                   │ nectar stream + HAFSQL backfill
         ┌─────────▼──────────┐
         │   Hive Worker      │  classifies + sentiment + languages
         │   (two-phase)      │  catch-up then explore
         └─────────┬──────────┘
                   │ SQLAlchemy async
         ┌─────────▼──────────┐
         │   PostgreSQL 17 +  │  posts, categories, centroids
         │   pgvector         │  (HNSW)
         └─────────▲──────────┘
                   │
         ┌─────────┴──────────┐        ┌─────────────────┐
         │   FastAPI app      │◄───────│  Seed Script    │
         │   /ui /api/browse  │        │  (runs on host) │
         │                    │        │  LLM + GPU      │
         └────────────────────┘        └─────────────────┘
```

### How it works

1. **Seed script** runs on your GPU. Fetches Hive posts (with stratified sampling for rare categories), classifies them with a local LLM, optionally uses multi-model ensemble voting, computes per-category centroid vectors, and uploads them.
2. **Worker** streams blocks from Hive (live) and walks backwards through HAFSQL (backfill). For each post: embeds the body in-process (`all-MiniLM-L6-v2`), compares against centroids, detects languages (`fasttext` + `json_metadata`), analyses sentiment (embedding-based), auto-maps Hive communities to categories (embedding community title+about, +0.08 boost), and saves everything directly to PostgreSQL.
3. **HiveComb UI** shows posts in a honeycomb hex grid built with **Alpine.js** (~17KB, CDN-loaded, no build step) for reactive rendering. Collapsible chip-based filters (category, sentiment, language, author), sticky filter bar, endless scrolling, sort toggle, lazy thumbnails, visibility-aware live polling, and toast notifications. WCAG AA accessible with full keyboard navigation. Three layout modes (hex grid, card grid, list) rendered via Alpine `x-for` loops. Embeds YouTube, 3Speak, and Instagram Reel videos. Hierarchical comment trees loaded directly from the Hive chain. Community discovery suggestions bar with subscribe/unsubscribe via Keychain. Open Graph meta tags for social media previews. Cross-post detection and thumbnail support (PeakD `cross_post_key` and Ecency `original_author`/`original_permlink` formats).
4. **Hive Keychain auth** — users log in with their Hive account via Keychain browser extension (pure client-side, no server auth endpoints). Logged-in users can save default filter preferences on-chain (via `posting_json_metadata`), post comments and replies, author new top-level posts (to their blog or a community, with optional cross-post), follow/unfollow users, and join/leave communities — all broadcast client-side via Keychain (no private keys touch the server).

### Category hierarchy (2 levels)

**9 parents, 38 leaf categories:**

| Parent | Leaves |
|--------|--------|
| technology | crypto, programming, ai, cybersecurity |
| creative | photography, art, music, writing, video, diy-crafts |
| lifestyle | travel, food, fashion, homesteading, gardening, pets |
| science-education | nature, science, education, health-fitness |
| society | politics, philosophy, history, social-issues |
| finance-business | finance, entrepreneurship, precious-metals |
| entertainment | gaming, movies-tv, books |
| sports | team-sports, combat-sports, motorsports, outdoor-sports, chess |
| community | hive, contests, spirituality |

Classification happens at the leaf level. Filtering by a parent covers all its children.

---

## Quick start

### 1. Configure

```bash
cp .env.example .env
# Edit .env — at minimum set:
#   POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
#   DATABASE_URL  (must match the postgres vars)
#   (no API_KEY needed — auth is pure client-side via Keychain)
```

### 2. Deploy

```bash
./deploy.sh up
```

This builds the image, runs migrations, verifies tables, seeds the category tree, and starts:

| Service | What it does |
|---------|-------------|
| `combflow-app` | FastAPI on port 8000 (1G memory limit) |
| `db` | PostgreSQL 17 + pgvector |
| `hive_worker` | Streams + classifies + saves posts (2G memory limit) |
| `caddy` | Reverse proxy on port 8080 (HTTP only, behind external nginx for TLS), domain routing, access logging, API→docs redirect (128M memory limit) |
| `prerender` | Headless Chromium prerender for bot/crawler SSR — Caddy routes bot user-agents here for fully-rendered HTML (1G memory limit) |
| `goaccess` | Usage stats dashboard at `CADDY_API/stats` — regenerates from Caddy access logs every 60s (128M memory limit) |

### 3. Check

```bash
./deploy.sh status
curl http://localhost:8000/categories | python3 -m json.tool
```

### 4. Seed the classifier (first time only)

Without seeds, posts are saved but not classified. The seed script fixes that.

```bash
# Install host deps (GPU-enabled torch + Ollama)
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install -r scripts/requirements.txt
ollama pull llama3.1:8b

# Run
export API_KEY=your-secret-key
python scripts/seed_categories.py --posts 3000
```

After seeding, the worker starts categorising immediately — no restart needed.

**Seed options:**

```
python scripts/seed_categories.py --hardware 16gb    # 16gb (default), 8gb, 24gb, 40gb, 80gb
python scripts/seed_categories.py --posts 3000       # more posts = better centroids
python scripts/seed_categories.py --resume           # resume an interrupted run
python scripts/seed_categories.py --stratify         # targeted fetch for under-represented categories
python scripts/seed_categories.py --report           # print category coverage report and exit
python scripts/seed_categories.py --min-reputation 25  # stricter reputation filter
```

---

## HiveComb UI

Visit **http://localhost:8000/ui** to browse posts in a honeycomb grid.

- **Filters** — collapsible sections for categories (parent toggles all children), sentiment (positive/neutral/negative chips), and languages (multi-select chips)
- **Filtered total** — results bar shows "Showing X of Y posts" where Y reflects active filters
- **Dynamic** — filters apply instantly with 150ms debounce, no page reload
- **Endless scrolling** — more posts load automatically as you scroll down
- **Sort toggle** — sort by newest or by most recent classification
- **Read tracking** — opened posts are dimmed so you can see what's new
- **Post modal** — click a hex to see full content, categories, languages, and sentiment (body scroll locked while open). Renders Hive-standard two-column layout classes (`pull-left`/`pull-right`/`text-justify`) for bilingual and multi-column posts, with responsive stacking on mobile.
- **Comment threads** — hierarchical comments loaded directly from the Hive chain (via `bridge.get_discussion`), reputation-filtered (rep <= 0 hidden), collapsible nested replies, upvote button with vote count on each comment (same vote weight logic as posts)
- **Comment posting** — logged-in users can post comments and replies via Hive Keychain, with 3-second cooldown and cache invalidation
- **Post authoring** — pen icon opens a full editor with title, preview description (120 chars, stored in `json_metadata.description`), markdown body with formatting toolbar (bold, italic, headings, links, images, lists, quotes, code blocks, tables, center, two-column layout, @mentions — plus Ctrl+B/I/K shortcuts), image upload via clipboard paste, drag-and-drop, or file picker (uploaded to Hive image hosting via Keychain-signed requests), markdown help modal, tag autocomplete from categories, community selector (blog vs joined communities), cross-post toggle, payout preference (Power Up 100% / 50/50 / Decline), and localStorage draft auto-save
- **Location picker** — map button in the editor opens a Leaflet/OpenStreetMap modal; click to place a pin or use "My Location" (browser geolocation). Reverse geocoding via Nominatim auto-fills the location name. Inserts a worldmappin-compatible hidden tag in the post body
- **Engagement stats** — card and list views show vote count and comment count next to each post (fetched from the Hive chain). Voting updates the count instantly with a visual bump animation.
- **Upvoting** — heart button on posts (card, list, and modal views) with live vote count. Dynamic vote weight adjusts automatically based on voting mana with configurable floor (default 50%) and max weight (default 25%) — users never run out of votes. Vote estimate in settings shows remaining votes based on current mana. Settings saved on-chain.
- **Follow users** — follow/unfollow button in post modal broadcasts Hive-native follow via Keychain. "Following" toggle in the filter bar shows only followed users' posts (active by default after login). Followed list synced from chain on login, cached in localStorage. Manage followed users in settings modal.
- **Mute users** — mute button in post modal broadcasts Hive-native mute via Keychain. Muted users' posts are hidden client-side. Unmute available in settings. Muted list synced from chain on login.
- **Notifications** — bell icon in the header shows unread Hive notifications (replies, mentions, votes, reblogs, follows, etc.) fetched from the Hive Bridge API. Unread count badge updates on login and periodically. Click to expand a dropdown of recent notifications with relative timestamps. Mark all as read via Keychain (writes last-read timestamp to `posting_json_metadata`). Notifications link to the relevant post or author profile.
- **Light/dark theme** — sun/moon toggle in the header switches between dark and light mode. Preference saved in localStorage (per-browser), respects system `prefers-color-scheme` by default.
- **Settings modal** — first-login setup for default filters (languages, categories, sentiment). Advanced section includes post payout preference (Power Up 100%, 50/50, or Decline Payout), NSFW toggle, and vote settings (manual mode, mana floor, max weight). Muted and followed users shown in a tabbed panel (only visible when you have muted or followed accounts). Preferences saved on-chain.
- **Profile avatars** — Hive profile pictures shown next to usernames in header, cards, list rows, and post modal
- **Community browsing** — community badges on posts (clickable to filter), community filter chips in sidebar, community info in post modal with hivel.ink link
- **Community discovery** — suggestions bar shows related communities when category filters are active; logged-in users can join/leave communities directly via Keychain
- **My Communities filter** — logged-in users can toggle a "My Communities" filter to show only posts from communities they've joined on Hive
- **Lazy thumbnails** — loaded on-demand as hexes enter the viewport
- **Sentiment borders** — each hex has a coloured border from red (negative) to green (positive)
- **Layout toggle** — switch between hex grid and card view (auto-selects cards on mobile)
- **Live polling** — visibility-aware, only polls when the tab is active; scales fetch limit based on time away (up to 200 posts on return)
- **Toast notifications** — non-blocking feedback for saves, errors, etc.
- **Author profile URLs** — `/@username` shows posts filtered by that author; also triggered by clicking any username

- **Curation mode** — advanced filter panel for manual curators (toggle visible when manual voting is enabled in settings). Post age slider (1h–7d with hourly/daily steps and +/- buttons), vote count dropdown, max payout $ input, and sort order (newest/oldest). Age and sort filters are server-side; vote count and payout filters apply client-side from cached Hive post metadata. Curation filter values persist per session; mode toggle persists across sessions in localStorage.
- **Keyboard navigation** — arrow keys, J/K to navigate posts, Enter/Space to open, H to vote, C to comment
- **Cross-post URL support** — Hive-style prefixed URLs (`/community/@author/permlink`) redirect to canonical deep links
- **Social previews** — post-specific Open Graph meta tags on deep links (title, description, thumbnail fetched from Hive API) for rich previews on Discord, Twitter, Slack, etc. Author profile pages show `@username — HiveComb`.
- **Misclassification reporting** — flag icon in post modal lets logged-in users report misclassified posts with a reason, signed via Hive Keychain (server verifies signature against on-chain posting keys)
- **Security** — CSP headers, SRI hashes on CDN resources, input validation, clickjacking protection, robust XSS-safe post rendering (raw-text tag stripping, unclosed iframe handling)
- **Accessibility** — WCAG AA: focus management, ARIA labels, keyboard navigation, colour contrast

---

## Authentication

Users log in with **Hive Keychain** (browser extension) — pure client-side, no server auth endpoints:

1. User enters their Hive username
2. Keychain signs a timestamped message with the user's Posting key (`requestSignBuffer`)
3. If signing succeeds, the username is stored in `localStorage`
4. Logout clears `localStorage`

No JWT, no cookies, no server-side auth. All chain operations (posting, voting, following, etc.) are broadcast directly via Keychain.

**Persistent preferences** — logged-in users can save default category, language, and sentiment filters on-chain in their Hive account's `posting_json_metadata` (under the `combflow` namespace). These are restored automatically on next visit and follow the user across devices.

---

## Seed script

The seed script (`scripts/seed_categories.py`) bootstraps the classification system:

- **Broad fetch** — queries HAFSQL for recent posts, classifies with a local Ollama LLM
- **Stratified sampling** (`--stratify`) — targeted tag-based queries for under-represented categories
- **Multi-model ensemble** (`--ensemble`) — runs multiple LLMs and takes majority vote
- **Incremental updates** (`--incremental`) — only fetches new posts, merges into existing checkpoint
- **Category coverage report** (`--report`) — prints per-category post counts, weak categories, and suggestions
- **Quality techniques** — disambiguation hints in prompts, primary-label weighting, outlier pruning, negative examples for confusable categories
- **Quality validation** — holds out 20% of labeled posts and reports precision/recall per category
- **Checkpoint/resume** — progress saved to `seeds/checkpoint.json`, resume with `--resume`
- **Automatic backups** — timestamped backups of `checkpoint.json` and `centroids.json` created before overwriting (e.g. `centroids.20260322_213757.json`)

---

## Multi-language support

Posts can have multiple languages (common on Hive where authors write bilingual content):

- **Detection**: `fasttext` (lid.176.ftz model) as primary detector, merged with `json_metadata` app-provided languages. The highest-confidence language is stored as `primary_language` on each post.
- **Storage**: `post_language` junction table (many-to-many)
- **Filtering**: browse endpoint accepts multiple language filters
- **Display**: all detected languages shown as tags in the discovery UI

---

## Worker

The worker runs entirely self-contained with two concurrent modes:

1. **Live stream** — follows the head of the chain via nectar, classifies new posts as they appear
2. **Backfill** (HAFSQL) — two-phase approach:
   - **Catch-up**: starts from NOW, works backwards to the saved frontier (covers downtime gaps)
   - **Explore**: continues from the frontier into older history
   - Unlimited retry with exponential backoff (10s–5min) on network failures — never dies on transient issues

Per-post pipeline:
- Check author reputation via HAFSQL (>= 20)
- Clean post body (strip markdown images, links, HTML, URLs) via shared `project/text.py`
- Reject posts with < 80 chars of meaningful text
- Classify against category centroids (sentence-transformers)
- Auto-map Hive communities to categories (fetch title+about via Hive API `bridge.get_community`, embed, 0.40 threshold, +0.08 boost)
- Detect languages (fasttext + json_metadata)
- Compute sentiment via embedding similarity
- Save classification + community mapping to PostgreSQL

No HTTP calls to the CombFlow API — the worker talks only to Hive nodes, HAFSQL, and its own PostgreSQL.

---

## Database

### Schema

| Table | Purpose |
|-------|---------|
| `posts` | Hive posts (author, permlink, created, sentiment, sentiment_score, community_id, is_nsfw, primary_language) |
| `categories` | 2-level hierarchy (parent_id for nesting) |
| `post_category` | Many-to-many: posts <-> categories |
| `post_language` | Many-to-many: posts <-> languages |
| `category_centroids` | 384-dim pgvector centroids + HNSW index |
| `stream_cursors` | Per-worker last-processed block |
| `community_mappings` | Auto-mapped community→category associations (worker-maintained) |
| `post_reports` | User-submitted misclassification reports with Hive Keychain signature verification |

### Indexes

- `ix_post_category_post_id_category_id` — composite on post_category
- `ix_post_category_category_id` — category lookups
- `ix_post_language_post_id` — post language lookups
- `ix_post_language_language` — language filter queries
- `ix_posts_community_id` — community filter queries
- `ix_posts_primary_language` — language filter queries
- `ix_community_mappings_category_slug` — community suggestion queries
- `ix_posts_created_desc` — descending date sort
- `ix_post_reports_post_id` — report lookups by post
- `ix_post_reports_created_at` — paginated report listing

### Migration

Migrations `001_initial_schema.py` (base schema) and `002_post_reports.py` (misclassification reports). Verified by `alembic/verify_migration.py` on startup.

### Persistence

Data lives in the `postgres_data` Docker volume. It survives restarts, rebuilds, and `./deploy.sh restart`. Only `./deploy.sh clean` destroys it.

### Backup

```bash
./deploy.sh backup
# -> backup_20260319_120000.sql
```

---

## Tests

Tests use a separate `combflow_test` database. Create it once:

```bash
docker-compose exec -T db psql -U combflow -d postgres -c "CREATE DATABASE combflow_test OWNER combflow;"
docker-compose exec -T db psql -U combflow -d combflow_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Then run:

```bash
DB_IP=$(docker inspect combflow_db_1 --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
DATABASE_URL="postgresql+asyncpg://combflow:change_me@${DB_IP}/combflow_test" \
  .venv/bin/python -m pytest tests/ -v
```

Tests use in-process fixtures with a real DB — they don't interfere with the running worker.

266 tests across 11 files:

| File | Tests | Coverage |
|------|-------|----------|
| `test_worker_utils.py` | 68 | Classification, sentiment, language detection, community resolution + boost + persistence, pipeline end-to-end, model loaders |
| `test_browse.py` | 55 | Browse with all filter combinations, single + multi community filter, authors filter, max_age + sort filters, filter list truncation, pagination edge cases (including cursor + sort=oldest), communities endpoint, suggested communities, cache TTL |
| `test_api.py` | 35 | Health, categories, HTML page routes, GZip middleware, OG meta tags (parametrized), 404s |
| `test_hafsql.py` | 25 | Reputation conversion, community metadata parsing (parametrized error handling), post body lookup, connection pool, cursor lifecycle |
| `test_crud.py` | 23 | Retry decorator, category tree, seed idempotency |
| `test_stream.py` | 17 | Stream timestamp parsing, batch processing (reputation, blacklist, HAFSQL fallback) |
| `test_reports.py` | 14 | Misclassification reporting, signature verification (parametrized), pagination |
| `test_text.py` | 9 | Text cleaning utilities |
| `test_cache.py` | 9 | TTL cache operations, cached_response decorator |
| `test_backfill.py` | 8 | Backfill thread: filtering, catch-up phase, error handling, stop signal |
| `test_posts.py` | 2 | Post detail, community_id handling |

---

## API reference

Interactive docs: **http://localhost:8000/docs** (Swagger UI)

CORS is open by default — any origin can call the API. To restrict access, set `CORS_ORIGINS` in `.env` (e.g. `["https://myapp.com"]`).

### Public (no auth)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check |
| GET | `/categories` | Full 2-level category tree |
| GET | `/posts/{author}/{permlink}` | Post detail with categories, languages, sentiment |
| GET | `/api/browse` | Browse posts (query: `category`, `language`, `sentiment`, `community`, `communities`, `authors`, `max_age`, `sort`, `limit`, `offset`) |
| GET | `/api/languages` | Available languages with post counts |
| GET | `/api/stats` | Overview statistics |
| GET | `/api/communities` | Communities with post counts, names, and categories |
| GET | `/api/communities/suggested` | Suggested communities for given category filters (cached 300s) |
| POST | `/api/posts/{author}/{permlink}/report` | Report a misclassified post (requires Keychain signature) |
| GET | `/api/reports` | List misclassification reports (paginated, filterable) |

---

## Using the API from your own app

The API is public and CORS-open — you can call it from any frontend, mobile app, or script.

```bash
# Browse all posts
curl https://your-server:8000/api/browse

# Filter by category (parent or leaf)
curl 'https://your-server:8000/api/browse?category=crypto&category=ai'

# Filter by community
curl 'https://your-server:8000/api/browse?community=hive-174578'

# Filter by multiple communities (e.g. all communities you've joined)
curl 'https://your-server:8000/api/browse?communities=hive-174578&communities=hive-163772'

# Filter by authors (e.g. users you follow)
curl 'https://your-server:8000/api/browse?authors=alice&authors=bob'

# Filter by language and sentiment
curl 'https://your-server:8000/api/browse?language=en&sentiment=positive&limit=20'

# Filter by post age (e.g. last 6 hours, useful for curation)
curl 'https://your-server:8000/api/browse?max_age=6h'

# Sort oldest first (combine with max_age for chronological curation)
curl 'https://your-server:8000/api/browse?max_age=1d&sort=oldest'

# Paginate with cursor (from previous response's next_cursor)
curl 'https://your-server:8000/api/browse?cursor=1711234567.0_4821'

# Get available languages
curl https://your-server:8000/api/languages

# Get category tree
curl https://your-server:8000/categories

# Get a specific post
curl https://your-server:8000/posts/alice/my-post-permlink
```

---

## Environment variables

| Variable | Description | Example |
|----------|-------------|---------|
| `DATABASE_URL` | asyncpg connection string | `postgresql+asyncpg://user:pass@db/combflow` |
| `POSTGRES_USER` | Postgres username | `combflow` |
| `POSTGRES_PASSWORD` | Postgres password | `change-me` |
| `POSTGRES_DB` | Postgres database name | `combflow` |
| `CADDY_UI` | UI domain (bare hostname, TLS handled by external nginx) | `honeycomb.example.com` |
| `CADDY_API` | API domain (root redirects to `/docs`) | `api.example.com` |
| `CADDY_UI_OLD` | Previous UI domain — 301 redirects to `CADDY_UI` | `old.example.com` |

### Usage statistics

A GoAccess dashboard is served at your API domain's `/stats` path (e.g. `https://api.example.com/stats`). It shows visitors, requests, paths, status codes, browsers, and more — updated every 60 seconds from Caddy's JSON access logs. No configuration needed beyond the standard `CADDY_API` setting.

---

## Project layout

```
combflow/combflow/
├── project/
│   ├── categories.py     # 2-level category tree (9 parents, 38 leaves)
│   ├── config.py          # pydantic-settings
│   ├── text.py            # shared text cleaning (zero deps, used by worker + seed script)
│   ├── cache.py           # in-process TTL cache
│   ├── hafsql.py          # HAFSQL PostgreSQL client (reputation, backfill)
│   ├── api/
│   │   ├── main.py        # FastAPI app, lifespan, OpenAPI config
│   │   ├── deps.py        # DB session dependency
│   │   ├── hive_auth.py    # Hive signature verification (secp256k1)
│   │   ├── routes/
│   │   │   ├── posts.py     # GET /posts/{author}/{permlink}
│   │   │   ├── reports.py   # POST report, GET /api/reports
│   │   │   └── ui.py        # HTML pages, browse API
│   │   └── templates/
│   │       ├── discover.html  # HiveComb discovery UI (Alpine.js reactive templates)
│   │       └── static/
│   │           ├── shared.js    # auth, validation, read tracking, focus trap, toasts
│   │           ├── theme.js       # FOUC-free theme init (runs before first paint)
│   │           ├── shared/      # shared modules
│   │           │   ├── keychain.js  # Keychain broadcasting (vote, comment, post, follow, mute, subscribe)
│   │           │   └── markdown.js  # Hive markdown rendering, sanitization, video embeds
│   │           └── discover/    # 13 focused JS modules
│   │               ├── state.js, filters.js, rendering.js, voting.js,
│   │               ├── social.js, comments.js, modal.js, auth.js, report.js,
│   │               └── preferences.js, communities.js, editor.js, location.js, notifications.js
│   ├── db/
│   │   ├── models.py      # ORM models (Post, Category, etc.)
│   │   ├── session.py     # async engine + session
│   │   └── crud.py        # all DB operations (batch-optimized)
│   └── worker/
│       ├── hive.py        # entry point shim
│       ├── main.py        # orchestrator, signal handling
│       ├── classify.py    # classification, sentiment, language detection
│       ├── community.py   # community → category mapping
│       ├── stream.py      # live blockchain stream
│       ├── backfill.py    # HAFSQL backfill thread
│       ├── bridge.py      # async DB bridge
│       └── health.py      # heartbeat file for Docker health check
├── alembic/
│   ├── versions/
│   │   └── 001_initial_schema.py  # complete schema (all tables + indexes + pgvector)
│   │   └── 002_post_reports.py       # misclassification reports table
│   └── verify_migration.py        # post-migration table verification
├── scripts/
│   ├── seed_categories.py  # LLM-based centroid computation with stratification
│   └── requirements.txt
├── seeds/                   # centroid JSON files
├── tests/                   # 266 tests
├── prerender/               # Headless Chromium prerender for bot SSR
├── Dockerfile
├── docker-compose.yml
├── goaccess-run.sh          # GoAccess log processing script
└── deploy.sh
```

---

## Troubleshooting

**Posts not being classified** — Seeds not loaded. Run `python scripts/seed_categories.py`.

**Worker restarting** — Check `./deploy.sh logs`. Usually a missing env var or DB not ready.

**Seed script interrupted** — Run `python scripts/seed_categories.py --resume`.

**Weak categories** — Run `python scripts/seed_categories.py --report` to see coverage, then `python scripts/seed_categories.py --resume --stratify`.

**Port 8000 in use** — Change in `docker-compose.yml`: `"8001:8000"`.

**Login fails** — Ensure Hive Keychain extension is installed and unlocked. Login is purely client-side — Keychain must successfully sign the message.

**Comment posting fails** — Ensure you're logged in and have sufficient Hive RC (Resource Credits). The Keychain popup will show the specific error.
