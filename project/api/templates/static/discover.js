let allPosts = [];
let metaCache = {};
let metaCacheKeys = [];
const META_CACHE_MAX = 1200;
const ALL_POSTS_MAX = 1000;
let fetchAbort = null;
let filterTimer = null;
let totalPostCount = 0;
let filteredTotalCount = 0;

// Community data
let _communityList = []; // from /api/communities
let _activeCommunityFilter = null; // active community filter (community id)
let _myCommunitiesActive = false; // "My Communities" multi-filter toggle
let _userCommunities = null; // from bridge.list_all_subscriptions (for editor)

// Voting state
let _votedPosts = {}; // { "author/permlink": true } — sessionStorage-backed
let _manaCache = null; // { manaPercent, fetchedAt }
const MANA_CACHE_TTL = 60000; // 60s

// Mute state
let _mutedUsers = new Set(); // localStorage-backed for instant filtering
const MUTED_KEY = 'honeycomb_muted';

// Follow state
let _followedUsers = new Set();
const FOLLOWED_KEY = 'honeycomb_followed';
let _followingFilterActive = false;

// Endless scrolling state
const PAGE_SIZE = 60;

// Hive RPC nodes with automatic fallback
const HIVE_NODES = ['https://api.hive.blog', 'https://api.deathwing.me', 'https://rpc.ausbit.dev'];
const PROXY_DOMAINS = /(?:files\.peakd\.com|images\.ecency\.com|images\.hive\.blog|cdn\.steemitimages\.com|steemitimages\.com|usermedia\.actifit\.io|imgur\.com|i\.imgur\.com|blurt\.media)/i;
async function hiveRpc(method, params) {
  for (const node of HIVE_NODES) {
    try {
      const res = await fetch(node, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({jsonrpc:'2.0', method, params, id:1})
      });
      const data = await res.json();
      if (data.result) return data.result;
    } catch(e) { /* try next node */ }
  }
  return null;
}
let currentOffset = 0;
let loadingMore = false;
let noMorePosts = false;
let _lastCursor = null;

// Layout mode: 'hex' or 'card'
let layoutMode = window.innerWidth <= 768 ? 'card' : (localStorage.getItem('combflow_layout') || 'hex');

// Sort mode
let sortMode = 'newest';

// Deep-link mode: when opened via /@author/permlink, anchor to that post
let _deepLinked = false;

// Lazy thumbnail observer
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

// ── Layout toggle ──
function setLayout(mode) {
  layoutMode = mode;
  localStorage.setItem('combflow_layout', mode);
  document.querySelectorAll('#layout-toggle button').forEach(b => {
    const active = b.dataset.layout === mode;
    b.classList.toggle('active', active);
    b.setAttribute('aria-pressed', active);
  });
  renderAll(allPosts, true);
}

// ── Sort ──
function applySort(mode) {
  sortMode = mode;
  if (mode === 'newest') {
    allPosts.sort((a, b) => (b.created || '').localeCompare(a.created || ''));
  } else if (mode === 'sentiment-pos') {
    allPosts.sort((a, b) => (b.sentiment_score || 0) - (a.sentiment_score || 0));
  } else if (mode === 'sentiment-neg') {
    allPosts.sort((a, b) => (a.sentiment_score || 0) - (b.sentiment_score || 0));
  }
  renderAll(allPosts, true);
}

function getEffectiveLayout() {
  return window.innerWidth <= 768 ? 'card' : layoutMode;
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
  // Only proxy domains known to work with the Hive image proxy
  if (PROXY_DOMAINS.test(raw)) {
    return `https://images.hive.blog/${size}x0/${raw}`;
  }
  return raw;
}

// ── Hex geometry helpers ──
function hexMetrics() {
  const w = window.innerWidth <= 768 ? 120 : 180;
  const gap = 6;
  const h = w * 1.1547;
  return { w, h, gap };
}

// ── Skeletons ──
function showSkeletons() {
  const layout = getEffectiveLayout();
  if (layout === 'card') {
    const cardGrid = document.getElementById('card-grid');
    cardGrid.style.display = '';
    document.getElementById('hex-container').style.display = 'none';
    cardGrid.innerHTML = '';
    for (let i = 0; i < 12; i++) {
      const el = document.createElement('div');
      el.className = 'skeleton skeleton-card';
      cardGrid.appendChild(el);
    }
  } else {
    const hexGrid = document.getElementById('hex-grid');
    const container = document.getElementById('hex-container');
    container.style.display = '';
    document.getElementById('card-grid').style.display = 'none';
    hexGrid.innerHTML = '';
    const { w, h, gap } = hexMetrics();
    const containerW = container.clientWidth - 40;
    const cols = Math.max(2, Math.floor((containerW + gap) / (w + gap)));
    const gridW = cols * (w + gap) - gap + (w / 2 + gap / 2);
    const rowStep = h * 0.75 + gap;
    let idx = 0;
    hexGrid.style.width = gridW + 'px';
    let maxY = 0;
    for (let row = 0; idx < 18; row++) {
      const isOffset = row % 2 === 1;
      const rowCols = isOffset ? cols - 1 : cols;
      for (let col = 0; col < rowCols && idx < 18; col++, idx++) {
        const x = col * (w + gap) + (isOffset ? (w + gap) / 2 : 0);
        const y = row * rowStep;
        const el = document.createElement('div');
        el.className = 'skeleton skeleton-hex';
        el.style.left = x + 'px';
        el.style.top = y + 'px';
        hexGrid.appendChild(el);
        if (y + h > maxY) maxY = y + h;
      }
    }
    hexGrid.style.height = maxY + 'px';
  }
}

// ── Filter count badges ──
function updateFilterCounts() {
  const catCount = document.querySelectorAll('#cat-chips .chip.active:not(.cat-parent)').length;
  const sentCount = document.querySelectorAll('#sentiment-chips .chip.active').length;
  const langCount = document.querySelectorAll('#lang-chips .chip.active').length;

  setFilterBadge('count-categories', catCount);
  setFilterBadge('count-sentiment', sentCount);
  setFilterBadge('count-languages', langCount);
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
  const hasFilters = document.querySelectorAll('.chip.active').length > 0;
  const displayTotal = hasFilters ? filteredTotalCount : totalPostCount;
  const filterLabel = hasFilters ? ' (filtered)' : '';
  bar.textContent = `Showing ${allPosts.length.toLocaleString()} of ${displayTotal.toLocaleString()} posts${filterLabel}`;
}

// ── Voting ──
function loadVotedPosts() {
  try { _votedPosts = JSON.parse(sessionStorage.getItem('honeycomb_voted') || '{}'); } catch(e) { _votedPosts = {}; }
}
function saveVotedPost(key) {
  _votedPosts[key] = true;
  sessionStorage.setItem('honeycomb_voted', JSON.stringify(_votedPosts));
}

async function fetchManaPercent() {
  if (_manaCache && Date.now() - _manaCache.fetchedAt < MANA_CACHE_TTL) return _manaCache.manaPercent;
  const auth = getStoredAuth();
  if (!auth) return 100;
  const accounts = await hiveRpc('condenser_api.get_accounts', [[auth.username]]);
  const account = accounts?.[0];
  if (!account) return 100;
  const vestingShares = parseFloat(account.vesting_shares);
  const delegatedIn = parseFloat(account.received_vesting_shares || '0');
  const delegatedOut = parseFloat(account.delegated_vesting_shares || '0');
  const maxMana = (vestingShares + delegatedIn - delegatedOut) * 1e6;
  const currentMana = computeCurrentMana(account.voting_manabar, maxMana);
  const pct = manaToPercent(currentMana, maxMana);
  _manaCache = { manaPercent: pct, fetchedAt: Date.now() };
  return pct;
}

function getVotePrefs() {
  // Read from on-chain prefs cached in settings, or use defaults
  return {
    floor: Number(localStorage.getItem('honeycomb_voteFloor') || 50),
    maxWeight: Number(localStorage.getItem('honeycomb_voteMaxWeight') || 25),
  };
}

async function handleVote(author, permlink, btn) {
  const auth = getStoredAuth();
  if (!auth) { showLoginPrompt(); return; }
  const key = `${author}/${permlink}`;
  if (_votedPosts[key]) { showToast('Already voted', 'info'); return; }

  btn.disabled = true;
  try {
    const manaPercent = await fetchManaPercent();
    const prefs = getVotePrefs();
    const weight = calculateVoteWeight(manaPercent, prefs.floor, prefs.maxWeight);
    if (weight === 0) {
      showToast('Voting power too low, try again later', 'info');
      btn.disabled = false;
      return;
    }
    await broadcastVote(author, permlink, weight);
    saveVotedPost(key);
    // Invalidate mana cache since we just voted
    _manaCache = null;
    // Update all heart icons for this post
    document.querySelectorAll(`.vote-btn[data-vote-key="${CSS.escape(key)}"]`).forEach(el => {
      el.classList.add('voted');
      el.setAttribute('aria-label', 'Voted');
    });
    showToast('Voted!', 'success');
  } catch(e) {
    showToast(e.message || 'Vote failed', 'error');
  }
  btn.disabled = false;
}

// Check if user already voted on a post (from bridge.get_post active_votes)
function checkExistingVote(postResult, username) {
  if (!postResult || !username) return false;
  const votes = postResult.active_votes || [];
  return votes.some(v => v.voter === username);
}

// ── Muting ──
function loadMutedUsers() {
  try { _mutedUsers = new Set(JSON.parse(localStorage.getItem(MUTED_KEY) || '[]')); } catch(e) { _mutedUsers = new Set(); }
}
function saveMutedUsers() {
  localStorage.setItem(MUTED_KEY, JSON.stringify(Array.from(_mutedUsers)));
}

async function fetchMutedList() {
  const auth = getStoredAuth();
  if (!auth) return;
  try {
    const result = await hiveRpc('bridge.get_relationship_between_accounts', [auth.username, '']);
    // Fallback: use condenser_api.get_following with type 'ignore'
  } catch(e) {}
  // Use condenser_api approach
  try {
    let allMuted = [];
    let start = '';
    for (let i = 0; i < 10; i++) { // max 1000 muted users
      const result = await hiveRpc('condenser_api.get_following', [getStoredAuth().username, start, 'ignore', 100]);
      if (!result || result.length === 0) break;
      allMuted = allMuted.concat(result.map(r => r.following));
      if (result.length < 100) break;
      start = result[result.length - 1].following;
    }
    _mutedUsers = new Set(allMuted);
    saveMutedUsers();
  } catch(e) {}
}

async function handleMuteUser(username) {
  const auth = getStoredAuth();
  if (!auth) { showLoginPrompt(); return; }
  if (_mutedUsers.has(username)) { showToast(`@${username} is already muted`, 'info'); return; }

  // Show confirmation
  if (!confirm(`Mute @${username}? Their posts will be hidden.`)) return;

  try {
    await broadcastMute(username);
    _mutedUsers.add(username);
    saveMutedUsers();
    showToast(`Muted @${username}`, 'success');
    closeModal();
    // Remove muted user's posts from view
    allPosts = allPosts.filter(p => p.author !== username);
    renderAll(allPosts, true);
    updateResultsBar();
  } catch(e) {
    showToast(e.message || 'Could not mute user', 'error');
  }
}

async function handleUnmuteUser(username) {
  try {
    await broadcastUnmute(username);
    _mutedUsers.delete(username);
    saveMutedUsers();
    showToast(`Unmuted @${username}`, 'success');
    renderMutedUsersList();
  } catch(e) {
    showToast(e.message || 'Could not unmute user', 'error');
  }
}

function filterMutedPosts(posts) {
  if (_mutedUsers.size === 0) return posts;
  return posts.filter(p => !_mutedUsers.has(p.author));
}

// ── Followed users ──
function loadFollowedUsers() {
  try { _followedUsers = new Set(JSON.parse(localStorage.getItem(FOLLOWED_KEY) || '[]')); } catch(e) { _followedUsers = new Set(); }
}
function saveFollowedUsers() {
  localStorage.setItem(FOLLOWED_KEY, JSON.stringify(Array.from(_followedUsers)));
}

async function fetchFollowedList() {
  const auth = getStoredAuth();
  if (!auth) return;
  try {
    let allFollowed = [];
    let start = '';
    for (let i = 0; i < 100; i++) { // max 10000 followed users
      const result = await hiveRpc('condenser_api.get_following', [auth.username, start, 'blog', 100]);
      if (!result || result.length === 0) break;
      allFollowed = allFollowed.concat(result.map(r => r.following));
      if (result.length < 100) break;
      start = result[result.length - 1].following;
    }
    _followedUsers = new Set(allFollowed);
    saveFollowedUsers();
  } catch(e) {}
}

async function handleFollowUser(username) {
  const auth = getStoredAuth();
  if (!auth) { showLoginPrompt(); return; }
  try {
    await broadcastFollow(username);
    _followedUsers.add(username);
    saveFollowedUsers();
    showToast(`Following @${username}`, 'success');
    const btn = document.getElementById('modal-follow-btn');
    if (btn) { btn.textContent = `Unfollow @${username}`; btn.onclick = () => handleUnfollowUser(username); }
  } catch(e) {
    showToast(e.message || 'Could not follow user', 'error');
  }
}

async function handleUnfollowUser(username) {
  try {
    await broadcastUnfollow(username);
    _followedUsers.delete(username);
    saveFollowedUsers();
    showToast(`Unfollowed @${username}`, 'success');
    // Update modal button if open
    const btn = document.getElementById('modal-follow-btn');
    if (btn && btn.style.display !== 'none') { btn.textContent = `Follow @${username}`; btn.onclick = () => handleFollowUser(username); }
    // Re-render followed list in settings if open
    renderFollowedUsersList();
  } catch(e) {
    showToast(e.message || 'Could not unfollow user', 'error');
  }
}

function renderFollowedUsersList() {
  const container = document.getElementById('settings-followed');
  if (!container) return;
  if (_followedUsers.size === 0) {
    container.innerHTML = '<p style="color:var(--text-dim);font-size:13px">No followed users.</p>';
    return;
  }
  container.innerHTML = '';
  _followedUsers.forEach(user => {
    const item = document.createElement('div');
    item.className = 'followed-user-item';
    item.innerHTML = `<span class="followed-user-name">@${esc(user)}</span><button type="button" class="btn btn-ghost followed-user-unfollow" onclick="handleUnfollowUser('${esc(user)}')">Unfollow</button>`;
    container.appendChild(item);
  });
}

// ── Init ──
async function init() {
  loadVotedPosts();
  loadMutedUsers();
  loadFollowedUsers();
  showSkeletons();

  let statsRes, catsRes, langsRes, postsRes, communitiesRes;
  try {
    [statsRes, catsRes, langsRes, postsRes, communitiesRes] = await Promise.all([
      fetch('/api/stats').then(r=>r.json()),
      fetch('/categories').then(r=>r.json()),
      fetch('/api/languages').then(r=>r.json()),
      fetch(`/api/browse?limit=${PAGE_SIZE}`).then(r=>r.json()),
      fetch('/api/communities').then(r=>r.json()).catch(() => ({ communities: [] })),
    ]);
  } catch(e) {
    const target = document.getElementById(getEffectiveLayout() === 'card' ? 'card-grid' : 'hex-grid');
    target.innerHTML = '<div class="empty"><h3>Connection error</h3><p>Could not reach the API. Please check if the server is running and try refreshing.</p></div>';
    if (getEffectiveLayout() === 'hex') { target.style.height = 'auto'; target.style.width = 'auto'; }
    document.getElementById('hex-container').style.display = getEffectiveLayout() === 'hex' ? '' : 'none';
    document.getElementById('card-grid').style.display = getEffectiveLayout() === 'card' ? '' : 'none';
    return;
  }

  totalPostCount = statsRes.total_posts || 0;
  filteredTotalCount = totalPostCount;

  if (statsRes.api_base_url) {
    const apiLink = document.getElementById('footer-api-link');
    if (apiLink) apiLink.href = statsRes.api_base_url + '/docs';
  }

  // Build category chips — one row per parent (as buttons for a11y)
  // Also populate leaf category list for editor tag autocomplete
  (catsRes.categories||[]).forEach(p => {
    (p.children||[]).forEach(ch => { _categoryLeafs.push(ch.name.toLowerCase().replace(/\s+/g, '-')); });
  });
  const catWrap = document.getElementById('cat-chips');
  (catsRes.categories||[]).forEach(parent => {
    const group = document.createElement('div');
    group.className = 'cat-group';
    const pChip = document.createElement('button');
    pChip.type = 'button';
    pChip.className = 'chip cat-parent';
    pChip.dataset.cat = parent.name;
    pChip.setAttribute('aria-pressed', 'false');
    pChip.textContent = parent.name;
    group.appendChild(pChip);
    (parent.children||[]).forEach(ch => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip';
      chip.dataset.cat = ch.name;
      chip.dataset.parent = parent.name;
      chip.setAttribute('aria-pressed', 'false');
      chip.textContent = ch.name;
      group.appendChild(chip);
    });
    catWrap.appendChild(group);
  });

  // Build language chips (as buttons for a11y)
  const langWrap = document.getElementById('lang-chips');
  (langsRes.languages||[]).slice(0, 40).forEach(l => {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'chip';
    el.dataset.lang = l.language;
    el.setAttribute('aria-pressed', 'false');
    el.textContent = l.language;
    langWrap.appendChild(el);
  });

  _communityList = (communitiesRes.communities || []).sort((a, b) => (b.post_count || 0) - (a.post_count || 0));

  // Wire up filter chip events (toggle active + aria-pressed)
  function toggleChip(chip) {
    chip.classList.toggle('active');
    chip.setAttribute('aria-pressed', chip.classList.contains('active'));
  }

  catWrap.addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    if (chip.classList.contains('cat-parent')) {
      const becoming = !chip.classList.contains('active');
      toggleChip(chip);
      catWrap.querySelectorAll(`.chip[data-parent="${chip.dataset.cat}"]`).forEach(c => {
        c.classList.toggle('active', becoming);
        c.setAttribute('aria-pressed', becoming);
      });
    } else {
      toggleChip(chip);
      const parentName = chip.dataset.parent;
      if (parentName) {
        const siblings = catWrap.querySelectorAll(`.chip[data-parent="${parentName}"]`);
        const allActive = Array.from(siblings).every(c => c.classList.contains('active'));
        const parentChip = catWrap.querySelector(`.cat-parent[data-cat="${parentName}"]`);
        if (parentChip) {
          parentChip.classList.toggle('active', allActive);
          parentChip.setAttribute('aria-pressed', allActive);
        }
      }
    }
    scheduleFilter();
  });

  document.getElementById('sentiment-chips').addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    toggleChip(chip);
    scheduleFilter();
  });

  langWrap.addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    toggleChip(chip);
    scheduleFilter();
  });

  // Set initial toggle state
  document.querySelectorAll('#layout-toggle button').forEach(b =>
    b.classList.toggle('active', b.dataset.layout === layoutMode)
  );

  // Auth UI + preferences
  renderAuthUI();
  if (getStoredAuth()) fetchMutedList(); // background fetch
  await loadAndApplyPreferences();

  // Fetch suggestions based on active categories (from preferences or manual)
  scheduleSuggestions();

  // If preferences activated filters, re-fetch with those filters
  if (document.querySelectorAll('.chip.active').length > 0) {
    await applyFilters();
  } else {
    allPosts = filterMutedPosts(postsRes.posts || []);
    currentOffset = allPosts.length;
    noMorePosts = allPosts.length < PAGE_SIZE;
    if (allPosts.length > 0) newestCreated = allPosts[0].created;
    seedMetaFromServer(allPosts);
    renderAll(allPosts, true);
    updateResultsBar();
    fetchMeta(allPosts);
  }
  setupInfiniteScroll();

  // Open post from URL if present (e.g. /@author/permlink)
  const postMatch = window.location.pathname.match(/^\/@([^/]+)\/(.+)$/);
  if (postMatch) {
    const [, author, permlink] = postMatch;
    _deepLinked = true;
    // Fetch the post from our API so modal gets full classification data
    try {
      const postRes = await fetch(`/posts/${encodeURIComponent(author)}/${encodeURIComponent(permlink)}`);
      if (postRes.ok) {
        const postData = await postRes.json();
        openModal(postData, true);
        // Re-fetch browse anchored to this post using cursor-based pagination
        const linkedTs = new Date(postData.created).getTime() / 1000 + 0.001;
        const anchorCursor = `${linkedTs}_${postData.id + 1}`;
        const anchorRes = await fetch(`/api/browse?limit=${PAGE_SIZE}&cursor=${encodeURIComponent(anchorCursor)}`);
        const anchorData = await anchorRes.json();
        const anchorPosts = anchorData.posts || [];
        const hasLinked = anchorPosts.some(p => p.author === author && p.permlink === permlink);
        if (!hasLinked) anchorPosts.unshift(postData);
        allPosts = anchorPosts;
        currentOffset = allPosts.length;
        noMorePosts = anchorPosts.length < PAGE_SIZE;
        _lastCursor = anchorData.next_cursor || null;
        seedMetaFromServer(allPosts);
        renderAll(allPosts, true);
        updateResultsBar();
        fetchMeta(allPosts);
      } else {
        // Post not in our DB — still try to open via Hive API
        openModal({ author, permlink }, true);
      }
    } catch(e) {
      openModal({ author, permlink }, true);
    }
  }
}

// ── Debounced filter trigger ──
function scheduleFilter() {
  clearTimeout(filterTimer);
  updateFilterCounts();
  filterTimer = setTimeout(applyFilters, 150);
  scheduleSuggestions();
}

// ── Build filter URL ──
function buildFilterUrl(limit, offset) {
  const cats = Array.from(document.querySelectorAll('#cat-chips .chip.active'))
    .map(c => c.dataset.cat).filter(Boolean);
  const sentiments = Array.from(document.querySelectorAll('#sentiment-chips .chip.active'))
    .map(c => c.dataset.sentiment).filter(Boolean);
  const langs = Array.from(document.querySelectorAll('#lang-chips .chip.active'))
    .map(c => c.dataset.lang).filter(Boolean);
  let url = `/api/browse?limit=${limit}&offset=${offset}`;
  cats.forEach(c => url += `&category=${encodeURIComponent(c)}`);
  langs.forEach(l => url += `&language=${encodeURIComponent(l)}`);
  if (_followingFilterActive && _followedUsers.size > 0) {
    _followedUsers.forEach(u => url += `&authors=${encodeURIComponent(u)}`);
  } else if (_myCommunitiesActive && _userCommunities && _userCommunities.length > 0) {
    _userCommunities.forEach(c => url += `&communities=${encodeURIComponent(c.id)}`);
  } else if (_activeCommunityFilter) {
    url += `&community=${encodeURIComponent(_activeCommunityFilter)}`;
  }
  if (sentiments.length === 1) url += `&sentiment=${encodeURIComponent(sentiments[0])}`;
  return { url, sentiments };
}

// ── Batch fetch titles + thumbnails from Hive (parallel with concurrency cap) ──
function cacheMetaEntry(key, entry) {
  if (!metaCache[key]) {
    metaCacheKeys.push(key);
    if (metaCacheKeys.length > META_CACHE_MAX) {
      const old = metaCacheKeys.shift();
      delete metaCache[old];
    }
  }
  metaCache[key] = entry;
}

async function fetchSingleMeta(p) {
  const key = `${p.author}/${p.permlink}`;
  const result = await hiveRpc('bridge.get_post', {author:p.author, permlink:p.permlink});
  if (result) {
    let images = (result.json_metadata?.image || []).map(u =>
      u.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/[\])].*$/, ''));
    if (!images.length && result.body) {
      const body = result.body;
      const mdMatch = body.match(/!\[[^\]]*\]\(([^)]+)\)/);
      const imgTagMatch = body.match(/<img[^>]+src=["']([^"']+)["']/i);
      const hostRe = /https?:\/\/(?:files\.peakd\.com|images\.ecency\.com|images\.hive\.blog|usermedia\.actifit\.io|images\.3speak\.tv|cdn\.steemitimages\.com)\/[^\s"'<>)]+/i;
      const extRe = /https?:\/\/[^\s"'<>)]+\.(?:jpg|jpeg|png|gif|webp|svg)/i;
      const found = mdMatch ? mdMatch[1] : (imgTagMatch ? imgTagMatch[1] : ((body.match(hostRe) || body.match(extRe) || [])[0] || ''));
      if (found) images = [found];
    }
    if (!images.length && result.body) {
      const ytMatch = result.body.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)/i);
      if (ytMatch) {
        images = [`https://img.youtube.com/vi/${ytMatch[1]}/hqdefault.jpg`];
      }
      if (!images.length) {
        const tsMatch = result.body.match(/3speak\.tv\/watch\?v=([\w.-]+)\/([\w-]+)/i);
        if (tsMatch) {
          images = [`https://images.3speak.tv/images/${tsMatch[2]}.webp`];
        }
      }
    }
    if (!images.length && result.cross_post_key) {
      const [cpAuthor, cpPermlink] = result.cross_post_key.split('/');
      if (cpAuthor && cpPermlink) {
        const cpResult = await hiveRpc('bridge.get_post', {author: cpAuthor, permlink: cpPermlink});
        if (cpResult) {
          let cpImages = (cpResult.json_metadata?.image || []).map(u =>
            u.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/[\])].*$/, ''));
          if (cpImages.length) images = [cpImages[0]];
        }
      }
    }
    cacheMetaEntry(key, {
      title: result.title || '',
      thumbnail: images.length ? images[0] : '',
    });
    updatePostElement(key);
  }
}

function seedMetaFromServer(posts) {
  for (const p of posts) {
    if (!p.title) continue;
    const key = `${p.author}/${p.permlink}`;
    if (metaCache[key]) continue;
    cacheMetaEntry(key, { title: p.title, thumbnail: '' });
  }
}

async function fetchMeta(posts) {
  if (!posts.length) return;
  const need = posts.filter(p => {
    const cached = metaCache[`${p.author}/${p.permlink}`];
    return !cached || !cached.thumbnail;
  });
  const chunks = [];
  for (let i = 0; i < need.length; i += 10) chunks.push(need.slice(i, i + 10));

  const CONCURRENCY = 6;
  let ci = 0;
  async function runNext() {
    if (ci >= chunks.length) return;
    const chunk = chunks[ci++];
    await Promise.all(chunk.map(fetchSingleMeta));
    return runNext();
  }
  await Promise.all(Array.from({ length: CONCURRENCY }, () => runNext()));
}

// ── Update a single post element with cached meta (works for both layouts) ──
function updatePostElement(key) {
  const cached = metaCache[key];
  if (!cached) return;

  // Update hex element if it exists
  const hexEl = document.querySelector(`.hex[data-key="${CSS.escape(key)}"]`);
  if (hexEl) {
    const titleEl = hexEl.querySelector('.hex-title');
    if (titleEl && cached.title) titleEl.textContent = cached.title;
    if (cached.thumbnail) {
      const inner = hexEl.querySelector('.hex-inner');
      if (inner && inner.classList.contains('no-img')) {
        inner.classList.remove('no-img');
        const ph = inner.querySelector('.hex-placeholder');
        if (ph) ph.remove();
        const imgDiv = document.createElement('div');
        imgDiv.className = 'hex-img';
        imgDiv.dataset.thumb = safeCssUrl(thumbUrl(cached.thumbnail));
        inner.insertBefore(imgDiv, inner.firstChild);
        thumbObserver.observe(imgDiv);
      }
    }
  }

  // Update card element if it exists
  const cardEl = document.querySelector(`.post-card[data-key="${CSS.escape(key)}"]`);
  if (cardEl) {
    const titleEl = cardEl.querySelector('.post-card-title');
    if (titleEl && cached.title) titleEl.textContent = cached.title;
    if (cached.thumbnail) {
      const thumbEl = cardEl.querySelector('.post-card-thumb');
      if (thumbEl && thumbEl.classList.contains('no-thumb')) {
        thumbEl.classList.remove('no-thumb');
        thumbEl.textContent = '';
        thumbEl.dataset.thumb = safeCssUrl(thumbUrl(cached.thumbnail));
        thumbObserver.observe(thumbEl);
      }
    }
  }

  // Update list element if it exists
  const listEl = document.querySelector(`.list-row[data-key="${CSS.escape(key)}"]`);
  if (listEl) {
    const titleEl = listEl.querySelector('.list-title');
    if (titleEl && cached.title) titleEl.textContent = cached.title;
    if (cached.thumbnail) {
      const thumbEl = listEl.querySelector('.list-thumb');
      if (thumbEl && !thumbEl.dataset.thumb && !thumbEl.style.backgroundImage) {
        thumbEl.textContent = '';
        thumbEl.dataset.thumb = safeCssUrl(thumbUrl(cached.thumbnail));
        thumbObserver.observe(thumbEl);
      }
    }
  }
}

// ── Filters ──
function resetFilters() {
  document.querySelectorAll('.chip.active').forEach(c => {
    c.classList.remove('active');
    c.setAttribute('aria-pressed', 'false');
  });
  _activeCommunityFilter = null;
  setMyCommunitiesActive(false);
  setFollowingActive(false);
  updateFilterCounts();
  document.getElementById('suggestions-bar').style.display = 'none';
  applyFilters();
}

async function applyFilters() {
  if (fetchAbort) fetchAbort.abort();
  fetchAbort = new AbortController();

  currentOffset = 0;
  noMorePosts = false;
  _lastCursor = null;

  const { url, sentiments } = buildFilterUrl(PAGE_SIZE, 0);

  try {
    const res = await fetch(url, {signal: fetchAbort.signal});
    const data = await res.json();
    allPosts = data.posts || [];
    filteredTotalCount = data.total || 0;
    if (sentiments.length > 1) {
      allPosts = allPosts.filter(p => sentiments.includes(p.sentiment));
    }
    allPosts = filterMutedPosts(allPosts);
    currentOffset = allPosts.length;
    noMorePosts = allPosts.length < PAGE_SIZE;
    seedMetaFromServer(allPosts);
    renderAll(allPosts, true);
    updateResultsBar();
    fetchMeta(allPosts);
  } catch(e) {
    if (e.name !== 'AbortError') console.error(e);
  }
}

// ── Infinite scroll ──
function setupInfiniteScroll() {
  const sentinel = document.getElementById('scroll-sentinel');
  const observer = new IntersectionObserver(entries => {
    if (entries[0].isIntersecting && !loadingMore && !noMorePosts) {
      loadMore();
    }
  }, { rootMargin: '400px' });
  observer.observe(sentinel);
}

async function loadMore() {
  loadingMore = true;
  document.getElementById('loading-more').style.display = 'block';

  const { url, sentiments } = buildFilterUrl(PAGE_SIZE, currentOffset);
  const fetchUrl = _lastCursor ? url + `&cursor=${encodeURIComponent(_lastCursor)}` : url;

  try {
    const res = await fetch(fetchUrl);
    const data = await res.json();
    let newPosts = data.posts || [];
    if (sentiments.length > 1) {
      newPosts = newPosts.filter(p => sentiments.includes(p.sentiment));
    }
    newPosts = filterMutedPosts(newPosts);
    if (newPosts.length < PAGE_SIZE) noMorePosts = true;
    _lastCursor = data.next_cursor || null;
    if (newPosts.length > 0) {
      seedMetaFromServer(newPosts);
      const startIdx = allPosts.length;
      allPosts = allPosts.concat(newPosts);
      currentOffset = allPosts.length;
      appendPosts(newPosts, startIdx);
      updateResultsBar();
      fetchMeta(newPosts);
    }
  } catch(e) {
    console.error(e);
  }

  document.getElementById('loading-more').style.display = 'none';
  loadingMore = false;
}

// ── Unified render dispatcher ──
function renderAll(posts, fullRebuild) {
  const layout = getEffectiveLayout();
  const hexContainer = document.getElementById('hex-container');
  const cardGrid = document.getElementById('card-grid');
  const listGrid = document.getElementById('list-grid');

  if (layout === 'list') {
    hexContainer.style.display = 'none';
    cardGrid.style.display = 'none';
    listGrid.style.display = '';
    if (fullRebuild) {
      listGrid.innerHTML = '';
      renderList(posts, listGrid);
    }
  } else if (layout === 'card') {
    hexContainer.style.display = 'none';
    cardGrid.style.display = '';
    listGrid.style.display = 'none';
    if (fullRebuild) {
      cardGrid.innerHTML = '';
      renderCards(posts, cardGrid);
    }
  } else {
    cardGrid.style.display = 'none';
    listGrid.style.display = 'none';
    hexContainer.style.display = '';
    if (fullRebuild) renderHexGrid(posts);
  }

  if (!posts.length) {
    const emptyHtml = '<div class="empty"><h3>No posts found</h3><p>Try adjusting your filters or wait for the worker to classify more posts.</p></div>';
    if (layout === 'list') {
      listGrid.innerHTML = emptyHtml;
    } else if (layout === 'card') {
      cardGrid.innerHTML = emptyHtml;
    } else {
      const grid = document.getElementById('hex-grid');
      grid.innerHTML = emptyHtml;
      grid.style.height = 'auto';
      grid.style.width = 'auto';
    }
  }
}

// ── Append posts incrementally (for infinite scroll) ──
function appendPosts(newPosts, startIdx) {
  const layout = getEffectiveLayout();
  if (layout === 'list') {
    renderList(newPosts, document.getElementById('list-grid'));
  } else if (layout === 'card') {
    renderCards(newPosts, document.getElementById('card-grid'));
  } else {
    appendHexes(newPosts, startIdx);
  }
}

// ── Card rendering (lazy thumbnails) ──
function renderCards(posts, container) {
  posts.forEach(p => {
    const key = `${p.author}/${p.permlink}`;
    const cached = metaCache[key];
    const thumb = cached ? thumbUrl(cached.thumbnail) : '';
    const title = (cached && cached.title) || p.permlink.replace(/-/g, ' ').slice(0, 60);
    const catLabel = (p.categories || []).slice(0, 2);
    const borderColor = sentimentColor(p.sentiment_score);

    const card = document.createElement('a');
    card.className = 'post-card' + (isRead(key) ? ' read' : '');
    card.tabIndex = 0;
    card.href = `/@${p.author}/${p.permlink}`;
    card.dataset.key = key;
    card.setAttribute('aria-label', `${title} by @${p.author}`);
    card.onclick = e => { e.preventDefault(); openModal(p); };
    card.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openModal(p); } };
    card.style.borderTopColor = borderColor;
    card.style.borderTopWidth = '3px';

    let tagsHtml = '';
    if (p.community_name) {
      tagsHtml += `<span class="community-badge" data-community="${esc(p.community_id)}" onclick="event.preventDefault();event.stopPropagation();filterByCommunity('${esc(p.community_id)}')" title="${esc(p.community_name)}">${esc(p.community_name)}</span>`;
    }
    catLabel.forEach(c => { tagsHtml += `<span class="tag">${esc(c)}</span>`; });
    if (p.sentiment && safeSentiment(p.sentiment)) {
      tagsHtml += `<span class="tag sentiment-${safeSentiment(p.sentiment)}">${esc(p.sentiment)}</span>`;
    }
    (p.languages || []).forEach(lang => {
      tagsHtml += `<span class="tag">${esc(lang.toUpperCase())}</span>`;
    });

    const noThumbStyle = `background:linear-gradient(135deg,${borderColor}22 0%,var(--bg2) 60%)`;
    const voted = _votedPosts[key];
    card.innerHTML = `
      <div class="post-card-thumb${thumb ? '' : ' no-thumb'}" ${thumb ? `data-thumb="${safeCssUrl(thumb)}"` : `style="${noThumbStyle}"`}>
        ${thumb ? '' : `<img class="no-thumb-avatar" src="https://images.hive.blog/u/${encodeURIComponent(p.author)}/avatar" alt="@${esc(p.author)}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'@${esc(p.author)}'}))">`}
      </div>
      <div class="post-card-body">
        <div class="post-card-title">${esc(title)}</div>
        <div class="post-card-author"><img class="author-avatar" src="https://images.hive.blog/u/${encodeURIComponent(p.author)}/avatar/small" alt="" width="24" height="24">@${esc(p.author)}${p.created ? ' · ' + new Date(p.created).toLocaleDateString('en', {month:'short',day:'numeric'}) : ''}</div>
        <div class="post-card-meta">${tagsHtml}</div>
      </div>
      <button type="button" class="vote-btn${voted ? ' voted' : ''}" data-vote-key="${esc(key)}" aria-label="${voted ? 'Voted' : 'Vote'}" onclick="event.preventDefault();event.stopPropagation();handleVote('${esc(p.author)}','${esc(p.permlink)}',this)"><svg viewBox="0 0 24 24" width="18" height="18"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg></button>`;
    container.appendChild(card);
    // Lazy-load thumbnail
    const thumbEl = card.querySelector('[data-thumb]');
    if (thumbEl) thumbObserver.observe(thumbEl);
  });
}

// ── List rendering ──
function renderList(posts, container) {
  posts.forEach(p => {
    const key = `${p.author}/${p.permlink}`;
    const cached = metaCache[key];
    const title = (cached && cached.title) || p.permlink.replace(/-/g, ' ').slice(0, 60);
    const thumb = cached ? thumbUrl(cached.thumbnail) : '';
    const borderColor = sentimentColor(p.sentiment_score);
    const catLabel = (p.categories || []).slice(0, 2);

    const row = document.createElement('a');
    row.className = 'list-row' + (isRead(key) ? ' read' : '');
    row.tabIndex = 0;
    row.href = `/@${p.author}/${p.permlink}`;
    row.dataset.key = key;
    row.setAttribute('aria-label', `${title} by @${p.author}`);
    row.onclick = e => { e.preventDefault(); openModal(p); };
    row.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openModal(p); } };
    row.style.borderLeftColor = borderColor;

    let tagsHtml = '';
    if (p.community_name) {
      tagsHtml += `<span class="community-badge">${esc(p.community_name)}</span>`;
    }
    catLabel.forEach(c => { tagsHtml += `<span class="tag">${esc(c)}</span>`; });

    const voted = _votedPosts[key];
    row.innerHTML = `
      <div class="list-thumb" ${thumb ? `data-thumb="${safeCssUrl(thumb)}"` : ''}>
        ${thumb ? '' : `<img class="no-thumb-avatar list-avatar" src="https://images.hive.blog/u/${encodeURIComponent(p.author)}/avatar/small" alt="@${esc(p.author)}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'@${esc(p.author).slice(0,2)}'}))">`}
      </div>
      <div class="list-content">
        <div class="list-title">${esc(title)}</div>
        <div class="list-meta"><img class="author-avatar" src="https://images.hive.blog/u/${encodeURIComponent(p.author)}/avatar/small" alt="" width="20" height="20">@${esc(p.author)} · ${p.created ? new Date(p.created).toLocaleDateString('en', {month:'short',day:'numeric'}) : ''} ${tagsHtml}</div>
      </div>
      <button type="button" class="vote-btn${voted ? ' voted' : ''}" data-vote-key="${esc(key)}" aria-label="${voted ? 'Voted' : 'Vote'}" onclick="event.preventDefault();event.stopPropagation();handleVote('${esc(p.author)}','${esc(p.permlink)}',this)"><svg viewBox="0 0 24 24" width="16" height="16"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg></button>`;
    container.appendChild(row);
    const thumbEl = row.querySelector('[data-thumb]');
    if (thumbEl) thumbObserver.observe(thumbEl);
  });
}

// ── Hex grid geometry ──
function computeHexLayout(count) {
  const { w, h, gap } = hexMetrics();
  const grid = document.getElementById('hex-grid');
  const containerW = grid.parentElement.clientWidth - 40;
  const cols = Math.max(2, Math.floor((containerW + gap) / (w + gap)));
  const gridW = cols * (w + gap) - gap + (w / 2 + gap / 2);
  const rowStep = h * 0.75 + gap;
  const positions = [];
  let maxY = 0, idx = 0, row = 0;
  while (idx < count) {
    const isOffset = row % 2 === 1;
    const rowCols = isOffset ? cols - 1 : cols;
    for (let col = 0; col < rowCols && idx < count; col++, idx++) {
      const x = col * (w + gap) + (isOffset ? (w + gap) / 2 : 0);
      const y = row * rowStep;
      positions.push({x, y});
      if (y + h > maxY) maxY = y + h;
    }
    row++;
  }
  return { positions, gridW, maxY };
}

function createHexElement(p, x, y) {
  const key = `${p.author}/${p.permlink}`;
  const borderColor = sentimentColor(p.sentiment_score);
  const cached = metaCache[key];
  const thumb = cached ? thumbUrl(cached.thumbnail) : '';
  const catLabel = (p.categories||[]).slice(0,2).join(', ') || '';
  const cachedTitle = (cached && cached.title) || p.permlink.replace(/-/g,' ').slice(0,40);

  const hex = document.createElement('a');
  hex.className = 'hex' + (isRead(key) ? ' read' : '');
  hex.tabIndex = 0;
  hex.href = `/@${p.author}/${p.permlink}`;
  hex.dataset.key = key;
  hex.setAttribute('aria-label', `${cachedTitle} by @${p.author}`);
  hex.style.left = x + 'px';
  hex.style.top = y + 'px';
  hex.onclick = e => { e.preventDefault(); openModal(p); };
  hex.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openModal(p); } };

  const communityHtml = p.community_name ? `<div class="hex-community">${esc(p.community_name)}</div>` : '';

  hex.innerHTML = `
    <div class="hex-border" style="background:${borderColor}"></div>
    <div class="hex-inner${thumb ? '' : ' no-img'}">
      ${thumb
        ? `<div class="hex-img" data-thumb="${safeCssUrl(thumb)}"></div>`
        : `<div class="hex-placeholder"><img class="no-thumb-avatar" src="https://images.hive.blog/u/${encodeURIComponent(p.author)}/avatar" alt="@${esc(p.author)}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'@${esc(p.author)}'}))"></div>`
      }
      <div class="hex-overlay">
        <div class="hex-title">${esc(cachedTitle)}</div>
        <div class="hex-author">@${esc(p.author)}</div>
        ${communityHtml}
        <div class="hex-cats">${esc(catLabel)}</div>
      </div>
    </div>`;
  // Lazy-load thumbnail
  const thumbEl = hex.querySelector('[data-thumb]');
  if (thumbEl) thumbObserver.observe(thumbEl);
  return hex;
}

// ── Full hex grid rebuild (filter change, layout switch, resize) ──
function renderHexGrid(posts) {
  const grid = document.getElementById('hex-grid');
  grid.innerHTML = '';
  if (!posts.length) {
    grid.style.height = 'auto';
    grid.style.width = 'auto';
    return;
  }
  const { positions, gridW, maxY } = computeHexLayout(posts.length);
  grid.style.width = gridW + 'px';
  grid.style.height = maxY + 'px';
  posts.forEach((p, i) => {
    grid.appendChild(createHexElement(p, positions[i].x, positions[i].y));
  });
}

// ── Incremental hex append (infinite scroll — no full rebuild) ──
function appendHexes(newPosts, startIdx) {
  const grid = document.getElementById('hex-grid');
  const { positions, gridW, maxY } = computeHexLayout(allPosts.length);
  grid.style.width = gridW + 'px';
  grid.style.height = maxY + 'px';
  newPosts.forEach((p, i) => {
    grid.appendChild(createHexElement(p, positions[startIdx + i].x, positions[startIdx + i].y));
  });
}

// Recompute grid on window resize
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => renderAll(allPosts, true), 200);
});

// ── Live update: poll for new posts (visibility-aware) ──
let liveTimer = null;
const LIVE_INTERVAL = 30000;
let newestCreated = null;

function hasActiveFilters() {
  return document.querySelectorAll('.chip.active').length > 0 || _followingFilterActive;
}

async function pollNewPosts() {
  if (_deepLinked || hasActiveFilters() || !newestCreated) return;
  try {
    const [browseData, statsData] = await Promise.all([
      fetch('/api/browse?limit=10').then(r => r.json()),
      fetch('/api/stats').then(r => r.json()),
    ]);
    totalPostCount = statsData.total_posts || totalPostCount;
    const fresh = filterMutedPosts((browseData.posts || []).filter(p =>
      p.created > newestCreated && !allPosts.some(e => e.id === p.id)
    ));
    if (fresh.length > 0) {
      seedMetaFromServer(fresh);
      allPosts = fresh.concat(allPosts);
      // Cap allPosts to prevent unbounded growth
      if (allPosts.length > ALL_POSTS_MAX) {
        allPosts = allPosts.slice(0, ALL_POSTS_MAX);
      }
      newestCreated = allPosts[0].created;
      renderAll(allPosts, true);
      updateResultsBar();
      fetchMeta(fresh);
    }
  } catch(e) {}
}

function startLiveUpdates() {
  liveTimer = setInterval(pollNewPosts, LIVE_INTERVAL);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearInterval(liveTimer);
    liveTimer = null;
  } else {
    pollNewPosts();
    startLiveUpdates();
  }
});

// ── Comments ──
function relativeTime(dateStr) {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + 'h ago';
  const days = Math.floor(hrs / 24);
  if (days < 30) return days + 'd ago';
  const months = Math.floor(days / 30);
  return months + 'mo ago';
}

function showCommentSkeletons() {
  const tree = document.getElementById('comments-tree');
  let html = '<div class="comment-skeleton">';
  for (let i = 0; i < 3; i++) {
    html += '<div class="comment-skeleton-item">' +
      '<div class="comment-skeleton-avatar skeleton"></div>' +
      '<div class="comment-skeleton-lines">' +
      '<div class="comment-skeleton-line skeleton"></div>' +
      '<div class="comment-skeleton-line skeleton"></div>' +
      '<div class="comment-skeleton-line skeleton"></div>' +
      '</div></div>';
  }
  html += '</div>';
  tree.innerHTML = html;
}

function renderComment(comment, depth) {
  const maxVisualDepth = 4;
  const flatClass = depth >= maxVisualDepth ? ' comment-children-depth4' : '';
  const bodyHtml = renderHiveBody(comment.body || '');
  const rep = comment.reputation != null ? comment.reputation.toFixed(0) : '';

  let childrenHtml = '';
  if (comment.children && comment.children.length > 0) {
    const collapsed = comment.children.length > 5;
    const toggleId = 'ct-' + comment.author + '-' + comment.permlink.slice(0, 12);
    childrenHtml = '<div class="comment-children' + flatClass + '"' +
      (collapsed ? ' id="' + esc(toggleId) + '" style="display:none"' : '') + '>';
    comment.children.forEach(child => {
      childrenHtml += renderComment(child, depth + 1);
    });
    childrenHtml += '</div>';
    if (collapsed) {
      childrenHtml = '<button type="button" class="comment-toggle" onclick="toggleCommentChildren(this,\'' + esc(toggleId) + '\')">' +
        comment.children.length + ' replies &#9660;</button>' + childrenHtml;
    }
  }

  const replyBtn = getStoredAuth()
    ? '<button type="button" class="comment-toggle comment-reply-btn" onclick="openReplyForm(\'' + esc(comment.author) + '\',\'' + esc(comment.permlink) + '\',this)">Reply</button>'
    : '';

  return '<div class="comment">' +
    '<div class="comment-head">' +
      '<a class="comment-author" href="https://peakd.com/@' + encodeURIComponent(comment.author) + '" target="_blank" rel="noopener noreferrer">@' + esc(comment.author) + '</a>' +
      (rep ? '<span class="comment-rep">' + esc(rep) + '</span>' : '') +
      replyBtn +
      '<span class="comment-time">' + esc(relativeTime(comment.created)) + '</span>' +
    '</div>' +
    '<div class="comment-body rendered-body">' + bodyHtml + '</div>' +
    childrenHtml +
  '</div>';
}

function toggleCommentChildren(btn, id) {
  const el = document.getElementById(id);
  if (!el) return;
  const hidden = el.style.display === 'none';
  el.style.display = hidden ? '' : 'none';
  const count = el.querySelectorAll(':scope > .comment').length;
  btn.innerHTML = hidden
    ? count + ' replies &#9650;'
    : count + ' replies &#9660;';
}

// Current post being viewed (for comment posting)
let _modalPostAuthor = '';
let _modalPostPermlink = '';
let _commentCooldown = false;

function renderCommentForm(parentAuthor, parentPermlink, isTopLevel) {
  const auth = getStoredAuth();
  if (!auth) {
    return '<div class="comment-form-login">Log in with Hive Keychain to comment.</div>';
  }
  const formId = isTopLevel ? 'comment-form-top' : 'comment-form-reply';
  return '<div class="comment-form" id="' + formId + '">' +
    '<textarea class="comment-textarea" id="' + formId + '-textarea" placeholder="Write a comment..." maxlength="64000" rows="3"></textarea>' +
    '<div class="comment-form-actions">' +
      '<button type="button" class="comment-preview-btn" onclick="toggleCommentPreview(\'' + formId + '\')">Preview</button>' +
      '<div style="flex:1"></div>' +
      (!isTopLevel ? '<button type="button" class="btn btn-ghost comment-cancel-btn" onclick="closeReplyForm()">Cancel</button>' : '') +
      '<button type="button" class="btn comment-submit-btn" id="' + formId + '-submit" onclick="submitComment(\'' + esc(parentAuthor) + '\',\'' + esc(parentPermlink) + '\',\'' + formId + '\')">Post Comment</button>' +
    '</div>' +
    '<div class="comment-preview rendered-body" id="' + formId + '-preview" style="display:none"></div>' +
  '</div>';
}

function toggleCommentPreview(formId) {
  const textarea = document.getElementById(formId + '-textarea');
  const preview = document.getElementById(formId + '-preview');
  if (!textarea || !preview) return;
  if (preview.style.display === 'none') {
    preview.innerHTML = renderHiveBody(textarea.value || '');
    preview.style.display = '';
  } else {
    preview.style.display = 'none';
  }
}

let _activeReplyFormParent = null;

function openReplyForm(author, permlink, btn) {
  closeReplyForm();
  const comment = btn.closest('.comment');
  if (!comment) return;
  const formHtml = renderCommentForm(author, permlink, false);
  const formWrapper = document.createElement('div');
  formWrapper.className = 'comment-reply-wrapper';
  formWrapper.innerHTML = formHtml;
  // Insert after comment-body, before children
  const body = comment.querySelector('.comment-body');
  body.insertAdjacentElement('afterend', formWrapper);
  _activeReplyFormParent = formWrapper;
  const textarea = formWrapper.querySelector('textarea');
  if (textarea) textarea.focus();
}

function closeReplyForm() {
  if (_activeReplyFormParent) {
    _activeReplyFormParent.remove();
    _activeReplyFormParent = null;
  }
}

async function submitComment(parentAuthor, parentPermlink, formId) {
  if (_commentCooldown) {
    showToast('Please wait a moment before posting again', 'info');
    return;
  }
  const textarea = document.getElementById(formId + '-textarea');
  const submitBtn = document.getElementById(formId + '-submit');
  if (!textarea || !submitBtn) return;
  const body = textarea.value.trim();
  if (!body) {
    showToast('Comment cannot be empty', 'error');
    return;
  }
  submitBtn.disabled = true;
  submitBtn.textContent = 'Broadcasting...';
  try {
    await broadcastComment(parentAuthor, parentPermlink, body);
    showToast('Comment posted!', 'success');
    textarea.value = '';
    closeReplyForm();
    // Cooldown
    _commentCooldown = true;
    setTimeout(() => { _commentCooldown = false; }, 3000);
    // Re-fetch after a short delay (blockchain confirmation)
    setTimeout(() => fetchComments(_modalPostAuthor, _modalPostPermlink), 2000);
  } catch(e) {
    showToast(e.message || 'Could not post comment', 'error');
  }
  submitBtn.disabled = false;
  submitBtn.textContent = 'Post Comment';
}

async function fetchComments(author, permlink) {
  const section = document.getElementById('comments-section');
  const tree = document.getElementById('comments-tree');
  const countEl = document.getElementById('comments-count');
  const hiddenEl = document.getElementById('comments-hidden');

  _modalPostAuthor = author;
  _modalPostPermlink = permlink;
  section.style.display = '';
  countEl.textContent = '';
  hiddenEl.style.display = 'none';
  showCommentSkeletons();

  try {
    const discussion = await hiveRpc('bridge.get_discussion', {author, permlink});
    if (!discussion) {
      tree.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:8px">Could not load comments.</div>';
      return;
    }

    const rootKey = `${author}/${permlink}`;
    const rootEntry = discussion[rootKey];
    if (!rootEntry || !rootEntry.replies || rootEntry.replies.length === 0) {
      const topForm = renderCommentForm(author, permlink, true);
      tree.innerHTML = topForm + '<div style="color:var(--text-dim);font-size:13px;padding:8px">No comments yet.</div>';
      countEl.textContent = '';
      return;
    }

    let hiddenCount = 0;
    function buildTree(key) {
      const entry = discussion[key];
      if (!entry) return null;
      if (entry.author_reputation != null && entry.author_reputation <= 0) {
        hiddenCount++;
        return null;
      }
      return {
        author: entry.author,
        permlink: entry.permlink,
        body: entry.body || '',
        reputation: entry.author_reputation || 0,
        created: entry.created,
        children: (entry.replies || []).map(buildTree).filter(Boolean)
      };
    }

    const comments = (rootEntry.replies || []).map(buildTree).filter(Boolean);

    let totalVisible = 0;
    function countAll(arr) { arr.forEach(c => { totalVisible++; if (c.children) countAll(c.children); }); }
    countAll(comments);

    const topForm = renderCommentForm(author, permlink, true);
    countEl.textContent = totalVisible + ' comment' + (totalVisible !== 1 ? 's' : '');
    let html = topForm;
    comments.forEach(c => { html += renderComment(c, 0); });
    tree.innerHTML = html;

    if (hiddenCount > 0) {
      hiddenEl.style.display = '';
      hiddenEl.textContent = hiddenCount + ' comment' + (hiddenCount !== 1 ? 's' : '') + ' hidden by reputation filter';
    }
  } catch(e) {
    tree.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:8px">Could not load comments.</div>';
  }
}

// ── Modal ──
async function openModal(post, skipPush) {
  markRead(`${post.author}/${post.permlink}`);
  if (!skipPush) {
    history.pushState({ author: post.author, permlink: post.permlink },
                      '', `/@${post.author}/${post.permlink}`);
  }
  document.getElementById('modal-title').textContent = 'Loading...';
  document.getElementById('modal-author').innerHTML = `<img class="author-avatar" src="https://images.hive.blog/u/${encodeURIComponent(post.author)}/avatar/small" alt="" width="28" height="28">@${esc(post.author)}`;
  const commEl = document.getElementById('modal-community');
  if (post.community_name && post.community_id) {
    commEl.textContent = post.community_name;
    commEl.href = `https://peakd.com/c/${encodeURIComponent(post.community_id)}`;
    commEl.style.display = '';
  } else {
    commEl.style.display = 'none';
  }
  const dateEl = document.getElementById('modal-date');
  dateEl.textContent = post.created ? new Date(post.created).toLocaleDateString('en', {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
  document.getElementById('modal-body').innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-dim)"><div class="spinner" style="width:28px;height:28px;border:3px solid var(--card);border-top-color:var(--hive-red);border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 8px"></div>Loading content...</div>';

  const tagsEl = document.getElementById('modal-tags');
  tagsEl.innerHTML = '';
  (post.categories||[]).forEach(c => {
    const t = document.createElement('span');
    t.className = 'tag'; t.textContent = c; tagsEl.appendChild(t);
  });
  if (post.sentiment && safeSentiment(post.sentiment)) {
    const s = document.createElement('span');
    s.className = `tag sentiment-${safeSentiment(post.sentiment)}`;
    s.textContent = `${post.sentiment} (${(post.sentiment_score||0).toFixed(2)})`;
    tagsEl.appendChild(s);
  }
  (post.languages||[]).forEach(lang => {
    const l = document.createElement('span');
    l.className = 'tag'; l.textContent = lang.toUpperCase(); tagsEl.appendChild(l);
  });

  document.getElementById('modal-score').textContent = '';
  document.getElementById('modal-hivelink').href = `https://hivel.ink/@${post.author}/${post.permlink}`;

  // Vote button in modal
  const voteKey = `${post.author}/${post.permlink}`;
  const modalVoteBtn = document.getElementById('modal-vote-btn');
  modalVoteBtn.className = 'vote-btn modal-vote-btn' + (_votedPosts[voteKey] ? ' voted' : '');
  modalVoteBtn.setAttribute('data-vote-key', voteKey);
  modalVoteBtn.setAttribute('aria-label', _votedPosts[voteKey] ? 'Voted' : 'Vote');
  modalVoteBtn.onclick = () => handleVote(post.author, post.permlink, modalVoteBtn);

  // Follow/Mute buttons in modal
  const auth = getStoredAuth();
  const followBtn = document.getElementById('modal-follow-btn');
  const muteBtn = document.getElementById('modal-mute-btn');
  if (auth && auth.username !== post.author) {
    if (_followedUsers.has(post.author)) {
      followBtn.textContent = `Unfollow @${post.author}`;
      followBtn.onclick = () => handleUnfollowUser(post.author);
    } else {
      followBtn.textContent = `Follow @${post.author}`;
      followBtn.onclick = () => handleFollowUser(post.author);
    }
    followBtn.style.display = '';
    muteBtn.textContent = `Mute @${post.author}`;
    muteBtn.onclick = () => handleMuteUser(post.author);
    muteBtn.style.display = '';
  } else {
    followBtn.style.display = 'none';
    muteBtn.style.display = 'none';
  }

  const modalEl = document.getElementById('modal');
  modalEl.classList.add('open');
  trapFocus(modalEl.querySelector('.modal'));

  // Fetch comments in parallel with post body
  fetchComments(post.author, post.permlink);

  const result = await hiveRpc('bridge.get_post', {author: post.author, permlink: post.permlink});
  if (result) {
    document.getElementById('modal-title').textContent = result.title || post.permlink;
    document.getElementById('modal-body').innerHTML = renderHiveBody(result.body || '');
    // Check if user already voted
    if (auth && checkExistingVote(result, auth.username) && !_votedPosts[voteKey]) {
      saveVotedPost(voteKey);
      document.querySelectorAll(`.vote-btn[data-vote-key="${CSS.escape(voteKey)}"]`).forEach(el => {
        el.classList.add('voted');
        el.setAttribute('aria-label', 'Voted');
      });
    }
  }
  if (!result) {
    document.getElementById('modal-title').textContent = post.permlink;
    document.getElementById('modal-body').textContent = '(Could not load post content)';
  }
}

function closeModal(skipPush) {
  const modalEl = document.getElementById('modal');
  releaseFocus(modalEl.querySelector('.modal'));
  modalEl.classList.remove('open');
  if (!skipPush && !_deepLinked && window.location.pathname !== '/') {
    history.pushState(null, '', '/');
  }
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (document.getElementById('editor-modal').classList.contains('open')) confirmCloseEditor();
    else if (document.getElementById('login-overlay').classList.contains('open')) closeLogin();
    else if (document.getElementById('settings-modal').classList.contains('open')) closeSettingsModal();
    else closeModal();
  }
});

// ── Popstate (browser back/forward) ──
window.addEventListener('popstate', e => {
  if (e.state && e.state.author) {
    openModal({ author: e.state.author, permlink: e.state.permlink }, true);
  } else {
    closeModal(true);
  }
});

// ── Auth UI ──
function renderAuthUI() {
  const area = document.getElementById('auth-area');
  const auth = getStoredAuth();
  if (auth) {
    area.innerHTML =
      '<button type="button" class="auth-login" onclick="openEditor()" style="background:var(--hive-red);color:#fff;border-color:var(--hive-red);padding:6px 10px" title="Write Post"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.66V20a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5.34"/><polygon points="18 2 22 6 12 16 8 16 8 12 18 2"/></svg></button>' +
      '<span class="auth-user"><img class="auth-avatar" src="https://images.hive.blog/u/' + encodeURIComponent(auth.username) + '/avatar/small" alt="" width="22" height="22">@' + esc(auth.username) + '</span>' +
      '<a class="auth-settings" href="#" onclick="showSettingsModal();return false" title="Filter preferences">Settings</a>' +
      '<a class="auth-logout" href="#" onclick="doLogout();return false">Logout</a>';
    document.getElementById('btn-save-prefs').style.display = '';
    document.getElementById('my-communities-toggle').style.display = '';
    document.getElementById('following-toggle').style.display = '';
    fetchUserCommunities(auth.username).then(list => { _userCommunities = list; });
  } else {
    area.innerHTML = '<a class="auth-login" href="#" onclick="showLoginPrompt();return false">Login</a>';
    document.getElementById('btn-save-prefs').style.display = 'none';
    document.getElementById('my-communities-toggle').style.display = 'none';
    document.getElementById('following-toggle').style.display = 'none';
  }
}

function showLoginPrompt() {
  const overlay = document.getElementById('login-overlay');
  overlay.classList.add('open');
  document.getElementById('login-error').textContent = '';
  trapFocus(overlay.querySelector('.login-box'));
}

function closeLogin() {
  const overlay = document.getElementById('login-overlay');
  releaseFocus(overlay.querySelector('.login-box'));
  overlay.classList.remove('open');
}

function openSignup() {
  closeLogin();
  document.getElementById('signup-iframe').src = 'https://hivedapps.com/';
  document.getElementById('signup-overlay').classList.add('open');
}
function closeSignup() {
  document.getElementById('signup-overlay').classList.remove('open');
  document.getElementById('signup-iframe').src = 'about:blank';
}

async function doLogin() {
  const input = document.getElementById('login-username').value.trim().toLowerCase();
  if (!input) return;
  const btn = document.getElementById('login-btn');
  const err = document.getElementById('login-error');
  btn.disabled = true;
  btn.textContent = 'Signing...';
  err.textContent = '';
  try {
    await loginWithKeychain(input);
    closeLogin();
    renderAuthUI();
    fetchMutedList(); // background fetch
    fetchFollowedList().then(() => {
      if (_followedUsers.size > 0) { setFollowingActive(true); scheduleFilter(); }
    }); // background fetch
    const prefs = await loadAndApplyPreferences();
    if (isFirstLogin(prefs)) {
      showSettingsModal();
    } else if (document.querySelectorAll('.chip.active').length > 0) {
      applyFilters();
    }
  } catch(e) {
    err.textContent = e.message || 'Login failed. Is Keychain unlocked?';
  }
  btn.disabled = false;
  btn.textContent = 'Sign In';
}

async function doLogout() {
  await logout();
  sessionStorage.removeItem('honeycomb_user_communities');
  sessionStorage.removeItem('honeycomb_voted');
  _userCommunities = null;
  _votedPosts = {};
  _manaCache = null;
  setMyCommunitiesActive(false);
  setFollowingActive(false);
  renderAuthUI();
  resetFilters();
}

// ── Preferences ──
async function loadAndApplyPreferences() {
  const auth = getStoredAuth();
  if (!auth) return null;
  try {
    const accounts = await hiveRpc('condenser_api.get_accounts', [[auth.username]]);
    const account = accounts?.[0];
    if (!account) return null;
    let meta = {};
    try { meta = JSON.parse(account.posting_json_metadata || '{}'); } catch(e) {}
    const prefs = meta.combflow || {};
    applyPreferenceFilters(prefs);
    return prefs;
  } catch(e) { return null; }
}

function applyPreferenceFilters(prefs) {
  // Cache vote settings locally
  if (prefs.voteFloor != null) localStorage.setItem('honeycomb_voteFloor', prefs.voteFloor);
  if (prefs.voteMaxWeight != null) localStorage.setItem('honeycomb_voteMaxWeight', prefs.voteMaxWeight);
  // Activate category chips
  (prefs.default_categories || []).forEach(cat => {
    const chip = document.querySelector('#cat-chips .chip[data-cat="' + CSS.escape(cat) + '"]');
    if (chip) { chip.classList.add('active'); chip.setAttribute('aria-pressed', 'true'); }
  });
  // Activate language chips
  (prefs.default_languages || []).forEach(lang => {
    const chip = document.querySelector('#lang-chips .chip[data-lang="' + CSS.escape(lang) + '"]');
    if (chip) { chip.classList.add('active'); chip.setAttribute('aria-pressed', 'true'); }
  });
  // Activate sentiment chip
  if (prefs.default_sentiment) {
    const chip = document.querySelector('#sentiment-chips .chip[data-sentiment="' + CSS.escape(prefs.default_sentiment) + '"]');
    if (chip) { chip.classList.add('active'); chip.setAttribute('aria-pressed', 'true'); }
  }
  updateFilterCounts();
}

async function savePreferences() {
  const auth = getStoredAuth();
  if (!auth) return;

  const cats = Array.from(document.querySelectorAll('#cat-chips .chip.active:not(.cat-parent)'))
    .map(c => c.dataset.cat).filter(Boolean);
  const langs = Array.from(document.querySelectorAll('#lang-chips .chip.active'))
    .map(c => c.dataset.lang).filter(Boolean);
  const sentiments = Array.from(document.querySelectorAll('#sentiment-chips .chip.active'))
    .map(c => c.dataset.sentiment).filter(Boolean);

  try {
    // Read current posting_json_metadata to merge
    const accounts = await hiveRpc('condenser_api.get_accounts', [[auth.username]]);
    const account = accounts?.[0];
    if (!account) { showToast('Could not read account', 'error'); return; }

    let postingMeta = {};
    try { postingMeta = JSON.parse(account.posting_json_metadata || '{}'); } catch(e) {}

    const prefs = {
      default_categories: cats,
      default_languages: langs,
      default_sentiment: sentiments.length === 1 ? sentiments[0] : null,
    };
    postingMeta.combflow = prefs;

    const ops = [['account_update2', {
      account: auth.username,
      json_metadata: '',
      posting_json_metadata: JSON.stringify(postingMeta),
      extensions: [],
    }]];

    if (!window.hive_keychain) {
      showToast('Hive Keychain required', 'error');
      return;
    }
    window.hive_keychain.requestBroadcast(
      auth.username,
      ops,
      'posting',
      (response) => {
        if (response.success) {
          showToast('Preferences saved on-chain', 'success');
        } else {
          showToast('Could not save preferences', 'error');
        }
      }
    );
  } catch(e) {
    showToast('Could not save preferences', 'error');
  }
}

// ── First-login settings modal ──
function isFirstLogin(prefs) {
  if (!prefs) return false;
  return (!prefs.default_categories || prefs.default_categories.length === 0)
      && (!prefs.default_languages || prefs.default_languages.length === 0)
      && !prefs.default_sentiment;
}

// Wire settings modal chip handlers once
let settingsWired = false;
function wireSettingsOnce() {
  if (settingsWired) return;
  settingsWired = true;

  function handleCatClick(e) {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    const container = e.currentTarget;
    if (chip.classList.contains('cat-parent')) {
      const becoming = !chip.classList.contains('active');
      chip.classList.toggle('active', becoming);
      chip.setAttribute('aria-pressed', becoming);
      container.querySelectorAll(`.chip[data-parent="${chip.dataset.cat}"]`).forEach(c => {
        c.classList.toggle('active', becoming);
        c.setAttribute('aria-pressed', becoming);
      });
    } else {
      chip.classList.toggle('active');
      chip.setAttribute('aria-pressed', chip.classList.contains('active'));
      const parentName = chip.dataset.parent;
      if (parentName) {
        const siblings = container.querySelectorAll(`.chip[data-parent="${parentName}"]`);
        const allActive = Array.from(siblings).every(c => c.classList.contains('active'));
        const parentChip = container.querySelector(`.cat-parent[data-cat="${parentName}"]`);
        if (parentChip) {
          parentChip.classList.toggle('active', allActive);
          parentChip.setAttribute('aria-pressed', allActive);
        }
      }
    }
  }

  function handleSimpleChipClick(e) {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    chip.classList.toggle('active');
    chip.setAttribute('aria-pressed', chip.classList.contains('active'));
  }

  document.getElementById('settings-cats').addEventListener('click', handleCatClick);
  document.getElementById('settings-sentiment').addEventListener('click', handleSimpleChipClick);
  document.getElementById('settings-langs').addEventListener('click', handleSimpleChipClick);
}

async function showSettingsModal() {
  const modal = document.getElementById('settings-modal');

  // Fetch saved on-chain defaults
  let savedPrefs = {};
  const auth = getStoredAuth();
  if (auth) {
    try {
      const accounts = await hiveRpc('condenser_api.get_accounts', [[auth.username]]);
      const account = accounts?.[0];
      if (account) {
        let meta = {};
        try { meta = JSON.parse(account.posting_json_metadata || '{}'); } catch(e) {}
        savedPrefs = meta.combflow || {};
      }
    } catch(e) {}
  }
  const savedCats = savedPrefs.default_categories || [];
  const savedLangs = savedPrefs.default_languages || [];
  const savedSentiment = savedPrefs.default_sentiment || null;

  // Populate category chips from existing filter chips
  const settingsCats = document.getElementById('settings-cats');
  settingsCats.innerHTML = '';
  document.querySelectorAll('#cat-chips .cat-group').forEach(group => {
    const clone = document.createElement('div');
    clone.className = 'cat-group';
    group.querySelectorAll('.chip').forEach(chip => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = chip.classList.contains('cat-parent') ? 'chip cat-parent' : 'chip';
      btn.dataset.cat = chip.dataset.cat;
      if (chip.dataset.parent) btn.dataset.parent = chip.dataset.parent;
      const isActive = savedCats.includes(chip.dataset.cat);
      if (isActive) btn.classList.add('active');
      btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
      btn.textContent = chip.textContent;
      clone.appendChild(btn);
    });
    settingsCats.appendChild(clone);
  });

  // Populate language chips from existing filter chips
  const settingsLangs = document.getElementById('settings-langs');
  settingsLangs.innerHTML = '';
  document.querySelectorAll('#lang-chips .chip').forEach(chip => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip';
    btn.dataset.lang = chip.dataset.lang;
    const isActive = savedLangs.includes(chip.dataset.lang);
    if (isActive) btn.classList.add('active');
    btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    btn.textContent = chip.textContent;
    settingsLangs.appendChild(btn);
  });

  // Set sentiment chips in settings modal
  document.querySelectorAll('#settings-sentiment .chip').forEach(c => {
    const isActive = c.dataset.sentiment === savedSentiment;
    c.classList.toggle('active', isActive);
    c.setAttribute('aria-pressed', isActive ? 'true' : 'false');
  });

  // Set vote settings
  const voteFloorInput = document.getElementById('settings-vote-floor');
  const voteMaxInput = document.getElementById('settings-vote-max');
  if (voteFloorInput) {
    const vf = savedPrefs.voteFloor != null ? savedPrefs.voteFloor : 50;
    const vm = savedPrefs.voteMaxWeight != null ? savedPrefs.voteMaxWeight : 25;
    voteFloorInput.value = vf;
    document.getElementById('settings-vote-floor-val').textContent = vf + '%';
    voteMaxInput.value = vm;
    document.getElementById('settings-vote-max-val').textContent = vm + '%';
  }

  // Render muted + followed users
  renderMutedUsersList();
  renderFollowedUsersList();

  wireSettingsOnce();

  modal.classList.add('open');
  trapFocus(modal.querySelector('.modal'));
}

async function saveSettings() {
  const auth = getStoredAuth();
  const cats = Array.from(document.querySelectorAll('#settings-cats .chip.active:not(.cat-parent)'))
    .map(c => c.dataset.cat).filter(Boolean);
  const langs = Array.from(document.querySelectorAll('#settings-langs .chip.active'))
    .map(c => c.dataset.lang).filter(Boolean);
  const sentiments = Array.from(document.querySelectorAll('#settings-sentiment .chip.active'))
    .map(c => c.dataset.sentiment).filter(Boolean);

  // Save on-chain
  if (auth && window.hive_keychain) {
    try {
      const accounts = await hiveRpc('condenser_api.get_accounts', [[auth.username]]);
      const account = accounts?.[0];
      if (account) {
        let postingMeta = {};
        try { postingMeta = JSON.parse(account.posting_json_metadata || '{}'); } catch(e) {}
        const voteFloor = Number(document.getElementById('settings-vote-floor').value);
        const voteMax = Number(document.getElementById('settings-vote-max').value);
        const prefs = {
          default_categories: cats,
          default_languages: langs,
          default_sentiment: sentiments.length === 1 ? sentiments[0] : null,
          voteFloor: voteFloor,
          voteMaxWeight: voteMax,
        };
        // Cache vote prefs locally for immediate use
        localStorage.setItem('honeycomb_voteFloor', voteFloor);
        localStorage.setItem('honeycomb_voteMaxWeight', voteMax);
        postingMeta.combflow = prefs;
        const ops = [['account_update2', {
          account: auth.username,
          json_metadata: '',
          posting_json_metadata: JSON.stringify(postingMeta),
          extensions: [],
        }]];
        window.hive_keychain.requestBroadcast(auth.username, ops, 'posting', (response) => {
          if (response.success) showToast('Preferences saved on-chain', 'success');
          else showToast('Could not save preferences', 'error');
        });
      }
    } catch(e) {
      showToast('Could not save preferences', 'error');
    }
  }

  // Sync selections to the real filter chips
  applyPreferenceFilters({
    default_categories: cats,
    default_languages: langs,
    default_sentiment: sentiments.length === 1 ? sentiments[0] : null,
  });
  updateFilterCounts();

  closeSettingsModal();
  applyFilters();
}

function skipSettings() {
  closeSettingsModal();
}

function closeSettingsModal() {
  const modal = document.getElementById('settings-modal');
  releaseFocus(modal.querySelector('.modal'));
  modal.classList.remove('open');
}

// ── Muted users list (in settings modal) ──
function renderMutedUsersList() {
  const container = document.getElementById('settings-muted');
  if (!container) return;
  if (_mutedUsers.size === 0) {
    container.innerHTML = '<p style="color:var(--text-dim);font-size:13px">No muted users.</p>';
    return;
  }
  container.innerHTML = '';
  _mutedUsers.forEach(user => {
    const item = document.createElement('div');
    item.className = 'muted-user-item';
    item.innerHTML = `<span class="muted-user-name">@${esc(user)}</span><button type="button" class="btn btn-ghost muted-user-unmute" onclick="handleUnmuteUser('${esc(user)}')">Unmute</button>`;
    container.appendChild(item);
  });
}

// ── Copy post link ──
function copyPostLink() {
  const url = window.location.href;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(() => {
      showToast('Link copied', 'success');
    }).catch(() => _fallbackCopy(url));
  } else {
    _fallbackCopy(url);
  }
}
function _fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand('copy');
    showToast('Link copied', 'success');
  } catch { showToast('Could not copy link', 'error'); }
  document.body.removeChild(ta);
}

// ── Community suggestions ──
let _suggestionsAbort = null;
let _suggestionsTimer = null;

function getActiveCategorySlugs() {
  return Array.from(document.querySelectorAll('#cat-chips .chip.active:not(.cat-parent)'))
    .map(c => c.dataset.cat).filter(Boolean);
}

function scheduleSuggestions() {
  clearTimeout(_suggestionsTimer);
  _suggestionsTimer = setTimeout(fetchSuggestions, 200);
}

async function fetchSuggestions() {
  const cats = getActiveCategorySlugs();
  const bar = document.getElementById('suggestions-bar');
  const list = document.getElementById('suggestions-list');

  if (cats.length === 0) {
    bar.style.display = 'none';
    return;
  }

  // Show skeleton while loading
  bar.style.display = '';
  list.innerHTML = '';
  for (let i = 0; i < 3; i++) {
    const sk = document.createElement('div');
    sk.className = 'skeleton suggestion-skeleton';
    list.appendChild(sk);
  }

  if (_suggestionsAbort) _suggestionsAbort.abort();
  _suggestionsAbort = new AbortController();

  try {
    let url = '/api/communities/suggested?';
    cats.forEach(c => url += 'category=' + encodeURIComponent(c) + '&');
    const res = await fetch(url, { signal: _suggestionsAbort.signal });
    const data = await res.json();
    const suggestions = data.suggestions || [];
    if (suggestions.length === 0) {
      bar.style.display = 'none';
      return;
    }
    renderSuggestions(suggestions);
  } catch(e) {
    if (e.name !== 'AbortError') {
      bar.style.display = 'none';
    }
  }
}

function getUserCommunitySet() {
  if (!_userCommunities) return new Set();
  return new Set(_userCommunities.map(c => c.id));
}

function renderSuggestions(suggestions) {
  const list = document.getElementById('suggestions-list');
  list.innerHTML = '';
  const auth = getStoredAuth();
  const memberSet = getUserCommunitySet();

  suggestions.forEach(s => {
    const item = document.createElement('div');
    item.className = 'suggestion-item';
    if (_activeCommunityFilter === s.id) item.classList.add('active');
    item.dataset.communityId = s.id;
    item.style.cursor = 'pointer';
    item.onclick = () => filterByCommunity(s.id);

    const name = document.createElement('span');
    name.className = 'suggestion-name';
    name.textContent = s.name || s.id;
    item.appendChild(name);

    const count = document.createElement('span');
    count.className = 'suggestion-count';
    count.textContent = (s.post_count || 0) + ' posts';
    item.appendChild(count);

    if (auth) {
      const btn = document.createElement('button');
      btn.type = 'button';
      const isMember = memberSet.has(s.id);
      btn.className = 'suggestion-action' + (isMember ? ' joined' : '');
      btn.textContent = isMember ? 'Joined' : 'Join';
      if (!isMember) {
        btn.onclick = (e) => { e.stopPropagation(); handleJoinCommunity(s.id, s.name || s.id, btn); };
      } else {
        btn.onclick = (e) => e.stopPropagation();
      }
      item.appendChild(btn);
    } else {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'suggestion-action';
      btn.textContent = 'Join';
      btn.onclick = (e) => { e.stopPropagation(); showLoginPrompt(); };
      item.appendChild(btn);
    }

    list.appendChild(item);
  });
}

async function handleJoinCommunity(communityId, communityName, btn) {
  btn.disabled = true;
  btn.textContent = 'Joining...';
  try {
    await subscribeCommunity(communityId);
    btn.classList.add('joined');
    btn.textContent = 'Joined';
    btn.onclick = null;
    showToast('Joined ' + communityName + '!', 'success');
    // Update sessionStorage cache
    if (_userCommunities) {
      _userCommunities.push({ id: communityId, name: communityName, role: 'guest' });
      sessionStorage.setItem('honeycomb_user_communities', JSON.stringify(_userCommunities));
    }
  } catch(e) {
    btn.textContent = 'Join';
    showToast(e.message || 'Could not join community', 'error');
  }
  btn.disabled = false;
}

// ── "My Communities" toggle ──
function toggleMyCommunities() {
  setMyCommunitiesActive(!_myCommunitiesActive);
  if (_myCommunitiesActive) {
    setFollowingActive(false);
    _activeCommunityFilter = null;
    updateSuggestionActiveState();
  }
  scheduleFilter();
}

function setMyCommunitiesActive(active) {
  _myCommunitiesActive = active;
  const btn = document.getElementById('my-communities-toggle');
  if (btn) {
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  }
}

// ── "Following" toggle ──
function toggleFollowing() {
  setFollowingActive(!_followingFilterActive);
  if (_followingFilterActive) {
    setMyCommunitiesActive(false);
    _activeCommunityFilter = null;
    updateSuggestionActiveState();
  }
  scheduleFilter();
}

function setFollowingActive(active) {
  _followingFilterActive = active;
  const btn = document.getElementById('following-toggle');
  if (btn) {
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  }
}

// ── Filter by community (from suggestion or badge click) ──
function filterByCommunity(communityId) {
  if (_activeCommunityFilter === communityId) {
    _activeCommunityFilter = null;
  } else {
    _activeCommunityFilter = communityId;
  }
  setMyCommunitiesActive(false);
  updateSuggestionActiveState();
  scheduleFilter();
}

function updateSuggestionActiveState() {
  document.querySelectorAll('.suggestion-item').forEach(item => {
    item.classList.toggle('active', item.dataset.communityId === _activeCommunityFilter);
  });
}

// ── Fetch user communities for editor ──
async function fetchUserCommunities(username) {
  const cached = sessionStorage.getItem('honeycomb_user_communities');
  if (cached) {
    try { return JSON.parse(cached); } catch(e) {}
  }
  const result = await hiveRpc('bridge.list_all_subscriptions', { account: username });
  if (result) {
    const list = result.map(entry => ({
      id: entry[0],
      name: entry[1],
      role: entry[2],
    })).sort((a, b) => a.name.localeCompare(b.name));
    sessionStorage.setItem('honeycomb_user_communities', JSON.stringify(list));
    return list;
  } else {
    return null;
  }
}

function populateEditorCommunities(communities) {
  const select = document.getElementById('editor-community-select');
  // Remove all options except "My Blog"
  while (select.options.length > 1) select.remove(1);
  if (!communities || communities.length === 0) return;
  communities.forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.id;
    opt.textContent = `${c.name} (${c.role})`;
    select.appendChild(opt);
  });
}

function onCommunitySelect() {
  const select = document.getElementById('editor-community-select');
  const crosspostLabel = document.getElementById('editor-crosspost-label');
  if (select.value) {
    crosspostLabel.style.display = '';
  } else {
    crosspostLabel.style.display = 'none';
    document.getElementById('editor-crosspost').checked = false;
  }
  autoSaveDraft();
}

// ── Smart community suggestions based on tags ──
function updateCommunitySuggestion() {
  const hint = document.getElementById('editor-community-hint');
  const select = document.getElementById('editor-community-select');
  if (!_userCommunities || !_userCommunities.length || select.value) {
    hint.style.display = 'none';
    return;
  }
  // Check if any tag matches a community name
  for (const tag of _editorTags) {
    const tagLower = tag.toLowerCase();
    const match = _userCommunities.find(c =>
      c.name.toLowerCase().includes(tagLower) || tagLower.includes(c.name.toLowerCase().split(' ')[0])
    );
    if (match) {
      hint.textContent = `Post to ${match.name}?`;
      hint.onclick = () => {
        select.value = match.id;
        onCommunitySelect();
        hint.style.display = 'none';
      };
      hint.style.display = '';
      return;
    }
  }
  hint.style.display = 'none';
}

// ── Post Editor ──
const DRAFT_KEY = 'honeycomb_draft';
let _editorTags = [];
let _draftTimer = null;
let _categoryLeafs = []; // populated from /categories response

async function openEditor() {
  const auth = getStoredAuth();
  if (!auth) { showLoginPrompt(); return; }
  const modal = document.getElementById('editor-modal');
  // Restore draft
  try {
    const draft = JSON.parse(localStorage.getItem(DRAFT_KEY));
    if (draft) {
      document.getElementById('editor-title').value = draft.title || '';
      document.getElementById('editor-body').value = draft.body || '';
      document.getElementById('editor-description').value = draft.description || '';
      _editorTags = draft.tags || [];
      if (draft.communityId) {
        document.getElementById('editor-community-select').value = draft.communityId;
      }
      if (draft.location) {
        _selectedLocation = draft.location;
        document.getElementById('editor-location-btn').classList.add('has-location');
        _updateLocationBadge();
      }
    }
  } catch(e) {}
  renderEditorTags();
  updateEditorTitleCount();
  updateEditorDescCount();
  showEditorTab('write');
  modal.classList.add('open');
  trapFocus(modal.querySelector('.modal'));

  // Fetch communities in background
  const loading = document.getElementById('editor-community-loading');
  loading.style.display = '';
  _userCommunities = await fetchUserCommunities(auth.username);
  loading.style.display = 'none';
  if (_userCommunities) {
    populateEditorCommunities(_userCommunities);
    // Restore draft community selection
    try {
      const draft = JSON.parse(localStorage.getItem(DRAFT_KEY));
      if (draft && draft.communityId) {
        document.getElementById('editor-community-select').value = draft.communityId;
        onCommunitySelect();
      }
    } catch(e) {}
  }
}

function confirmCloseEditor() {
  const title = document.getElementById('editor-title').value.trim();
  const body = document.getElementById('editor-body').value.trim();
  if (title || body || _editorTags.length > 0) {
    saveDraft();
    showToast('Draft saved', 'info');
  }
  closeEditor();
}

function closeEditor() {
  const modal = document.getElementById('editor-modal');
  releaseFocus(modal.querySelector('.modal'));
  modal.classList.remove('open');
  document.getElementById('editor-tag-suggestions').style.display = 'none';
  document.getElementById('editor-community-hint').style.display = 'none';
}

function updateEditorTitleCount() {
  const input = document.getElementById('editor-title');
  document.getElementById('editor-title-count').textContent = input.value.length + '/256';
}

function updateEditorDescCount() {
  const input = document.getElementById('editor-description');
  document.getElementById('editor-desc-count').textContent = input.value.length + '/120';
}

// ── Editor toolbar actions ──
function editorInsert(before, after) {
  const ta = document.getElementById('editor-body');
  const start = ta.selectionStart, end = ta.selectionEnd;
  const sel = ta.value.substring(start, end);
  const replacement = before + (sel || 'text') + after;
  ta.setRangeText(replacement, start, end, 'select');
  ta.selectionStart = start + before.length;
  ta.selectionEnd = start + before.length + (sel || 'text').length;
  ta.focus();
  autoSaveDraft();
}

function editorInsertLine(prefix) {
  const ta = document.getElementById('editor-body');
  const start = ta.selectionStart;
  // Find start of current line
  const lineStart = ta.value.lastIndexOf('\n', start - 1) + 1;
  const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
  const text = sel || '';
  ta.setRangeText(prefix + text, lineStart === start ? start : start, ta.selectionEnd, 'end');
  ta.focus();
  autoSaveDraft();
}

function editorInsertLink() {
  const ta = document.getElementById('editor-body');
  const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
  const isUrl = /^https?:\/\//.test(sel);
  if (isUrl) {
    editorInsert('[link text](', ')');
  } else {
    editorInsert('[' + (sel || 'link text') + '](', ')');
    if (sel) {
      // Place cursor inside the url part
      const pos = ta.selectionEnd - 1;
      ta.selectionStart = pos;
      ta.selectionEnd = pos;
    }
  }
}

function editorInsertImage() {
  const ta = document.getElementById('editor-body');
  const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
  const isUrl = /^https?:\/\//.test(sel);
  if (isUrl) {
    editorInsert('![](', ')');
  } else {
    editorInsert('![' + (sel || 'alt text') + '](', ')');
    if (sel) {
      const pos = ta.selectionEnd - 1;
      ta.selectionStart = pos;
      ta.selectionEnd = pos;
    }
  }
}

function editorInsertTable() {
  editorInsert('\n| Column 1 | Column 2 | Column 3 |\n|----------|----------|----------|\n| ', ' |  |  |\n');
}

// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
  const ta = document.getElementById('editor-body');
  if (!ta || document.activeElement !== ta) return;
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey) {
    if (e.key === 'b') { e.preventDefault(); editorInsert('**', '**'); }
    else if (e.key === 'i') { e.preventDefault(); editorInsert('*', '*'); }
    else if (e.key === 'k') { e.preventDefault(); editorInsertLink(); }
  }
});

function showMarkdownHelp() {
  document.getElementById('md-help-modal').classList.add('open');
  trapFocus(document.querySelector('#md-help-modal .modal'));
}
function closeMdHelp() {
  const modal = document.getElementById('md-help-modal');
  releaseFocus(modal.querySelector('.modal'));
  modal.classList.remove('open');
}

function showEditorTab(tab) {
  const writeTab = document.getElementById('editor-tab-write');
  const previewTab = document.getElementById('editor-tab-preview');
  const textarea = document.getElementById('editor-body');
  const preview = document.getElementById('editor-preview');
  const toolbar = document.getElementById('editor-toolbar');
  if (tab === 'preview') {
    preview.innerHTML = renderHiveBody(textarea.value || '');
    preview.style.display = '';
    textarea.style.display = 'none';
    toolbar.style.display = 'none';
    previewTab.classList.add('active');
    writeTab.classList.remove('active');
  } else {
    preview.style.display = 'none';
    textarea.style.display = '';
    toolbar.style.display = '';
    writeTab.classList.add('active');
    previewTab.classList.remove('active');
  }
}

function renderEditorTags() {
  const list = document.getElementById('editor-tags-list');
  list.innerHTML = '';
  _editorTags.forEach((tag, i) => {
    const el = document.createElement('span');
    el.className = 'editor-tag';
    el.innerHTML = esc(tag) + '<button type="button" onclick="removeEditorTag(' + i + ')" aria-label="Remove tag">&times;</button>';
    list.appendChild(el);
  });
}

function addEditorTag(tag) {
  tag = tag.toLowerCase().replace(/[^a-z0-9-]/g, '').slice(0, 50);
  if (!tag || _editorTags.includes(tag) || _editorTags.length >= 10) return;
  _editorTags.push(tag);
  renderEditorTags();
  document.getElementById('editor-tags-input').value = '';
  document.getElementById('editor-tag-suggestions').style.display = 'none';
  autoSaveDraft();
  updateCommunitySuggestion();
}

function removeEditorTag(i) {
  _editorTags.splice(i, 1);
  renderEditorTags();
  autoSaveDraft();
  updateCommunitySuggestion();
}

function handleTagKey(e) {
  if (e.key === 'Enter' || e.key === ',') {
    e.preventDefault();
    const val = e.target.value.trim().replace(/,$/,'');
    if (val) addEditorTag(val);
  }
}

function showTagSuggestions() {
  const input = document.getElementById('editor-tags-input');
  const sugBox = document.getElementById('editor-tag-suggestions');
  const q = input.value.trim().toLowerCase();
  if (!q || q.length < 2) { sugBox.style.display = 'none'; return; }
  const matches = _categoryLeafs.filter(c =>
    !_editorTags.includes(c) && (c.startsWith(q) || c.includes(q))
  ).slice(0, 8);
  if (!matches.length) { sugBox.style.display = 'none'; return; }
  sugBox.innerHTML = '';
  matches.forEach(m => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'editor-tag-suggestion';
    btn.textContent = m;
    btn.onclick = () => addEditorTag(m);
    sugBox.appendChild(btn);
  });
  sugBox.style.display = '';
}

// ── Location Picker ──
let _leafletLoaded = false;
let _locationMap = null;
let _locationMarker = null;
let _selectedLocation = null; // {lat, lng}

function _loadLeaflet() {
  if (_leafletLoaded) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
    document.head.appendChild(link);
    const script = document.createElement('script');
    script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
    script.onload = () => { _leafletLoaded = true; resolve(); };
    script.onerror = () => reject(new Error('Failed to load Leaflet'));
    document.head.appendChild(script);
  });
}

async function openLocationPicker() {
  const modal = document.getElementById('location-modal');
  modal.classList.add('open');
  trapFocus(modal.querySelector('.modal'));
  try {
    await _loadLeaflet();
  } catch(e) {
    showToast('Could not load map library', 'error');
    closeLocationPicker();
    return;
  }
  if (!_locationMap) {
    _locationMap = L.map('location-map').setView([20, 0], 2);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors',
      maxZoom: 19,
    }).addTo(_locationMap);
    _locationMap.on('click', function(e) {
      _placeMarker(e.latlng.lat, e.latlng.lng);
    });
  }
  setTimeout(() => _locationMap.invalidateSize(), 100);
  // Restore existing location if set
  if (_selectedLocation) {
    _placeMarker(_selectedLocation.lat, _selectedLocation.lng);
    document.getElementById('location-description').value = _selectedLocation.description || '';
  }
}

function _placeMarker(lat, lng) {
  if (_locationMarker) {
    _locationMarker.setLatLng([lat, lng]);
  } else {
    _locationMarker = L.marker([lat, lng]).addTo(_locationMap);
  }
  _selectedLocation = { lat, lng, description: (_selectedLocation && _selectedLocation.description) || '' };
  document.getElementById('location-coords').textContent = `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
  document.getElementById('location-confirm-btn').disabled = false;
  _reverseGeocode(lat, lng);
}

let _locationAutoFilled = false;

function _reverseGeocode(lat, lng) {
  const descEl = document.getElementById('location-description');
  fetch(`https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lng}&format=json&zoom=14&accept-language=en`, {
    headers: { 'User-Agent': 'HoneyComb/1.0' }
  }).then(r => r.json()).then(data => {
    if (!data.address) return;
    const a = data.address;
    const parts = [a.city || a.town || a.village || a.hamlet || '', a.country || ''].filter(Boolean);
    const guess = parts.join(', ');
    if (guess && (!descEl.value.trim() || _locationAutoFilled)) {
      descEl.value = guess;
      _locationAutoFilled = true;
    }
  }).catch(() => {});
}

function useMyLocation() {
  if (!navigator.geolocation) {
    showToast('Geolocation not supported by your browser', 'error');
    return;
  }
  const btn = document.getElementById('location-myloc-btn');
  btn.disabled = true;
  btn.textContent = 'Locating...';
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      _placeMarker(pos.coords.latitude, pos.coords.longitude);
      _locationMap.setView([pos.coords.latitude, pos.coords.longitude], 14);
      btn.disabled = false;
      btn.innerHTML = '&#x1F4CD; My Location';
    },
    (err) => {
      if (err.code === 1) showToast('Location access denied', 'error');
      else showToast('Could not determine location', 'error');
      btn.disabled = false;
      btn.innerHTML = '&#x1F4CD; My Location';
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
}

function confirmLocation() {
  if (!_selectedLocation) return;
  _selectedLocation.description = document.getElementById('location-description').value.trim() || 'location';
  // Insert/replace worldmappin tag in body
  const bodyEl = document.getElementById('editor-body');
  const tag = `[//]:# (!worldmappin ${_selectedLocation.lat.toFixed(5)} lat ${_selectedLocation.lng.toFixed(5)} long ${_selectedLocation.description} d3scr)`;
  const wmRegex = /\[\/\/\]:#\s*\(!worldmappin\s+[\d.-]+\s+lat\s+[\d.-]+\s+long\s+.+?\s+d3scr\)/;
  if (wmRegex.test(bodyEl.value)) {
    bodyEl.value = bodyEl.value.replace(wmRegex, tag);
  } else {
    bodyEl.value = bodyEl.value.trimEnd() + '\n\n' + tag;
  }
  _updateLocationBadge();
  document.getElementById('editor-location-btn').classList.add('has-location');
  autoSaveDraft();
  closeLocationPicker();
}

function _updateLocationBadge() {
  const badge = document.getElementById('editor-location-badge');
  if (_selectedLocation) {
    badge.innerHTML = `&#x1F4CD; ${esc(_selectedLocation.description || 'Location set')} <span class="remove-location" onclick="event.stopPropagation();removeLocation()" title="Remove location">&times;</span>`;
    badge.style.display = '';
    badge.onclick = (e) => { if (!e.target.classList.contains('remove-location')) openLocationPicker(); };
  } else {
    badge.style.display = 'none';
  }
}

function removeLocation() {
  _selectedLocation = null;
  _locationAutoFilled = false;
  if (_locationMarker) {
    _locationMap.removeLayer(_locationMarker);
    _locationMarker = null;
  }
  document.getElementById('editor-location-btn').classList.remove('has-location');
  document.getElementById('location-coords').textContent = '';
  document.getElementById('location-confirm-btn').disabled = true;
  // Remove worldmappin tag from body
  const bodyEl = document.getElementById('editor-body');
  bodyEl.value = bodyEl.value.replace(/\n*\[\/\/\]:#\s*\(!worldmappin\s+[\d.-]+\s+lat\s+[\d.-]+\s+long\s+.+?\s+d3scr\)/, '');
  _updateLocationBadge();
  autoSaveDraft();
}

function closeLocationPicker() {
  const modal = document.getElementById('location-modal');
  releaseFocus(modal.querySelector('.modal'));
  modal.classList.remove('open');
}

function saveDraft() {
  localStorage.setItem(DRAFT_KEY, JSON.stringify({
    title: document.getElementById('editor-title').value,
    body: document.getElementById('editor-body').value,
    description: document.getElementById('editor-description').value,
    tags: _editorTags,
    communityId: document.getElementById('editor-community-select').value || null,
    location: _selectedLocation || null,
  }));
}

function autoSaveDraft() {
  clearTimeout(_draftTimer);
  _draftTimer = setTimeout(saveDraft, 10000);
}

function clearDraft() {
  localStorage.removeItem(DRAFT_KEY);
  clearTimeout(_draftTimer);
}

async function publishPost() {
  const title = document.getElementById('editor-title').value.trim();
  const body = document.getElementById('editor-body').value.trim();
  const description = document.getElementById('editor-description').value.trim();
  const communityId = document.getElementById('editor-community-select').value || null;
  const crossPost = communityId && document.getElementById('editor-crosspost').checked;
  const btn = document.getElementById('editor-publish-btn');

  if (!title) { showToast('Title is required', 'error'); return; }
  if (!body) { showToast('Body is required', 'error'); return; }

  btn.disabled = true;
  btn.textContent = 'Publishing...';
  try {
    const result = await broadcastPost(title, body, _editorTags, communityId, description);
    showToast('Post published!', 'success');

    // Cross-post to blog if requested
    if (crossPost) {
      try {
        await broadcastCrossPost(result.author, result.permlink, communityId);
        showToast('Cross-posted to my blog', 'success');
      } catch(e) {
        showToast('Post published to community. Cross-post failed \u2014 you can reblog manually.', 'info', 5000);
      }
    }

    clearDraft();
    document.getElementById('editor-title').value = '';
    document.getElementById('editor-body').value = '';
    document.getElementById('editor-description').value = '';
    document.getElementById('editor-community-select').value = '';
    document.getElementById('editor-crosspost').checked = false;
    document.getElementById('editor-crosspost-label').style.display = 'none';
    document.getElementById('editor-community-hint').style.display = 'none';
    _selectedLocation = null;
  _locationAutoFilled = false;
    if (_locationMarker && _locationMap) { _locationMap.removeLayer(_locationMarker); _locationMarker = null; }
    document.getElementById('editor-location-btn').classList.remove('has-location');
    _updateLocationBadge();
    _editorTags = [];
    renderEditorTags();
    closeEditor();
    // Navigate to the new post
    showToast('Your post will appear in the feed once processed', 'info', 5000);
    history.pushState({ author: result.author, permlink: result.permlink },
      '', `/@${result.author}/${result.permlink}`);
    openModal({ author: result.author, permlink: result.permlink }, true);
  } catch(e) {
    showToast(e.message || 'Could not publish post', 'error');
  }
  btn.disabled = false;
  btn.textContent = 'Publish';
}

// Warn on navigation if draft exists
window.addEventListener('beforeunload', e => {
  const title = document.getElementById('editor-title');
  const body = document.getElementById('editor-body');
  if (document.getElementById('editor-modal').classList.contains('open') &&
      (title.value.trim() || body.value.trim())) {
    saveDraft();
    e.preventDefault();
  }
});

// ── Back to top ──
window.addEventListener('scroll', () => {
  const btn = document.getElementById('back-to-top');
  btn.classList.toggle('visible', window.scrollY > 600);
}, { passive: true });

init().then(startLiveUpdates);
