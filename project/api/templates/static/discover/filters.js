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
  const curationLabel = state.curationMode && state.curationFilteredCount > 0
    ? ` (${state.curationFilteredCount} hidden by curation filters)` : '';
  bar.textContent = `Showing ${state.posts.length.toLocaleString()} of ${displayTotal.toLocaleString()} posts${filterLabel}${curationLabel}`;
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
    syncAllChipsDom();
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
  saveCurationSession();
  checkFiltersMatchDefault();
}

// ── Curation mode: age slider steps ──
// Steps 0-23 = 1h through 24h, steps 24-30 = 2d through 7d
const CURATION_AGE_STEPS = [
  '1h','2h','3h','4h','5h','6h','7h','8h','9h','10h','11h','12h',
  '13h','14h','15h','16h','17h','18h','19h','20h','21h','22h','23h','24h',
  '2d','3d','4d','5d','6d','7d','7d'
];
const CURATION_AGE_LABELS = [
  '< 1 hour','< 2 hours','< 3 hours','< 4 hours','< 5 hours','< 6 hours',
  '< 7 hours','< 8 hours','< 9 hours','< 10 hours','< 11 hours','< 12 hours',
  '< 13 hours','< 14 hours','< 15 hours','< 16 hours','< 17 hours','< 18 hours',
  '< 19 hours','< 20 hours','< 21 hours','< 22 hours','< 23 hours','< 1 day',
  '< 2 days','< 3 days','< 4 days','< 5 days','< 6 days','< 7 days','< 7 days'
];

function getCurationAgeValue(step) { return CURATION_AGE_STEPS[step] || '7d'; }
function getCurationAgeLabel(step) { return CURATION_AGE_LABELS[step] || '< 7 days'; }

// ── Curation mode: toggle ──
function setCurationMode(enabled) {
  state.curationMode = enabled;
  localStorage.setItem('honeycomb_curationMode', enabled);
  // Show/hide the collapsible curation section in the filter sidebar
  const section = document.getElementById('filter-curation');
  if (section) section.style.display = enabled ? '' : 'none';
  // Sync settings modal checkbox if open
  const settingsCb = document.getElementById('settings-curation-mode');
  if (settingsCb) settingsCb.checked = enabled;
  scheduleFilter();
}

function initCurationUI() {
  // Restore state from sessionStorage
  const raw = sessionStorage.getItem('honeycomb_curationFilters');
  if (raw) {
    try {
      const s = JSON.parse(raw);
      if (s.maxAge) state.curationMaxAge = s.maxAge;
      if (s.votes != null) state.curationVotes = s.votes;
      if (s.maxPayout != null) state.curationMaxPayout = s.maxPayout;
      if (s.sort) state.curationSort = s.sort;
    } catch(e) {}
  }
  // Set UI controls to match state
  const slider = document.getElementById('curation-age-slider');
  const label = document.getElementById('curation-age-label');
  if (slider) {
    const idx = CURATION_AGE_STEPS.indexOf(state.curationMaxAge);
    slider.value = idx >= 0 ? idx : 30;
    if (label) label.textContent = getCurationAgeLabel(Number(slider.value));
  }
  const voteSel = document.getElementById('curation-votes');
  if (voteSel) voteSel.value = state.curationVotes;
  const payoutIn = document.getElementById('curation-payout');
  if (payoutIn) payoutIn.value = state.curationMaxPayout;
  const sortSel = document.getElementById('curation-sort');
  if (sortSel) sortSel.value = state.curationSort;
  // Show curation section only if curation mode is active
  const section = document.getElementById('filter-curation');
  if (section) section.style.display = state.curationMode ? '' : 'none';
}

function saveCurationSession() {
  sessionStorage.setItem('honeycomb_curationFilters', JSON.stringify({
    maxAge: state.curationMaxAge,
    votes: state.curationVotes,
    maxPayout: state.curationMaxPayout,
    sort: state.curationSort,
  }));
}

// ── Curation mode: client-side filtering (votes + payout) ──
function applyCurationFilters(posts) {
  if (!state.curationMode) { state.curationFilteredCount = 0; return posts; }
  const before = posts.length;
  let filtered = posts;
  // Vote count filter (< threshold)
  if (state.curationVotes) {
    const maxVotes = parseInt(state.curationVotes);
    if (!isNaN(maxVotes)) {
      filtered = filtered.filter(p => {
        const key = `${p.author}/${p.permlink}`;
        const meta = state.metaCache[key];
        if (!meta || meta.votes == null) return true; // keep if meta not loaded yet
        return meta.votes < maxVotes;
      });
    }
  }
  // Payout filter
  if (state.curationMaxPayout !== '' && state.curationMaxPayout != null) {
    const maxP = parseFloat(state.curationMaxPayout);
    if (!isNaN(maxP) && maxP >= 0) {
      filtered = filtered.filter(p => {
        const key = `${p.author}/${p.permlink}`;
        const meta = state.metaCache[key];
        if (!meta || meta.payout == null) return true; // keep if meta not loaded yet
        return meta.payout < maxP;
      });
    }
  }
  state.curationFilteredCount = before - filtered.length;
  return filtered;
}

// ── Gather current filter params (shared by GET and POST paths) ──
function _gatherFilterParams(limit, offset) {
  const f = Alpine.store('filters');
  const cats = Array.from(f.categories);
  const sentiments = Array.from(f.sentiments);
  const langs = Array.from(f.languages);
  const realSentiments = sentiments.filter(s => s !== 'nsfw');
  const nsfwMode = getNsfwMode();

  const params = { limit, offset };
  if (cats.length) params.category = cats;
  if (langs.length) params.language = langs;

  // Author / community filters (mutually exclusive)
  let authors = null;
  if (state.authorFilterUser) {
    authors = [state.authorFilterUser];
  } else if (state.followingFilterActive && state.followedUsers.size > 0) {
    authors = Array.from(state.followedUsers);
  }
  if (authors) {
    params.authors = authors;
  } else if (state.myCommunitiesActive && state.userCommunities && state.userCommunities.length > 0) {
    params.communities = state.userCommunities.map(c => c.id);
  } else if (state.activeCommunityFilter) {
    params.community = state.activeCommunityFilter;
  }

  if (realSentiments.length === 1) params.sentiment = realSentiments[0];
  if (nsfwMode === 'show' || nsfwMode === 'filter') params.include_nsfw = true;
  if (nsfwMode === 'filter' && sentiments.includes('nsfw')) params.nsfw_only = true;

  if (state.curationMode) {
    if (state.curationMaxAge && state.curationMaxAge !== '7d') params.max_age = state.curationMaxAge;
    if (state.curationSort && state.curationSort !== 'newest') params.sort = state.curationSort;
  }

  return { params, sentiments: realSentiments };
}

// ── Build filter URL (reads from Alpine store) ──
function buildFilterUrl(limit, offset) {
  const { params, sentiments } = _gatherFilterParams(limit, offset);
  let url = `/api/browse?limit=${params.limit}&offset=${params.offset}`;
  (params.category || []).forEach(c => url += `&category=${encodeURIComponent(c)}`);
  (params.language || []).forEach(l => url += `&language=${encodeURIComponent(l)}`);
  (params.authors || []).forEach(u => url += `&authors=${encodeURIComponent(u)}`);
  (params.communities || []).forEach(c => url += `&communities=${encodeURIComponent(c)}`);
  if (params.community) url += `&community=${encodeURIComponent(params.community)}`;
  if (params.sentiment) url += `&sentiment=${encodeURIComponent(params.sentiment)}`;
  if (params.include_nsfw) url += '&include_nsfw=true';
  if (params.nsfw_only) url += '&nsfw_only=true';
  if (params.max_age) url += `&max_age=${encodeURIComponent(params.max_age)}`;
  if (params.sort) url += `&sort=${encodeURIComponent(params.sort)}`;
  return { url, sentiments };
}

// ── Fetch browse results, using POST when authors list is large ──
async function browseFetch(limit, offset, cursor, signal) {
  const { params, sentiments } = _gatherFilterParams(limit, offset);
  const usePost = params.authors && params.authors.length > 50;

  let data;
  if (usePost) {
    const body = Object.assign({}, params);
    if (cursor) body.cursor = cursor;
    const res = await fetch('/api/browse', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
      signal,
    });
    data = await res.json();
  } else {
    const { url } = buildFilterUrl(limit, offset);
    const fetchUrl = cursor ? url + `&cursor=${encodeURIComponent(cursor)}` : url;
    const res = await fetch(fetchUrl, { signal });
    data = await res.json();
  }
  return { data, sentiments };
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
  // Reset curation filter values (keep mode toggle as-is)
  state.curationMaxAge = '7d';
  state.curationVotes = '';
  state.curationMaxPayout = '';
  state.curationSort = 'newest';
  const ageSlider = document.getElementById('curation-age-slider');
  if (ageSlider) { ageSlider.value = 30; document.getElementById('curation-age-label').textContent = getCurationAgeLabel(30); }
  const voteSel = document.getElementById('curation-votes');
  if (voteSel) voteSel.value = '';
  const payoutIn = document.getElementById('curation-payout');
  if (payoutIn) payoutIn.value = '';
  const sortSel = document.getElementById('curation-sort');
  if (sortSel) sortSel.value = 'newest';
  // Clear session filters so cleared state persists across navigation
  sessionStorage.removeItem('honeycomb_sessionFilters');
  sessionStorage.removeItem('honeycomb_curationFilters');
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

  try {
    const { data, sentiments } = await browseFetch(PAGE_SIZE, 0, null, fetchAbort.signal);
    state.posts = data.posts || [];
    state.filteredTotalCount = data.total || 0;
    const serverCount = state.posts.length;
    if (sentiments.length > 1) {
      state.posts = state.posts.filter(p => sentiments.includes(p.sentiment));
    }
    state.posts = filterMutedPosts(state.posts);
    state.posts = applyCurationFilters(state.posts);
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
