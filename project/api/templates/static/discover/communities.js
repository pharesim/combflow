// ── Community suggestions ──
let _suggestionsAbort = null;
let _suggestionsTimer = null;

function getActiveCategorySlugs() {
  return Array.from(Alpine.store('filters').categories);
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
  if (!state.userCommunities) return new Set();
  return new Set(state.userCommunities.map(c => c.id));
}

function renderSuggestions(suggestions) {
  const list = document.getElementById('suggestions-list');
  list.innerHTML = '';
  const auth = getStoredAuth();
  const memberSet = getUserCommunitySet();

  suggestions.forEach(s => {
    const item = document.createElement('div');
    item.className = 'suggestion-item';
    if (state.activeCommunityFilter === s.id) item.classList.add('active');
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
    if (state.userCommunities) {
      state.userCommunities.push({ id: communityId, name: communityName, role: 'guest' });
      sessionStorage.setItem('honeycomb_user_communities', JSON.stringify(state.userCommunities));
    }
  } catch(e) {
    btn.textContent = 'Join';
    showToast(e.message || 'Could not join community', 'error');
  }
  btn.disabled = false;
}

// ── "My Communities" toggle ──
function toggleMyCommunities() {
  setMyCommunitiesActive(!state.myCommunitiesActive);
  if (state.myCommunitiesActive) {
    clearAuthorFilter();
    setFollowingActive(false);
    state.activeCommunityFilter = null;
    updateSuggestionActiveState();
  }
  scheduleFilter();
}

function setMyCommunitiesActive(active) {
  state.myCommunitiesActive = active;
  const btn = document.getElementById('my-communities-toggle');
  if (btn) {
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  }
}

// ── "Following" toggle ──
function toggleFollowing() {
  setFollowingActive(!state.followingFilterActive);
  if (state.followingFilterActive) {
    clearAuthorFilter();
    setMyCommunitiesActive(false);
    state.activeCommunityFilter = null;
    updateSuggestionActiveState();
  }
  scheduleFilter();
}

function setFollowingActive(active) {
  state.followingFilterActive = active;
  const btn = document.getElementById('following-toggle');
  if (btn) {
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  }
}

// ── Filter by community (from suggestion or badge click) ──
function filterByCommunity(communityId) {
  if (state.activeCommunityFilter === communityId) {
    state.activeCommunityFilter = null;
  } else {
    state.activeCommunityFilter = communityId;
  }
  setMyCommunitiesActive(false);
  updateSuggestionActiveState();
  syncCommunityChips();
  scheduleFilter();
}

// ── Filter by author (click username) ──
function filterByAuthor(username) {
  if (state.authorFilterUser === username) {
    clearAuthorFilter();
  } else {
    state.authorFilterUser = username;
    state.myPostsActive = false;
    setMyCommunitiesActive(false);
    setFollowingActive(false);
    updateAuthorFilterBanner();
  }
  scheduleFilter();
}

function clearAuthorFilter() {
  state.authorFilterUser = null;
  updateAuthorFilterBanner();
}

function updateAuthorFilterBanner() {
  const banner = document.getElementById('author-filter-banner');
  if (!banner) return;
  if (state.authorFilterUser) {
    banner.innerHTML = `Posts by <strong>@${esc(state.authorFilterUser)}</strong> <button type="button" class="author-filter-clear" aria-label="Clear author filter" onclick="clearAuthorFilter();scheduleFilter()">&times;</button>`;
    banner.style.display = '';
  } else {
    banner.style.display = 'none';
    banner.innerHTML = '';
  }
}

function syncCommunityChips() {
  document.querySelectorAll('#community-chips .chip').forEach(c => {
    const active = c.dataset.communityId === state.activeCommunityFilter;
    c.classList.toggle('active', active);
    c.setAttribute('aria-pressed', String(active));
  });
  updateFilterCounts();
}

function updateSuggestionActiveState() {
  document.querySelectorAll('.suggestion-item').forEach(item => {
    item.classList.toggle('active', item.dataset.communityId === state.activeCommunityFilter);
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
