# CombFlow

> Semantic post discovery for the Hive blockchain — streams live, backfills history, classifies with AI.

CombFlow listens to the Hive blockchain, classifies posts by meaning (not just keywords), detects multiple languages and sentiment, and stores them for exploration. The **HoneyComb** discovery UI lets users browse and filter posts in a hex grid, read threaded comment discussions, post comments and top-level posts, discover and join Hive communities, and post to communities — all via Hive Keychain without leaving the app.

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
2. **Worker** streams blocks from Hive (live) and walks backwards through HAFSQL (backfill). For each post: embeds the body in-process (`all-MiniLM-L6-v2`), compares against centroids, detects languages (`langdetect` + `json_metadata`), analyses sentiment (embedding-based), auto-maps Hive communities to categories (embedding community title+about, +0.08 boost), and saves everything directly to PostgreSQL.
3. **HoneyComb UI** shows posts in a honeycomb hex grid built with **Alpine.js** (~17KB, CDN-loaded, no build step) for reactive rendering. Collapsible chip-based filters (category, sentiment, language, author), sticky filter bar, endless scrolling, sort toggle, lazy thumbnails, visibility-aware live polling, and toast notifications. WCAG AA accessible with full keyboard navigation. Three layout modes (hex grid, card grid, list) rendered via Alpine `x-for` loops. Embeds YouTube, 3Speak, and Instagram Reel videos. Hierarchical comment trees loaded directly from the Hive chain. Community discovery suggestions bar with subscribe/unsubscribe via Keychain. Open Graph meta tags for social media previews. Cross-post detection and thumbnail support (PeakD `cross_post_key` and Ecency `original_author`/`original_permlink` formats).
4. **Hive Keychain auth** — users log in with their Hive account via Keychain browser extension. JWT is stored in an httpOnly cookie. Accounts with negative reputation are blocked at login. Logged-in users can save default filter preferences on-chain (via `posting_json_metadata`), post comments and replies, author new top-level posts (to their blog or a community, with optional cross-post), follow/unfollow users, and join/leave communities — all broadcast client-side via Keychain (no private keys touch the server).

### Category hierarchy (2 levels)

**9 parents, 43 leaf categories:**

| Parent | Leaves |
|--------|--------|
| technology | crypto, programming, ai, cybersecurity, gaming |
| creative | photography, art, music, writing, video, diy-crafts |
| lifestyle | travel, food, fashion, home-garden, parenting, pets |
| science-education | nature, science, education, health, psychology |
| society | politics, philosophy, history, religion, social-issues |
| finance-business | finance, economics, entrepreneurship |
| entertainment | movies-tv, anime-manga, books |
| sports | team-sports, combat-sports, motorsports, outdoor-sports, fitness |
| community | hive, introductions, contests, charity, local-communities |

Classification happens at the leaf level. Filtering by a parent covers all its children.

---

## Quick start

### 1. Configure

```bash
cp .env.example .env
# Edit .env — at minimum set:
#   POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
#   DATABASE_URL  (must match the postgres vars)
#   API_KEY       (any secret string — also used as JWT signing key)
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
| `caddy` | Reverse proxy with auto-TLS, domain routing, API→docs redirect (128M memory limit) |

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

## HoneyComb UI

Visit **http://localhost:8000/ui** to browse posts in a honeycomb grid.

- **Filters** — collapsible sections for categories (parent toggles all children), sentiment (positive/neutral/negative chips), and languages (multi-select chips)
- **Filtered total** — results bar shows "Showing X of Y posts" where Y reflects active filters
- **Dynamic** — filters apply instantly with 150ms debounce, no page reload
- **Endless scrolling** — more posts load automatically as you scroll down
- **Sort toggle** — sort by newest or by most recent classification
- **Read tracking** — opened posts are dimmed so you can see what's new
- **Post modal** — click a hex to see full content, categories, languages, and sentiment (body scroll locked while open)
- **Comment threads** — hierarchical comments loaded from HAFSQL, reputation-filtered (rep <= 0 hidden), collapsible nested replies, includes author's own replies
- **Comment posting** — logged-in users can post comments and replies via Hive Keychain, with 3-second cooldown and cache invalidation
- **Post authoring** — pen icon opens a full editor with title, preview description (120 chars, stored in `json_metadata.description`), markdown body with formatting toolbar (bold, italic, headings, links, images, lists, quotes, code blocks, tables, center, @mentions — plus Ctrl+B/I/K shortcuts), image upload via clipboard paste, drag-and-drop, or file picker (uploaded to Hive image hosting via Keychain-signed requests), markdown help modal, tag autocomplete from categories, community selector (blog vs joined communities), cross-post toggle, 100% Power Up default, and localStorage draft auto-save
- **Location picker** — map button in the editor opens a Leaflet/OpenStreetMap modal; click to place a pin or use "My Location" (browser geolocation). Reverse geocoding via Nominatim auto-fills the location name. Inserts a worldmappin-compatible hidden tag in the post body
- **Engagement stats** — card and list views show vote count and comment count next to each post (fetched from the Hive chain). Voting updates the count instantly with a visual bump animation.
- **Upvoting** — heart button on posts (card, list, and modal views) with live vote count. Dynamic vote weight adjusts automatically based on voting mana with configurable floor (default 50%) and max weight (default 25%) — users never run out of votes. Settings saved on-chain.
- **Follow users** — follow/unfollow button in post modal broadcasts Hive-native follow via Keychain. "Following" toggle in the filter bar shows only followed users' posts (active by default after login). Followed list synced from chain on login, cached in localStorage. Manage followed users in settings modal.
- **Mute users** — mute button in post modal broadcasts Hive-native mute via Keychain. Muted users' posts are hidden client-side. Unmute available in settings. Muted list synced from chain on login.
- **Notifications** — bell icon in the header shows unread Hive notifications (replies, mentions, votes, reblogs, follows, etc.) fetched from the Hive Bridge API. Unread count badge updates on login and periodically. Click to expand a dropdown of recent notifications with relative timestamps. Mark all as read via Keychain (writes last-read timestamp to `posting_json_metadata`). Notifications link to the relevant post or author profile.
- **Light/dark theme** — sun/moon toggle in the header switches between dark and light mode. Preference saved in localStorage (per-browser), respects system `prefers-color-scheme` by default.
- **Settings modal** — first-login setup for default filters (languages, categories, sentiment). Vote settings (manual mode, mana floor, max weight) tucked under a collapsible "Advanced" section. Muted and followed users shown in a tabbed panel (only visible when you have muted or followed accounts). Preferences saved on-chain.
- **Profile avatars** — Hive profile pictures shown next to usernames in header, cards, list rows, and post modal
- **Community browsing** — community badges on posts (clickable to filter), community filter chips in sidebar, community info in post modal with hivel.ink link
- **Community discovery** — suggestions bar shows related communities when category filters are active; logged-in users can join/leave communities directly via Keychain
- **My Communities filter** — logged-in users can toggle a "My Communities" filter to show only posts from communities they've joined on Hive
- **Lazy thumbnails** — loaded on-demand as hexes enter the viewport
- **Sentiment borders** — each hex has a coloured border from red (negative) to green (positive)
- **Layout toggle** — switch between hex grid and card view (auto-selects cards on mobile)
- **Live polling** — visibility-aware, only polls when the tab is active
- **Toast notifications** — non-blocking feedback for saves, errors, etc.
- **Author profile URLs** — `/@username` shows posts filtered by that author; also triggered by clicking any username

- **Keyboard navigation** — arrow keys, J/K to navigate posts, Enter/Space to open, H to vote, C to comment
- **Cross-post URL support** — Hive-style prefixed URLs (`/community/@author/permlink`) redirect to canonical deep links
- **Social previews** — Open Graph meta tags on post deep links for rich previews on Discord, Twitter, etc.
- **Security** — CSP headers, SRI hashes on CDN resources, input validation, clickjacking protection, robust XSS-safe post rendering (raw-text tag stripping, unclosed iframe handling)
- **Accessibility** — WCAG AA: focus management, ARIA labels, keyboard navigation, colour contrast

---

## Authentication

Users log in with **Hive Keychain** (browser extension):

1. UI requests a challenge from `POST /api/auth/challenge`
2. Keychain signs the challenge with the user's Posting key
3. Backend verifies the signature against the on-chain public key via `POST /api/auth/verify`
4. Accounts with reputation < 0 are rejected (403)
5. JWT is set as an httpOnly cookie scoped to `/api`
6. Logout clears the cookie via `POST /api/auth/logout`

**Persistent preferences** — logged-in users can save default category, language, and sentiment filters on-chain in their Hive account's `posting_json_metadata` (under the `combflow` namespace). These are restored automatically on next visit and follow the user across devices.

**Detailed error messages** — auth failures surface specific messages (reputation too low, rate limited, service unavailable) instead of generic errors.

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

- **Detection**: combines `json_metadata` app-provided language + `langdetect` probabilistic detection (threshold 0.25)
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

Per-post pipeline:
- Check author reputation via HAFSQL (>= 20)
- Clean post body (strip markdown images, links, HTML, URLs) via shared `project/text.py`
- Reject posts with < 80 chars of meaningful text
- Classify against category centroids (sentence-transformers)
- Auto-map Hive communities to categories (fetch title+about via Hive API `bridge.get_community`, embed, 0.40 threshold, +0.08 boost)
- Detect languages (langdetect + json_metadata)
- Compute sentiment via embedding similarity
- Save classification + community mapping to PostgreSQL

No HTTP calls to the CombFlow API — the worker talks only to Hive nodes, HAFSQL, and its own PostgreSQL.

---

## Database

### Schema

| Table | Purpose |
|-------|---------|
| `posts` | Hive posts (author, permlink, created, sentiment, sentiment_score, community_id) |
| `categories` | 2-level hierarchy (parent_id for nesting) |
| `post_category` | Many-to-many: posts <-> categories |
| `post_language` | Many-to-many: posts <-> languages |
| `category_centroids` | 384-dim pgvector centroids + HNSW index |
| `stream_cursors` | Per-worker last-processed block |
| `community_mappings` | Auto-mapped community→category associations (worker-maintained) |

### Indexes

- `ix_post_category_post_id_category_id` — composite on post_category
- `ix_post_category_category_id` — category lookups
- `ix_post_language_post_id` — post language lookups
- `ix_post_language_language` — language filter queries
- `ix_posts_community_id` — community filter queries
- `ix_community_mappings_category_slug` — community suggestion queries
- `ix_posts_created_desc` — descending date sort

### Migration

Migrations: `001_initial_schema.py` (all tables, indexes, pgvector), `002_add_title.py` (title column), `003_drop_thumbnail_url.py` (thumbnails fetched client-side), `004_drop_title.py` (title fetched client-side). Verified by `alembic/verify_migration.py` on startup.

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

250 tests across 12 files:

| File | Tests | Coverage |
|------|-------|----------|
| `test_worker_utils.py` | 59 | Classification, sentiment, language detection, community resolution + boost + persistence, pipeline end-to-end, text cleaning |
| `test_browse.py` | 42 | Browse with all filter combinations, single + multi community filter, authors filter, pagination edge cases, communities endpoint, suggested communities, cache TTL |
| `test_hafsql.py` | 33 | Reputation conversion, comment fetching, community metadata parsing, connection pool, cursor lifecycle |
| `test_auth.py` | 24 | Challenge flow, JWT verify, neg-rep block, error messages, rate limit boundaries, deps edge cases |
| `test_api.py` | 21 | Health, categories, HTML page routes, GZip middleware, auth key enforcement, schema validation, 404s |
| `test_internal.py` | 19 | Internal API endpoints (centroids, stream cursors) |
| `test_comments.py` | 19 | Hierarchical comment tree, multi-level nesting, orphaned comments, reputation filtering, cache invalidation, rate limit cleanup |
| `test_schemas.py` | 15 | Pydantic model validation |
| `test_crud.py` | 10 | Retry decorator, category tree, seed idempotency |
| `test_text.py` | 9 | Text cleaning utilities |
| `test_posts.py` | 5 | Create, upsert, detail |
| `test_cache.py` | 5 | TTL cache operations |

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
| GET | `/api/browse` | Browse posts (query: `category`, `language`, `sentiment`, `community`, `communities`, `authors`, `limit`, `offset`) |
| GET | `/api/languages` | Available languages with post counts |
| GET | `/api/stats` | Overview statistics |
| POST | `/api/auth/challenge` | Generate a Keychain login challenge |
| POST | `/api/auth/verify` | Verify Keychain signature, block neg-rep, set JWT cookie |
| POST | `/api/auth/logout` | Clear JWT cookie |
| GET | `/api/posts/{author}/{permlink}/comments` | Hierarchical comment tree (rep-filtered, depth limit, cached 120s) |
| GET | `/api/communities` | Communities with post counts, names, and categories |
| GET | `/api/communities/suggested` | Suggested communities for given category filters (cached 300s) |

### Authenticated (JWT cookie or Authorization header)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/auth/me` | Current user info |
| DELETE | `/api/posts/{author}/{permlink}/comments/cache` | Invalidate comment cache (rate-limited) |

### Internal (X-API-Key header)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/posts` | Ingest a classified post |
| POST | `/internal/centroids` | Upload category centroids |
| GET/PUT | `/internal/stream-cursor/{key}` | Read/update stream position |

---

## Using the API from your own app

The API is public and CORS-open — you can call it from any frontend, mobile app, or script.

### Browsing posts (no auth needed)

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

# Paginate with cursor (from previous response's next_cursor)
curl 'https://your-server:8000/api/browse?cursor=1711234567.0_4821'

# Get available languages
curl https://your-server:8000/api/languages

# Get category tree
curl https://your-server:8000/categories

# Get a specific post
curl https://your-server:8000/posts/alice/my-post-permlink
```

### Authenticating

Authentication uses Hive Keychain's challenge-response flow. Any Hive library that can sign with a posting key works — you don't need the browser extension.

**Step 1 — Get a challenge:**

```bash
curl -X POST https://your-server:8000/api/auth/challenge \
  -H 'Content-Type: application/json' \
  -d '{"username": "yourhiveuser"}'
# → {"challenge": "abc123...", "expires_in": 300}
```

**Step 2 — Sign the challenge** with the user's Hive posting key. The signature is a recoverable ECDSA signature (secp256k1) over SHA-256 of the challenge string — the same format Hive Keychain's `requestSignBuffer` produces. Any Hive library works (dhive, hivejs, lighthive, nectar, beem).

**Step 3 — Verify and get JWT:**

```bash
curl -X POST https://your-server:8000/api/auth/verify \
  -H 'Content-Type: application/json' \
  -d '{"username": "yourhiveuser", "challenge": "abc123...", "signature": "2055af..."}'
# → {"username": "yourhiveuser", "expires_at": "2026-03-27T..."}
```

The response sets an httpOnly cookie (for same-origin browser use) and returns the JWT in the response. For cross-origin or non-browser clients, extract the JWT from the `Set-Cookie` header or use the cookie value, then pass it as a Bearer token on subsequent requests.

### JavaScript example (cross-origin frontend)

```js
// Browse posts — no auth needed
const res = await fetch('https://your-server:8000/api/browse?category=crypto&limit=20');
const { posts, total, next_cursor } = await res.json();

// Authenticate via Hive Keychain (browser extension)
const { challenge } = await fetch('https://your-server:8000/api/auth/challenge', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: 'yourhiveuser' }),
}).then(r => r.json());

const token = await new Promise((resolve, reject) => {
  window.hive_keychain.requestSignBuffer('yourhiveuser', challenge, 'Posting', async (resp) => {
    if (!resp.success) return reject(new Error('Signing cancelled'));
    const verifyRes = await fetch('https://your-server:8000/api/auth/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'yourhiveuser', challenge, signature: resp.result }),
    });
    const jwt = verifyRes.headers.get('set-cookie')?.match(/honeycomb_jwt=([^;]+)/)?.[1];
    resolve(jwt);
  });
});
```

---

## Environment variables

| Variable | Description | Example |
|----------|-------------|---------|
| `DATABASE_URL` | asyncpg connection string | `postgresql+asyncpg://user:pass@db/combflow` |
| `API_KEY` | Shared secret for internal endpoints + JWT signing | `change-me` |
| `POSTGRES_USER` | Postgres username | `combflow` |
| `POSTGRES_PASSWORD` | Postgres password | `change-me` |
| `POSTGRES_DB` | Postgres database name | `combflow` |
| `CADDY_UI` | UI domain (Caddy auto-TLS when no port) | `honeycomb.example.com` |
| `CADDY_API` | API domain (root redirects to `/docs`) | `api.example.com` |
| `CADDY_UI_OLD` | Previous UI domain — 301 redirects to `CADDY_UI` | `old.example.com` |

---

## Project layout

```
combflow/combflow/
├── project/
│   ├── categories.py     # 2-level category tree (9 parents, 43 leaves)
│   ├── config.py          # pydantic-settings
│   ├── text.py            # shared text cleaning (zero deps, used by worker + seed script)
│   ├── cache.py           # in-process TTL cache
│   ├── hafsql.py          # HAFSQL PostgreSQL client (reputation, backfill, posting keys, comments)
│   ├── api/
│   │   ├── main.py        # FastAPI app, lifespan, OpenAPI config
│   │   ├── deps.py        # JWT auth + DB session dependencies
│   │   ├── schemas.py     # shared Pydantic models
│   │   ├── routes/
│   │   │   ├── auth.py      # Keychain challenge/verify, JWT
│   │   │   ├── posts.py     # POST /posts, GET /posts/{author}/{permlink}, comments tree + cache
│   │   │   ├── ui.py        # HTML pages, browse API
│   │   │   └── internal.py  # centroid upload + stream cursors
│   │   ├── rate_limit.py  # shared sliding-window rate limiter
│   │   └── templates/
│   │       ├── discover.html  # HoneyComb discovery UI (Alpine.js reactive templates)
│   │       └── static/
│   │           ├── shared.js    # auth, validation, read tracking, focus trap, toasts
│   │           ├── shared/      # shared modules
│   │           │   ├── keychain.js  # Keychain broadcasting (vote, comment, post, follow, mute, subscribe)
│   │           │   └── markdown.js  # Hive markdown rendering, sanitization, video embeds
│   │           └── discover/    # 13 focused JS modules
│   │               ├── state.js, filters.js, rendering.js, voting.js,
│   │               ├── social.js, comments.js, modal.js, auth.js,
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
│       └── bridge.py      # async DB bridge
├── alembic/
│   ├── versions/
│   │   ├── 001_initial_schema.py  # all tables + indexes (fresh install)
│   │   ├── 002_add_title.py       # title column on posts
│   │   ├── 003_drop_thumbnail_url.py  # thumbnails now client-side only
│   │   └── 004_drop_title.py          # title now client-side only
│   └── verify_migration.py        # post-migration table verification
├── scripts/
│   ├── seed_categories.py  # LLM-based centroid computation with stratification
│   └── requirements.txt
├── seeds/                   # centroid JSON files
├── tests/                   # 250 tests
├── Dockerfile
├── docker-compose.yml
└── deploy.sh
```

---

## Troubleshooting

**Posts not being classified** — Seeds not loaded. Run `python scripts/seed_categories.py`.

**Worker restarting** — Check `./deploy.sh logs`. Usually a missing env var or DB not ready.

**Seed script interrupted** — Run `python scripts/seed_categories.py --resume`.

**Weak categories** — Run `python scripts/seed_categories.py --report` to see coverage, then `python scripts/seed_categories.py --resume --stratify`.

**Port 8000 in use** — Change in `docker-compose.yml`: `"8001:8000"`.

**Login fails** — Ensure Hive Keychain extension is installed and unlocked. Check the error message in the toast notification — it will say if your reputation is too low (403), you're rate-limited (429), or the auth service is down (503).

**Comment posting fails** — Ensure you're logged in and have sufficient Hive RC (Resource Credits). The Keychain popup will show the specific error.
