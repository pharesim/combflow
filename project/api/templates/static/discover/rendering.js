// ── Layout toggle ──
function setLayout(mode) {
  state.layoutMode = mode;
  Alpine.store('app').layoutMode = mode;
  localStorage.setItem('combflow_layout', mode);
  document.querySelectorAll('#layout-toggle button').forEach(b => {
    const active = b.dataset.layout === mode;
    b.classList.toggle('active', active);
    b.setAttribute('aria-pressed', active);
  });
  const effective = getEffectiveLayout();
  // Recompute hex positions if switching to hex (defer to let Alpine show the container first)
  if (effective === 'hex') {
    requestAnimationFrame(() => syncHexPositions(Alpine.store('app').posts.length));
  }
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
    // Remove only non-template children (preserve Alpine x-for templates)
    cardGrid.querySelectorAll(':scope > :not(template)').forEach(el => el.remove());
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
    // Remove only non-template children (preserve Alpine x-for templates)
    hexGrid.querySelectorAll(':scope > :not(template)').forEach(el => el.remove());
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

// ── Batch fetch titles + thumbnails from Hive (parallel with concurrency cap) ──
function cacheMetaEntry(key, entry) {
  if (!state.metaCache[key]) {
    state.metaCacheKeys.push(key);
    if (state.metaCacheKeys.length > META_CACHE_MAX) {
      const old = state.metaCacheKeys.shift();
      delete state.metaCache[old];
    }
  }
  state.metaCache[key] = entry;
}

async function fetchSingleMeta(p, retries = 2) {
  const key = `${p.author}/${p.permlink}`;
  let result;
  for (let attempt = 0; attempt <= retries; attempt++) {
    result = await hiveRpc('bridge.get_post', {author:p.author, permlink:p.permlink});
    if (result) break;
    if (attempt < retries) await new Promise(r => setTimeout(r, 1000 * (attempt + 1)));
  }
  if (result) {
    let images = (result.json_metadata?.image || []).map(u =>
      u.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/[\])].*$/, ''));
    if (!images.length && result.body) {
      const body = result.body;
      // Collect all candidate image URLs from the body
      const candidates = [];
      // Markdown images: ![alt](url)
      for (const m of body.matchAll(/!\[[^\]]*\]\(([^)]+)\)/g)) candidates.push(m[1]);
      // HTML img tags
      for (const m of body.matchAll(/<img[^>]+src=["']([^"']+)["']/gi)) candidates.push(m[1]);
      // Bare image URLs on their own line (common Hive pattern)
      for (const m of body.matchAll(/^\s*(https?:\/\/[^\s"'<>]+\.(?:jpg|jpeg|png|gif|webp|svg))\s*$/gim)) candidates.push(m[1]);
      // Known Hive image hosts anywhere in text
      const hostRe = /https?:\/\/(?:files\.peakd\.com|images\.ecency\.com|images\.hive\.blog|usermedia\.actifit\.io|images\.3speak\.tv|cdn\.steemitimages\.com|img\.leopedia\.io)\/[^\s"'<>)]+/gi;
      for (const m of body.matchAll(hostRe)) candidates.push(m[0]);
      // URLs with image extensions anywhere
      for (const m of body.matchAll(/https?:\/\/[^\s"'<>)]+\.(?:jpg|jpeg|png|gif|webp|svg)/gi)) candidates.push(m[0]);
      // Prefer non-gif, then fall back to gif
      const nonGif = candidates.find(u => !/\.gif$/i.test(u));
      const found = nonGif || candidates[0] || '';
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
    // Bump Alpine metaRev so reactive templates re-evaluate
    Alpine.store('app').metaRev++;
  }
}

function seedMetaFromServer(posts) {
  for (const p of posts) {
    if (!p.title) continue;
    const key = `${p.author}/${p.permlink}`;
    if (state.metaCache[key]) continue;
    cacheMetaEntry(key, { title: p.title, thumbnail: '' });
  }
}

async function fetchMeta(posts) {
  if (!posts.length) return;
  const need = posts.filter(p => {
    const cached = state.metaCache[`${p.author}/${p.permlink}`];
    return !cached || !cached.thumbnail;
  });
  const chunks = [];
  for (let i = 0; i < need.length; i += 5) chunks.push(need.slice(i, i + 5));

  const CONCURRENCY = 2;
  let ci = 0;
  async function runNext() {
    if (ci >= chunks.length) return;
    const chunk = chunks[ci++];
    await Promise.all(chunk.map(p => fetchSingleMeta(p)));
    await new Promise(r => setTimeout(r, 200));
    return runNext();
  }
  await Promise.all(Array.from({ length: CONCURRENCY }, () => runNext()));
}

// ── Alpine template helper: get post metadata ──
// Called from x-bind expressions in templates. Reads metaRev to subscribe to updates.
function getPostMeta(p) {
  // Touch metaRev so Alpine re-evaluates when meta changes
  void Alpine.store('app').metaRev;
  const key = `${p.author}/${p.permlink}`;
  const cached = state.metaCache[key];
  const title = (cached && cached.title) || p.permlink.replace(/-/g, ' ').slice(0, 60);
  const thumb = cached ? thumbUrl(cached.thumbnail) : '';
  const borderColor = sentimentColor(p.sentiment_score);
  const catLabel = (p.categories || []).slice(0, 2);
  const voted = state.votedPosts[key];
  return { key, title, thumb, borderColor, catLabel, voted };
}

// ── Alpine template helper: build tags HTML for card/hex views ──
function getPostTagsHtml(p) {
  void Alpine.store('app').metaRev;
  let html = '';
  if (p.community_name) {
    html += `<span class="community-badge" data-community="${esc(p.community_id)}" onclick="event.preventDefault();event.stopPropagation();filterByCommunity('${esc(p.community_id)}')" title="${esc(p.community_name)}">${esc(p.community_name)}</span>`;
  }
  (p.categories || []).slice(0, 2).forEach(c => { html += `<span class="tag">${esc(c)}</span>`; });
  if (p.sentiment && safeSentiment(p.sentiment)) {
    html += `<span class="tag sentiment-${safeSentiment(p.sentiment)}">${esc(p.sentiment)}</span>`;
  }
  (p.languages || []).forEach(lang => {
    html += `<span class="tag">${esc(lang.toUpperCase())}</span>`;
  });
  return html;
}

// ── Alpine template helper: build simpler tags HTML for list view ──
function getListTagsHtml(p) {
  void Alpine.store('app').metaRev;
  let html = '';
  if (p.community_name) {
    html += `<span class="community-badge">${esc(p.community_name)}</span>`;
  }
  (p.categories || []).slice(0, 2).forEach(c => { html += `<span class="tag">${esc(c)}</span>`; });
  return html;
}

// ── Centralized vote button updater ──
function updateVoteButtons(key, voted) {
  const btns = document.querySelectorAll(`.vote-btn[data-vote-key="${CSS.escape(key)}"]`);
  btns.forEach(btn => {
    btn.classList.toggle('voted', !!voted);
    btn.setAttribute('aria-label', voted ? 'Voted' : 'Vote');
  });
}

// ── Infinite scroll ──
function setupInfiniteScroll() {
  const sentinel = document.getElementById('scroll-sentinel');
  const observer = new IntersectionObserver(entries => {
    if (entries[0].isIntersecting && !state.loadingMore && !state.noMorePosts) {
      loadMore();
    }
  }, { rootMargin: '400px' });
  observer.observe(sentinel);
}

async function loadMore() {
  state.loadingMore = true;
  document.getElementById('loading-more').style.display = 'block';

  const { url, sentiments } = buildFilterUrl(PAGE_SIZE, state.currentOffset);
  const fetchUrl = state.lastCursor ? url + `&cursor=${encodeURIComponent(state.lastCursor)}` : url;

  try {
    const res = await fetch(fetchUrl);
    const data = await res.json();
    let newPosts = data.posts || [];
    const serverCount = newPosts.length;
    if (sentiments.length > 1) {
      newPosts = newPosts.filter(p => sentiments.includes(p.sentiment));
    }
    newPosts = filterMutedPosts(newPosts);
    state.lastCursor = data.next_cursor || null;
    if (serverCount < PAGE_SIZE) state.noMorePosts = true;
    if (newPosts.length > 0) {
      seedMetaFromServer(newPosts);
      state.posts = state.posts.concat(newPosts);
      state.currentOffset = state.posts.length;
      syncPostsToStore();
      updateResultsBar();
      fetchMeta(newPosts);
    }
  } catch(e) {
    console.error(e);
  }

  document.getElementById('loading-more').style.display = 'none';
  state.loadingMore = false;
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

// ── Sync hex positions to Alpine store ──
function syncHexPositions(count) {
  if (count === 0) {
    const s = Alpine.store('app');
    s.hexPositions = [];
    s.hexGridW = 0;
    s.hexGridH = 0;
    // Set auto for empty state
    const grid = document.getElementById('hex-grid');
    if (grid) { grid.style.width = 'auto'; grid.style.height = 'auto'; }
    return;
  }
  const { positions, gridW, maxY } = computeHexLayout(count);
  const s = Alpine.store('app');
  s.hexPositions = positions;
  s.hexGridW = gridW;
  s.hexGridH = maxY;
}

// ── Sync state.posts to Alpine store (triggers reactive re-render) ──
function syncPostsToStore() {
  const s = Alpine.store('app');
  // Copy array so Alpine detects the change
  s.posts = state.posts.slice();
  // Recompute hex positions for current post count.
  // The hex container must be visible (via showSkeletons or x-show) for
  // clientWidth to be correct. callers switching layout should defer separately.
  const effective = getEffectiveLayout();
  if (effective === 'hex') {
    syncHexPositions(state.posts.length);
  }
}

// ── Unified render dispatcher ──
function renderAll(posts, fullRebuild) {
  const layout = getEffectiveLayout();
  const s = Alpine.store('app');

  // Sync layout mode to store (controls x-show on containers)
  s.layoutMode = layout;

  if (fullRebuild) {
    // Clear skeletons from hex-grid and card-grid (initial load)
    const hexGrid = document.getElementById('hex-grid');
    const cardGrid = document.getElementById('card-grid');
    hexGrid.querySelectorAll('.skeleton').forEach(el => el.remove());
    cardGrid.querySelectorAll('.skeleton').forEach(el => el.remove());

    // Sync posts to Alpine store — Alpine x-for handles DOM rendering
    syncPostsToStore();
    s.initialLoaded = true;
  }
}

// ── Observe thumbnails after Alpine renders new elements ──
// Called from x-effect on thumbnail elements in the template.
// Safe to call multiple times — only observes if data-thumb is set
// and background-image hasn't been applied yet.
function observeThumb(el) {
  if (el && el.dataset && el.dataset.thumb && !el.style.backgroundImage) {
    thumbObserver.observe(el);
  }
}

// ── Live update: poll for new posts (visibility-aware) ──
let liveTimer = null;
const LIVE_INTERVAL = 30000;

function hasActiveFilters() {
  const f = Alpine.store('filters');
  return f.categories.size > 0 || f.languages.size > 0 || f.sentiments.size > 0
    || state.activeCommunityFilter || state.myCommunitiesActive
    || state.followingFilterActive || state.myPostsActive || state.authorFilterUser;
}

async function pollNewPosts() {
  if (state.deepLinked || hasActiveFilters() || !state.newestCreated) return;
  try {
    const [browseData, statsData] = await Promise.all([
      fetch('/api/browse?limit=10').then(r => r.json()),
      fetch('/api/stats').then(r => r.json()),
    ]);
    state.totalPostCount = statsData.total_posts || state.totalPostCount;
    const fresh = filterMutedPosts((browseData.posts || []).filter(p =>
      p.created > state.newestCreated && !state.posts.some(e => e.id === p.id)
    ));
    if (fresh.length > 0) {
      seedMetaFromServer(fresh);
      state.posts = fresh.concat(state.posts);
      // Cap state.posts to prevent unbounded growth
      if (state.posts.length > ALL_POSTS_MAX) {
        state.posts = state.posts.slice(0, ALL_POSTS_MAX);
      }
      state.newestCreated = state.posts[0].created;
      renderAll(state.posts, true);
      updateResultsBar();
      fetchMeta(fresh);
    }
  } catch(e) {}
}

function startLiveUpdates() {
  liveTimer = setInterval(pollNewPosts, LIVE_INTERVAL);
}
