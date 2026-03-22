// ── Centralized application state ──
const state = {
  posts: [],
  metaCache: {},
  metaCacheKeys: [],
  totalPostCount: 0,
  filteredTotalCount: 0,
  communityList: [],
  activeCommunityFilter: null,
  myCommunitiesActive: false,
  userCommunities: null,
  votedPosts: {},
  manaCache: null,
  mutedUsers: new Set(),
  followedUsers: new Set(),
  followingFilterActive: false,
  myPostsActive: false,
  authorFilterUser: null,
  currentOffset: 0,
  loadingMore: false,
  noMorePosts: false,
  lastCursor: null,
  layoutMode: window.innerWidth <= 768 ? 'card' : (localStorage.getItem('combflow_layout') || 'hex'),
  sortMode: 'newest',
  deepLinked: false,
  newestCreated: null,
};

// Constants
const META_CACHE_MAX = 1200;
const ALL_POSTS_MAX = 1000;
const MANA_CACHE_TTL = 60000;
const MUTED_KEY = 'honeycomb_muted';
const FOLLOWED_KEY = 'honeycomb_followed';
const PAGE_SIZE = 60;

// Transient (not semantic state)
let fetchAbort = null;
let filterTimer = null;

// Hive RPC nodes with automatic fallback
const HIVE_NODES = ['https://api.hive.blog', 'https://techcoderx.com', 'https://api.openhive.network'];
const _nodePenalties = new Map(); // node -> failure count

function _sortedNodes() {
  return [...HIVE_NODES].sort((a, b) => (_nodePenalties.get(a) || 0) - (_nodePenalties.get(b) || 0));
}

const PROXY_DOMAINS = /(?:files\.peakd\.com|images\.ecency\.com|images\.hive\.blog|cdn\.steemitimages\.com|steemitimages\.com|usermedia\.actifit\.io|imgur\.com|i\.imgur\.com|blurt\.media)/i;
async function hiveRpc(method, params) {
  for (const node of _sortedNodes()) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 4000);
      const res = await fetch(node, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({jsonrpc:'2.0', method, params, id:1}),
        signal: controller.signal
      });
      clearTimeout(timer);
      if (!res.ok) { _nodePenalties.set(node, (_nodePenalties.get(node) || 0) + 1); continue; }
      const data = await res.json();
      if ('result' in data) { _nodePenalties.delete(node); return data.result; }
      _nodePenalties.set(node, (_nodePenalties.get(node) || 0) + 1);
    } catch(e) {
      _nodePenalties.set(node, (_nodePenalties.get(node) || 0) + 1);
    }
  }
  return null;
}

// Vote button SVG constants
const VOTE_SVG_18 = '<svg viewBox="0 0 24 24" width="18" height="18"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>';
const VOTE_SVG_16 = '<svg viewBox="0 0 24 24" width="16" height="16"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>';

// Lazy thumbnail observer
// ── Alpine.js store ──
// Registered before Alpine auto-initializes (Alpine script loaded after modules).
// The store holds reactive state that drives x-show/x-for/x-text in templates.
// Non-reactive state (filter params, pagination cursors, etc.) stays in `state`.
document.addEventListener('alpine:init', () => {
  Alpine.store('app', {
    // Reactive post list + metadata
    posts: [],
    initialLoaded: false,
    metaRev: 0,
    // Hex layout geometry
    hexPositions: [],
    hexGridW: 0,
    hexGridH: 0,
    // Layout
    layoutMode: window.innerWidth <= 768 ? 'card' : (localStorage.getItem('combflow_layout') || 'hex'),
    // UI state (modal visibility)
    loginOpen: false,
    signupOpen: false,
    settingsOpen: false,
    editorOpen: false,
    locationOpen: false,
    mdHelpOpen: false,
    modalOpen: false,
    votePopupOpen: false,
    authDropdownOpen: false,
    // Auth — initialize from localStorage so Alpine template renders immediately
    currentUser: (() => { const u = localStorage.getItem('honeycomb_user'), e = localStorage.getItem('honeycomb_expires'); return (u && e && new Date(e).getTime() > Date.now()) ? u : null; })(),
    // Comments
    comments: [],
    commentCount: 0,
    hiddenCount: 0,
    commentLoading: false,
    commentError: false,
    commentPostAuthor: '',
    commentPostPermlink: '',
    // Editor
    editorTags: [],
    editorTab: 'write',
  });

  // ── Filters store ──
  // Reactive Sets for each filter dimension + toggle helper.
  // Alpine proxies don't track Set mutations, so we bump a revision counter
  // after every mutation so Alpine.effect() re-runs.
  Alpine.store('filters', {
    _rev: 0,
    _categories: new Set(),
    _languages: new Set(),
    _sentiments: new Set(),

    get categories() { void this._rev; return this._categories; },
    get languages() { void this._rev; return this._languages; },
    get sentiments() { void this._rev; return this._sentiments; },

    toggle(dimension, value) {
      const set = this['_' + dimension];
      if (!set) return;
      if (set.has(value)) set.delete(value);
      else set.add(value);
      this._rev++;
    },

    add(dimension, value) {
      const set = this['_' + dimension];
      if (!set || set.has(value)) return;
      set.add(value);
      this._rev++;
    },

    remove(dimension, value) {
      const set = this['_' + dimension];
      if (!set || !set.has(value)) return;
      set.delete(value);
      this._rev++;
    },

    has(dimension, value) {
      void this._rev;
      return this['_' + dimension].has(value);
    },

    clear() {
      this._categories.clear();
      this._languages.clear();
      this._sentiments.clear();
      this._rev++;
    },

    // Batch-set: replace entire set contents (used by preferences)
    setAll(dimension, values) {
      const set = this['_' + dimension];
      if (!set) return;
      set.clear();
      (values || []).forEach(v => set.add(v));
      this._rev++;
    },
  });
});

const thumbObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      const el = entry.target;
      const url = el.dataset.thumb;
      if (url) {
        el.style.backgroundImage = `url('${safeCssUrl(url)}')`;
        delete el.dataset.thumb;
      }
      thumbObserver.unobserve(el);
    }
  });
}, { rootMargin: '200px' });
