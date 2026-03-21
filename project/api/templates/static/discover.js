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
let _userCommunities = null; // from bridge.list_all_subscriptions (for editor)

// Endless scrolling state
const PAGE_SIZE = 60;

// Hive RPC nodes with automatic fallback
const HIVE_NODES = ['https://api.hive.blog', 'https://api.deathwing.me', 'https://rpc.ausbit.dev'];
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
  // 3Speak and YouTube CDN URLs don't work through the Hive image proxy
  if (/3speak|img\.youtube\.com/.test(url)) return url;
  // Strip existing hive image proxy prefix to avoid double-wrapping
  const proxyRe = /^https?:\/\/images\.hive\.blog\/\d+x\d+\//;
  const raw = url.replace(proxyRe, '');
  return `https://images.hive.blog/${size}x0/${raw}`;
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
  const bar = document.getElementById('results-bar');
  const hasFilters = document.querySelectorAll('.chip.active').length > 0;
  if (hasFilters || allPosts.length > 0) {
    bar.style.display = '';
    const filterLabel = hasFilters ? ' (filtered)' : '';
    const displayTotal = hasFilters ? filteredTotalCount : totalPostCount;
    bar.textContent = `Showing ${allPosts.length.toLocaleString()} of ${displayTotal.toLocaleString()} posts${filterLabel}`;
  } else {
    bar.style.display = 'none';
  }
}

// ── Init ──
async function init() {
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
  (langsRes.languages||[]).slice(0, 20).forEach(l => {
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
  await loadAndApplyPreferences();

  // Fetch suggestions based on active categories (from preferences or manual)
  scheduleSuggestions();

  // If preferences activated filters, re-fetch with those filters
  if (document.querySelectorAll('.chip.active').length > 0) {
    await applyFilters();
  } else {
    allPosts = postsRes.posts || [];
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
  if (_activeCommunityFilter) url += `&community=${encodeURIComponent(_activeCommunityFilter)}`;
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
      const hostRe = /https?:\/\/(?:files\.peakd\.com|images\.ecency\.com|images\.hive\.blog|usermedia\.actifit\.io|images\.3speak\.tv|cdn\.steemitimages\.com)\/\S+/i;
      const extRe = /https?:\/\/\S+\.(?:jpg|jpeg|png|gif|webp|svg)/i;
      const found = mdMatch ? mdMatch[1] : ((body.match(hostRe) || body.match(extRe) || [])[0] || '');
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
    cacheMetaEntry(key, {
      title: result.title || '',
      thumbnail: images.length ? images[0] : '',
    });
    updatePostElement(key);
  }
}

function seedMetaFromServer(posts) {
  for (const p of posts) {
    if (!p.title && !p.thumbnail_url) continue;
    const key = `${p.author}/${p.permlink}`;
    if (metaCache[key]) continue;
    cacheMetaEntry(key, {
      title: p.title || '',
      thumbnail: p.thumbnail_url || '',
    });
  }
}

async function fetchMeta(posts) {
  if (!posts.length) return;
  const need = posts.filter(p => !metaCache[`${p.author}/${p.permlink}`]);
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
    card.innerHTML = `
      <div class="post-card-thumb${thumb ? '' : ' no-thumb'}" ${thumb ? `data-thumb="${safeCssUrl(thumb)}"` : `style="${noThumbStyle}"`}>
        ${thumb ? '' : `<span>@${esc(p.author)}</span>`}
      </div>
      <div class="post-card-body">
        <div class="post-card-title">${esc(title)}</div>
        <div class="post-card-author">@${esc(p.author)}${p.created ? ' · ' + new Date(p.created).toLocaleDateString('en', {month:'short',day:'numeric'}) : ''}</div>
        <div class="post-card-meta">${tagsHtml}</div>
      </div>`;
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

    row.innerHTML = `
      <div class="list-thumb" ${thumb ? `data-thumb="${safeCssUrl(thumb)}"` : ''}>
        ${thumb ? '' : `<span>@${esc(p.author).slice(0,2)}</span>`}
      </div>
      <div class="list-content">
        <div class="list-title">${esc(title)}</div>
        <div class="list-meta">@${esc(p.author)} · ${p.created ? new Date(p.created).toLocaleDateString('en', {month:'short',day:'numeric'}) : ''} ${tagsHtml}</div>
      </div>`;
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
        : `<div class="hex-placeholder">@${esc(p.author)}</div>`
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
  return document.querySelectorAll('.chip.active').length > 0;
}

async function pollNewPosts() {
  if (_deepLinked || hasActiveFilters() || !newestCreated) return;
  try {
    const [browseData, statsData] = await Promise.all([
      fetch('/api/browse?limit=10').then(r => r.json()),
      fetch('/api/stats').then(r => r.json()),
    ]);
    totalPostCount = statsData.total_posts || totalPostCount;
    const fresh = (browseData.posts || []).filter(p =>
      p.created > newestCreated && !allPosts.some(e => e.id === p.id)
    );
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
  document.getElementById('modal-author').textContent = `@${post.author}`;
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
  document.getElementById('modal-peakd-link').href = `https://peakd.com/@${post.author}/${post.permlink}`;
  document.getElementById('modal-ecency-link').href = `https://ecency.com/@${post.author}/${post.permlink}`;
  document.getElementById('modal-hiveblog-link').href = `https://hive.blog/@${post.author}/${post.permlink}`;
  const modalEl = document.getElementById('modal');
  modalEl.classList.add('open');
  trapFocus(modalEl.querySelector('.modal'));

  // Fetch comments in parallel with post body
  fetchComments(post.author, post.permlink);

  const result = await hiveRpc('bridge.get_post', {author: post.author, permlink: post.permlink});
  if (result) {
    document.getElementById('modal-title').textContent = result.title || post.permlink;
    document.getElementById('modal-body').innerHTML = renderHiveBody(result.body || '');
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
      '<button type="button" class="auth-login" onclick="openEditor()" style="background:var(--hive-red);color:#fff;border-color:var(--hive-red)">Write Post</button>' +
      '<span class="auth-user">@' + esc(auth.username) + '</span>' +
      '<a class="auth-settings" href="#" onclick="showSettingsModal();return false" title="Filter preferences">Settings</a>' +
      '<a class="auth-logout" href="#" onclick="doLogout();return false">Logout</a>';
    document.getElementById('btn-save-prefs').style.display = '';
  } else {
    area.innerHTML = '<a class="auth-login" href="#" onclick="showLoginPrompt();return false">Login</a>';
    document.getElementById('btn-save-prefs').style.display = 'none';
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
  _userCommunities = null;
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
    let jsonMeta = {};
    try { jsonMeta = JSON.parse(account.json_metadata || '{}'); } catch(e) {}

    const prefs = {
      default_categories: cats,
      default_languages: langs,
      default_sentiment: sentiments.length === 1 ? sentiments[0] : null,
    };
    postingMeta.combflow = prefs;
    if (!jsonMeta.combflow) jsonMeta.combflow = prefs;

    const ops = [['account_update2', {
      account: auth.username,
      json_metadata: JSON.stringify(jsonMeta),
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

function showSettingsModal() {
  const modal = document.getElementById('settings-modal');

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
      btn.setAttribute('aria-pressed', 'false');
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
    btn.setAttribute('aria-pressed', 'false');
    btn.textContent = chip.textContent;
    settingsLangs.appendChild(btn);
  });

  // Reset sentiment chips in settings modal
  document.querySelectorAll('#settings-sentiment .chip').forEach(c => {
    c.classList.remove('active');
    c.setAttribute('aria-pressed', 'false');
  });

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
        let jsonMeta = {};
        try { jsonMeta = JSON.parse(account.json_metadata || '{}'); } catch(e) {}
        const prefs = {
          default_categories: cats,
          default_languages: langs,
          default_sentiment: sentiments.length === 1 ? sentiments[0] : null,
        };
        postingMeta.combflow = prefs;
        if (!jsonMeta.combflow) jsonMeta.combflow = prefs;
        const ops = [['account_update2', {
          account: auth.username,
          json_metadata: JSON.stringify(jsonMeta),
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

// ── Copy post link ──
function copyPostLink() {
  const url = window.location.href;
  navigator.clipboard.writeText(url).then(() => {
    showToast('Link copied to clipboard', 'success');
  }).catch(() => {
    showToast('Could not copy link', 'error');
  });
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

// ── Filter by community (from suggestion or badge click) ──
function filterByCommunity(communityId) {
  if (_activeCommunityFilter === communityId) {
    _activeCommunityFilter = null;
  } else {
    _activeCommunityFilter = communityId;
  }
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
      _editorTags = draft.tags || [];
      document.getElementById('editor-decline-payout').checked = draft.declinePayout || false;
      if (draft.communityId) {
        document.getElementById('editor-community-select').value = draft.communityId;
      }
    }
  } catch(e) {}
  renderEditorTags();
  updateEditorTitleCount();
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

function showEditorTab(tab) {
  const writeTab = document.getElementById('editor-tab-write');
  const previewTab = document.getElementById('editor-tab-preview');
  const textarea = document.getElementById('editor-body');
  const preview = document.getElementById('editor-preview');
  if (tab === 'preview') {
    preview.innerHTML = renderHiveBody(textarea.value || '');
    preview.style.display = '';
    textarea.style.display = 'none';
    previewTab.classList.add('active');
    writeTab.classList.remove('active');
  } else {
    preview.style.display = 'none';
    textarea.style.display = '';
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

function saveDraft() {
  localStorage.setItem(DRAFT_KEY, JSON.stringify({
    title: document.getElementById('editor-title').value,
    body: document.getElementById('editor-body').value,
    tags: _editorTags,
    declinePayout: document.getElementById('editor-decline-payout').checked,
    communityId: document.getElementById('editor-community-select').value || null,
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
  const decline = document.getElementById('editor-decline-payout').checked;
  const communityId = document.getElementById('editor-community-select').value || null;
  const crossPost = communityId && document.getElementById('editor-crosspost').checked;
  const btn = document.getElementById('editor-publish-btn');

  if (!title) { showToast('Title is required', 'error'); return; }
  if (!body) { showToast('Body is required', 'error'); return; }

  btn.disabled = true;
  btn.textContent = 'Publishing...';
  try {
    const result = await broadcastPost(title, body, _editorTags, decline, communityId);
    showToast('Post published!', 'success');

    // Cross-post to blog if requested
    if (crossPost) {
      try {
        await broadcastCrossPost(result.author, result.permlink, communityId);
        showToast('Cross-posted to your blog', 'success');
      } catch(e) {
        showToast('Post published to community. Cross-post failed \u2014 you can reblog manually.', 'info', 5000);
      }
    }

    clearDraft();
    document.getElementById('editor-title').value = '';
    document.getElementById('editor-body').value = '';
    document.getElementById('editor-community-select').value = '';
    document.getElementById('editor-crosspost').checked = false;
    document.getElementById('editor-crosspost-label').style.display = 'none';
    document.getElementById('editor-community-hint').style.display = 'none';
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
