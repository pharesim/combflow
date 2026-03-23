// ── Collapsible filter bar ──
function toggleFiltersBar() {
  document.getElementById('filters-bar').classList.toggle('expanded');
}

// ── Collapsible filter sections ──
function toggleSection(id) {
  const section = document.getElementById(id);
  section.classList.toggle('collapsed');
  const header = section.querySelector('.filter-header');
  header.setAttribute('aria-expanded', !section.classList.contains('collapsed'));
}

// ── Sort ──
function applySort(mode) {
  state.sortMode = mode;
  if (mode === 'newest') {
    state.posts.sort((a, b) => (b.created || '').localeCompare(a.created || ''));
  } else if (mode === 'sentiment-pos') {
    state.posts.sort((a, b) => (b.sentiment_score || 0) - (a.sentiment_score || 0));
  } else if (mode === 'sentiment-neg') {
    state.posts.sort((a, b) => (a.sentiment_score || 0) - (b.sentiment_score || 0));
  }
  renderAll(state.posts, true);
}

function getEffectiveLayout() {
  return window.innerWidth <= 768 ? 'card' : state.layoutMode;
}

// ── Sentiment score to CSS color ──
function sentimentColor(score) {
  const s = Math.max(-1, Math.min(1, score || 0));
  const t = (s + 1) / 2;
  let r, g, b;
  if (t < 0.5) {
    const f = t / 0.5;
    r = Math.round((1 - f) * 231 + f * 100);
    g = Math.round((1 - f) * 46 + f * 120);
    b = Math.round((1 - f) * 59 + f * 110);
  } else {
    const f = (t - 0.5) / 0.5;
    r = Math.round((1 - f) * 100 + f * 46);
    g = Math.round((1 - f) * 120 + f * 204);
    b = Math.round((1 - f) * 110 + f * 113);
  }
  return `rgb(${r},${g},${b})`;
}

function thumbUrl(url, size = 256) {
  if (!url || !url.startsWith('http')) return '';
  // Strip existing hive image proxy prefix to avoid double-wrapping
  const proxyRe = /^https?:\/\/images\.hive\.blog\/\d+x\d+\//;
  const raw = url.replace(proxyRe, '');
  return `https://images.hive.blog/${size}x0/${raw}`;
}

// ── Filter count badges (reads from Alpine store) ──
function updateFilterCounts() {
  const f = Alpine.store('filters');
  const catCount = f.categories.size;
  const sentCount = f.sentiments.size;
  const langCount = f.languages.size;
  const comCount = document.querySelectorAll('#community-chips .chip.active').length;

  setFilterBadge('count-categories', catCount);
  setFilterBadge('count-sentiment', sentCount);
  setFilterBadge('count-languages', langCount);
  setFilterBadge('count-communities', comCount);
}

function setFilterBadge(id, count) {
  const el = document.getElementById(id);
  if (count > 0) {
    el.textContent = count;
    el.classList.add('visible');
  } else {
    el.classList.remove('visible');
  }
}

function updateResultsBar() {
  const bar = document.getElementById('footer-results');
  const f = Alpine.store('filters');
  const hasFilters = f.categories.size > 0 || f.languages.size > 0 || f.sentiments.size > 0
    || state.activeCommunityFilter || state.myCommunitiesActive || state.followingFilterActive
    || state.authorFilterUser;
  const displayTotal = hasFilters ? state.filteredTotalCount : state.totalPostCount;
  const filterLabel = hasFilters ? ' (filtered)' : '';
  bar.textContent = `Showing ${state.posts.length.toLocaleString()} of ${displayTotal.toLocaleString()} posts${filterLabel}`;
}

// ── Check if current filters match saved defaults ──
function checkFiltersMatchDefault() {
  const f = Alpine.store('filters');
  const hasExtra = !!state.activeCommunityFilter || state.myCommunitiesActive
    || state.followingFilterActive || !!state.authorFilterUser;
  const hasAny = f.categories.size > 0 || f.languages.size > 0 || f.sentiments.size > 0 || hasExtra;
  Alpine.store('app').hasActiveFilters = hasAny;
  const cached = localStorage.getItem('honeycomb_filterPrefs');
  if (!cached) {
    Alpine.store('app').filtersMatchDefault = !hasAny;
    return;
  }
  try {
    const d = JSON.parse(cached);
    const defCats = d.default_categories || [];
    const defLangs = d.default_languages || [];
    const defSent = d.default_sentiment ? [d.default_sentiment] : [];
    const catsMatch = f.categories.size === defCats.length && defCats.every(c => f.categories.has(c));
    const langsMatch = f.languages.size === defLangs.length && defLangs.every(l => f.languages.has(l));
    const sentsMatch = f.sentiments.size === defSent.length && defSent.every(s => f.sentiments.has(s));
    Alpine.store('app').filtersMatchDefault = catsMatch && langsMatch && sentsMatch && !hasExtra;
  } catch(e) {
    Alpine.store('app').filtersMatchDefault = false;
  }
}

// ── Session filter persistence ──
function saveSessionFilters() {
  const f = Alpine.store('filters');
  const session = {
    categories: Array.from(f.categories),
    languages: Array.from(f.languages),
    sentiments: Array.from(f.sentiments),
    community: state.activeCommunityFilter,
    myCommunities: state.myCommunitiesActive,
    following: state.followingFilterActive,
  };
  sessionStorage.setItem('honeycomb_sessionFilters', JSON.stringify(session));
}

function loadSessionFilters() {
  const raw = sessionStorage.getItem('honeycomb_sessionFilters');
  if (!raw) return false;
  try {
    const s = JSON.parse(raw);
    const f = Alpine.store('filters');
    if (s.categories?.length) f.setAll('categories', s.categories);
    if (s.languages?.length) f.setAll('languages', s.languages);
    if (s.sentiments?.length) f.setAll('sentiments', s.sentiments);
    if (s.community) state.activeCommunityFilter = s.community;
    if (s.myCommunities) setMyCommunitiesActive(true);
    if (s.following) setFollowingActive(true);
    syncCommunityChips();
    updateFilterCounts();
    return true;
  } catch(e) { return false; }
}

// ── Debounced filter trigger ──
function scheduleFilter() {
  clearTimeout(filterTimer);
  updateFilterCounts();
  filterTimer = setTimeout(applyFilters, 150);
  scheduleSuggestions();
  saveSessionFilters();
  checkFiltersMatchDefault();
}

// ── Build filter URL (reads from Alpine store) ──
function buildFilterUrl(limit, offset) {
  const f = Alpine.store('filters');
  const cats = Array.from(f.categories);
  const sentiments = Array.from(f.sentiments);
  const langs = Array.from(f.languages);
  let url = `/api/browse?limit=${limit}&offset=${offset}`;
  cats.forEach(c => url += `&category=${encodeURIComponent(c)}`);
  langs.forEach(l => url += `&language=${encodeURIComponent(l)}`);
  if (state.authorFilterUser) {
    url += `&authors=${encodeURIComponent(state.authorFilterUser)}`;
  } else if (state.followingFilterActive && state.followedUsers.size > 0) {
    state.followedUsers.forEach(u => url += `&authors=${encodeURIComponent(u)}`);
  } else if (state.myCommunitiesActive && state.userCommunities && state.userCommunities.length > 0) {
    state.userCommunities.forEach(c => url += `&communities=${encodeURIComponent(c.id)}`);
  } else if (state.activeCommunityFilter) {
    url += `&community=${encodeURIComponent(state.activeCommunityFilter)}`;
  }
  // Sentiment filter (exclude nsfw pseudo-sentiment from the server param)
  const realSentiments = sentiments.filter(s => s !== 'nsfw');
  if (realSentiments.length === 1) url += `&sentiment=${encodeURIComponent(realSentiments[0])}`;
  // NSFW mode: hide (default) / show / filter
  const nsfwMode = getNsfwMode();
  if (nsfwMode === 'show' || nsfwMode === 'filter') url += '&include_nsfw=true';
  if (nsfwMode === 'filter' && sentiments.includes('nsfw')) url += '&nsfw_only=true';
  return { url, sentiments: realSentiments };
}

// ── Filters ──
function clearFilters() {
  // Clear Alpine store (triggers effect -> syncAllChipsDom + scheduleFilter)
  Alpine.store('filters').clear();
  // Clear community/author/following state
  state.activeCommunityFilter = null;
  clearAuthorFilter();
  setMyCommunitiesActive(false);
  setFollowingActive(false);
  syncCommunityChips();
  updateSuggestionActiveState();
  updateFilterCounts();
  document.getElementById('suggestions-bar').style.display = 'none';
  // Clear session filters so cleared state persists across navigation
  sessionStorage.removeItem('honeycomb_sessionFilters');
  // Apply immediately (don't wait for effect's debounce)
  clearTimeout(filterTimer);
  applyFilters();
}

function resetFilters() {
  // Clear current filters
  Alpine.store('filters').clear();
  state.activeCommunityFilter = null;
  clearAuthorFilter();
  setMyCommunitiesActive(false);
  setFollowingActive(false);
  syncCommunityChips();
  updateSuggestionActiveState();
  document.getElementById('suggestions-bar').style.display = 'none';
  // Re-apply saved preferences (does not save on-chain)
  const cached = localStorage.getItem('honeycomb_filterPrefs');
  if (cached) {
    try {
      applyPreferenceFilters(JSON.parse(cached));
    } catch(e) {}
  }
  updateFilterCounts();
  // Update session to match restored defaults
  saveSessionFilters();
  clearTimeout(filterTimer);
  applyFilters();
}

// ── Sync DOM chip classes from Alpine store ──
function syncAllChipsDom() {
  const f = Alpine.store('filters');
  // Category chips
  document.querySelectorAll('#cat-chips .chip').forEach(c => {
    if (c.classList.contains('cat-parent')) {
      // Parent active if all children active
      const parentName = c.dataset.cat;
      const siblings = document.querySelectorAll(`#cat-chips .chip[data-parent="${parentName}"]`);
      const allActive = siblings.length > 0 && Array.from(siblings).every(s => f.categories.has(s.dataset.cat));
      c.classList.toggle('active', allActive);
      c.setAttribute('aria-pressed', String(allActive));
    } else {
      const active = f.categories.has(c.dataset.cat);
      c.classList.toggle('active', active);
      c.setAttribute('aria-pressed', String(active));
    }
  });
  // Language chips
  document.querySelectorAll('#lang-chips .chip').forEach(c => {
    const active = f.languages.has(c.dataset.lang);
    c.classList.toggle('active', active);
    c.setAttribute('aria-pressed', String(active));
  });
  // Sentiment chips
  document.querySelectorAll('#sentiment-chips .chip').forEach(c => {
    const active = f.sentiments.has(c.dataset.sentiment);
    c.classList.toggle('active', active);
    c.setAttribute('aria-pressed', String(active));
  });
}

async function applyFilters() {
  if (fetchAbort) fetchAbort.abort();
  fetchAbort = new AbortController();

  state.currentOffset = 0;
  state.noMorePosts = false;
  state.lastCursor = null;

  const { url, sentiments } = buildFilterUrl(PAGE_SIZE, 0);

  try {
    const res = await fetch(url, {signal: fetchAbort.signal});
    const data = await res.json();
    state.posts = data.posts || [];
    state.filteredTotalCount = data.total || 0;
    const serverCount = state.posts.length;
    if (sentiments.length > 1) {
      state.posts = state.posts.filter(p => sentiments.includes(p.sentiment));
    }
    state.posts = filterMutedPosts(state.posts);
    state.currentOffset = state.posts.length;
    state.lastCursor = data.next_cursor || null;
    state.noMorePosts = serverCount < PAGE_SIZE;
    seedMetaFromServer(state.posts);
    renderAll(state.posts, true);
    updateResultsBar();
    fetchMeta(state.posts);
  } catch(e) {
    if (e.name !== 'AbortError') console.error(e);
  }
}

// ── Alpine.effect(): react to filter store changes ──
// Gated by _filterEffectReady (set true after init completes chip creation + preferences).
// This prevents premature filtering before the DOM and data are ready.
let _filterEffectReady = false;

function enableFilterEffect() {
  _filterEffectReady = true;
}

// Hook into alpine:init to register the effect after Alpine is ready
document.addEventListener('alpine:init', () => {
  // Use queueMicrotask so the stores are registered first
  queueMicrotask(() => {
    Alpine.effect(() => {
      // Touch the reactive revision to subscribe to changes
      const f = Alpine.store('filters');
      void f._rev;
      void f.categories.size;
      void f.languages.size;
      void f.sentiments.size;
      // Only act after init is done
      if (!_filterEffectReady) return;
      // Sync DOM chip appearances and schedule filter
      syncAllChipsDom();
      scheduleFilter();
    });
  });
});
