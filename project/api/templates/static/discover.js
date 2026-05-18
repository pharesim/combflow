// ── Init ──
async function init() {
  loadVotedPosts();
  loadMutedUsers();
  loadFollowedUsers();
  showSkeletons();

  // Deep-link: detect early and fire modal fetch in parallel with grid setup
  const _deepPostMatch = window.location.pathname.match(/^\/@([^/]+)\/(.+)$/)
    || window.location.pathname.match(/^\/[^@][^/]*\/@([^/]+)\/(.+)$/);
  let _deepModalPromise = null;
  if (_deepPostMatch) {
    const [, _dlAuthor, _dlPermlink] = _deepPostMatch;
    state.deepLinked = true;
    // Seed metaCache from server-inlined post data so openModal skips its
    // own bridge.get_post RPC. Background refresh in modal.js still patches
    // stale numbers (votes, payout) once the fresh fetch returns.
    try {
      const _inlineEl = document.getElementById('hivecomb-post-data');
      if (_inlineEl) {
        const _ip = JSON.parse(_inlineEl.textContent);
        if (_ip && _ip.body) {
          const _imgs = (_ip.json_metadata && _ip.json_metadata.image) || [];
          const _payout = _ip.pending_payout_value ? parseFloat(_ip.pending_payout_value) : null;
          state.metaCache[`${_dlAuthor}/${_dlPermlink}`] = {
            title: _ip.title || '',
            thumbnail: Array.isArray(_imgs) && _imgs.length ? _imgs[0] : '',
            votes: (_ip.stats && _ip.stats.total_votes) || (_ip.active_votes || []).length,
            children: _ip.children || 0,
            payout: isNaN(_payout) ? null : _payout,
            body: _ip.body,
            json_metadata: _ip.json_metadata || null,
          };
        }
      }
    } catch(e) { /* fall through to RPC path */ }
    _deepModalPromise = fetch(`/posts/${encodeURIComponent(_dlAuthor)}/${encodeURIComponent(_dlPermlink)}`)
      .then(r => r.ok ? r.json() : null)
      .then(postData => {
        if (postData) openModal(postData, true);
        else window.prerenderReady = true;
        return postData;
      })
      .catch(() => {
        openModal({ author: _dlAuthor, permlink: _dlPermlink }, true);
        return null;
      });
  }

  // URL-driven filter surfaces — /c/{cat}, /community/{id}, /lang/{lang}, /@{author}.
  // Bake into the initial browse so the first (and only) fetch is already filtered,
  // and the prerender snapshot reflects the filtered state without any second fetch.
  const _urlCategoryMatch = window.location.pathname.match(/^\/c\/([a-z0-9-]+)$/);
  const _urlCommunityMatch = window.location.pathname.match(/^\/community\/(hive-\d+)$/);
  const _urlLanguageMatch = window.location.pathname.match(/^\/lang\/([a-z]{2,3})$/);
  const _urlAuthorMatch = window.location.pathname.match(/^\/@([^/]+)$/);

  // Pre-read cached filters so the initial browse already includes them
  // (avoids a wasted unfiltered fetch followed by a second filtered fetch)
  let _initBrowseUrl = `/api/browse?limit=${PAGE_SIZE}`;
  // URL-derived filters take priority over session/prefs (explicit deep link).
  if (_urlCategoryMatch || _urlCommunityMatch || _urlLanguageMatch || _urlAuthorMatch) {
    window.prerenderReady = false;
    if (_urlCategoryMatch) _initBrowseUrl += `&category=${encodeURIComponent(_urlCategoryMatch[1])}`;
    if (_urlCommunityMatch) _initBrowseUrl += `&community=${encodeURIComponent(_urlCommunityMatch[1])}`;
    if (_urlLanguageMatch) _initBrowseUrl += `&language=${encodeURIComponent(_urlLanguageMatch[1])}`;
    if (_urlAuthorMatch) _initBrowseUrl += `&authors=${encodeURIComponent(_urlAuthorMatch[1])}`;
  }
  const _sessionRaw = sessionStorage.getItem('honeycomb_sessionFilters');
  const _prefsRaw = localStorage.getItem('honeycomb_filterPrefs');
  const _initFilters = (_urlCategoryMatch || _urlCommunityMatch || _urlLanguageMatch || _urlAuthorMatch)
    ? null
    : (_sessionRaw ? JSON.parse(_sessionRaw) : (_prefsRaw ? JSON.parse(_prefsRaw) : null));
  if (_initFilters) {
    (_initFilters.categories || _initFilters.default_categories || []).forEach(c =>
      _initBrowseUrl += `&category=${encodeURIComponent(c)}`);
    (_initFilters.languages || _initFilters.default_languages || []).forEach(l =>
      _initBrowseUrl += `&language=${encodeURIComponent(l)}`);
    const _sent = _initFilters.sentiments || (_initFilters.default_sentiment ? [_initFilters.default_sentiment] : []);
    const _realSent = _sent.filter(s => s !== 'nsfw');
    if (_realSent.length === 1) _initBrowseUrl += `&sentiment=${encodeURIComponent(_realSent[0])}`;
    if (_initFilters.community)
      _initBrowseUrl += `&community=${encodeURIComponent(_initFilters.community)}`;
    if (_sent.includes('nsfw') || localStorage.getItem('honeycomb_nsfwMode') === 'show')
      _initBrowseUrl += '&include_nsfw=true';
    if (_sent.includes('nsfw') && localStorage.getItem('honeycomb_nsfwMode') === 'filter')
      _initBrowseUrl += '&nsfw_only=true';
  }

  // ── Phase 1: fetch browse + categories (fast), render posts immediately ──
  // Languages and stats do full table scans on cold caches (rebuild / long idle)
  // and can take 5-10s. Don't let them block hex rendering.
  const statsP = fetch('/api/stats').then(r => r.json()).catch(() => ({}));
  const langsP = fetch('/api/languages').then(r => r.json()).catch(() => ({ languages: [] }));
  const communitiesP = fetch('/api/communities').then(r => r.json()).catch(() => ({ communities: [] }));

  let catsRes, postsRes;
  try {
    [catsRes, postsRes] = await Promise.all([
      fetch('/categories').then(r => r.json()),
      fetch(_initBrowseUrl).then(r => r.json()),
    ]);
  } catch(e) {
    const s = Alpine.store('app');
    s.layoutMode = getEffectiveLayout();
    s.posts = [];
    s.initialLoaded = true;
    const target = document.getElementById(getEffectiveLayout() === 'card' ? 'card-grid' : 'hex-grid');
    target.querySelectorAll('.skeleton').forEach(el => el.remove());
    const errDiv = document.createElement('div');
    errDiv.className = 'empty';
    errDiv.innerHTML = '<h3>Connection error</h3><p>Could not reach the API. Please check if the server is running and try refreshing.</p>';
    target.appendChild(errDiv);
    if (getEffectiveLayout() === 'hex') { target.style.height = 'auto'; target.style.width = 'auto'; }
    return;
  }

  // Use browse response total as initial estimate (stats may still be loading)
  state.totalPostCount = postsRes.total || 0;
  state.filteredTotalCount = state.totalPostCount;

  // Build category chips (fast — small table, needed for preferences)
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
      const chip = document.createElement('a');
      chip.href = `/c/${encodeURIComponent(ch.name)}`;
      chip.className = 'chip';
      chip.dataset.cat = ch.name;
      chip.dataset.parent = parent.name;
      chip.setAttribute('aria-pressed', 'false');
      chip.textContent = ch.name;
      group.appendChild(chip);
    });
    catWrap.appendChild(group);
  });

  // Wire category + sentiment chip events (sentiment chips are in HTML)
  const f = Alpine.store('filters');
  catWrap.addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    // Modifier-click (cmd/ctrl/shift) navigates normally — opens link in new tab.
    if (e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    if (chip.classList.contains('cat-parent')) {
      const children = catWrap.querySelectorAll(`.chip[data-parent="${chip.dataset.cat}"]`);
      const allActive = Array.from(children).every(c => f.categories.has(c.dataset.cat));
      children.forEach(c => {
        if (allActive) f.remove('categories', c.dataset.cat);
        else f.add('categories', c.dataset.cat);
      });
    } else {
      f.toggle('categories', chip.dataset.cat);
    }
  });
  document.getElementById('sentiment-chips').addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    f.toggle('sentiments', chip.dataset.sentiment);
  });

  // Set initial toggle state
  document.querySelectorAll('#layout-toggle button').forEach(b =>
    b.classList.toggle('active', b.dataset.layout === state.layoutMode)
  );

  // Auth UI + preferences
  const authInit = getStoredAuth();
  if (authInit) {
    Alpine.store('app').currentUser = authInit.username;
    fetchUserCommunities(authInit.username).then(list => { state.userCommunities = list; });
    fetchMutedList();
    fetchFollowedList();
    fetchUnreadCount();
    startNotifPolling();
  }
  // Session filters take priority over saved defaults
  const hasSession = loadSessionFilters();
  if (!hasSession) {
    await loadAndApplyPreferences();
  } else {
    // Still load preferences in background to set hasDefaultFilters flag
    const cached = localStorage.getItem('honeycomb_filterPrefs');
    if (cached) {
      try {
        const fp = JSON.parse(cached);
        const hasDefaults = (fp.default_categories?.length > 0)
          || (fp.default_languages?.length > 0)
          || !!fp.default_sentiment;
        Alpine.store('app').hasDefaultFilters = hasDefaults;
      } catch(e) {}
    }
  }
  applyNsfwMode(getNsfwMode());
  initCurationUI();

  // Render posts immediately — don't wait for languages/stats/communities
  const fStore = Alpine.store('filters');
  // URL-driven filters: seed state/store so chips and banners show as active.
  // Done before enableFilterEffect() so this doesn't trigger a second fetch.
  if (_urlCategoryMatch) fStore.add('categories', _urlCategoryMatch[1]);
  if (_urlLanguageMatch) fStore.add('languages', _urlLanguageMatch[1]);
  if (_urlCommunityMatch) state.activeCommunityFilter = _urlCommunityMatch[1];
  if (_urlAuthorMatch) state.authorFilterUser = _urlAuthorMatch[1];
  if (state.myCommunitiesActive || state.followingFilterActive) {
    await applyFilters();
  } else {
    const rawPosts = postsRes.posts || [];
    if (fStore.categories.size > 0 || fStore.languages.size > 0 || fStore.sentiments.size > 0
      || state.activeCommunityFilter || state.authorFilterUser) {
      state.filteredTotalCount = postsRes.total || 0;
    }
    state.posts = filterMutedPosts(rawPosts);
    state.posts = applyCurationFilters(state.posts);
    state.currentOffset = state.posts.length;
    state.lastCursor = postsRes.next_cursor || null;
    state.noMorePosts = rawPosts.length < PAGE_SIZE;
    if (state.posts.length > 0) state.newestCreated = state.posts[0].created;
    seedMetaFromServer(state.posts);
    renderAll(state.posts, true);
    updateResultsBar();
    fetchMeta(state.posts);
  }
  enableFilterEffect();

  // ── Phase 2: build remaining filter UI when slow endpoints finish ──
  Promise.all([statsP, langsP, communitiesP]).then(([statsRes, langsRes, communitiesRes]) => {
    // Update total from stats (more accurate than browse estimate)
    state.totalPostCount = statsRes.total_posts || state.totalPostCount;
    if (!hasActiveFilters()) state.filteredTotalCount = state.totalPostCount;
    updateResultsBar();
    if (statsRes.api_base_url) {
      const apiLink = document.getElementById('footer-api-link');
      if (apiLink) apiLink.href = statsRes.api_base_url + '/docs';
    }

    // Build language chips (anchors so right-click → copy link, cmd-click → new tab)
    const langWrap = document.getElementById('lang-chips');
    (langsRes.languages || []).slice(0, 40).forEach(l => {
      const el = document.createElement('a');
      el.href = `/lang/${encodeURIComponent(l.language)}`;
      el.className = 'chip';
      el.dataset.lang = l.language;
      el.setAttribute('aria-pressed', 'false');
      el.textContent = l.language;
      langWrap.appendChild(el);
    });
    langWrap.addEventListener('click', e => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      f.toggle('languages', chip.dataset.lang);
    });

    // Build community chips (anchors so right-click → copy link, cmd-click → new tab)
    state.communityList = (communitiesRes.communities || []).sort((a, b) => (b.post_count || 0) - (a.post_count || 0));
    const comChipWrap = document.getElementById('community-chips');
    state.communityList.slice(0, 100).forEach(c => {
      const el = document.createElement('a');
      el.href = `/community/${encodeURIComponent(c.id)}`;
      el.className = 'chip';
      el.dataset.communityId = c.id;
      el.setAttribute('aria-pressed', 'false');
      el.textContent = c.name || c.id;
      comChipWrap.appendChild(el);
    });
    comChipWrap.addEventListener('click', e => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      filterByCommunity(chip.dataset.communityId);
    });

    // Sync chip active states to reflect any applied filters
    syncAllChipsDom();
    updateFilterCounts();
    scheduleSuggestions();
    checkFilterBarFit();
  }).catch(e => console.error('Phase 2 init failed:', e));

  checkFilterBarFit();
  setupInfiniteScroll();

  // Editor image paste/drop/pick listeners
  const editorBody = document.getElementById('editor-body');
  editorBody.addEventListener('paste', onEditorPaste);
  editorBody.addEventListener('dragover', onEditorDragOver);
  editorBody.addEventListener('dragleave', onEditorDragLeave);
  editorBody.addEventListener('drop', onEditorDrop);
  document.getElementById('editor-image-input').addEventListener('change', onEditorImagePick);

  // URL-driven filters' UI affordances (banner, indicators) — chips were
  // already synced via the Alpine effect after enableFilterEffect().
  if (_urlAuthorMatch) updateAuthorFilterBanner();
  if (_urlCommunityMatch) showCommunityIndicator();

  // Deep-link: modal was already opened early — now anchor the grid to that post
  if (_deepModalPromise) {
    const postData = await _deepModalPromise;
    if (postData) {
      const [, author, permlink] = _deepPostMatch;
      const linkedTs = new Date(postData.created).getTime() / 1000 + 0.001;
      const anchorCursor = `${linkedTs}_${postData.id + 1}`;
      try {
        const anchorRes = await fetch(`/api/browse?limit=${PAGE_SIZE}&cursor=${encodeURIComponent(anchorCursor)}`);
        const anchorData = await anchorRes.json();
        const anchorPosts = anchorData.posts || [];
        const hasLinked = anchorPosts.some(p => p.author === author && p.permlink === permlink);
        if (!hasLinked) anchorPosts.unshift(postData);
        state.posts = anchorPosts;
        state.currentOffset = state.posts.length;
        state.noMorePosts = anchorPosts.length < PAGE_SIZE;
        state.lastCursor = anchorData.next_cursor || null;
        seedMetaFromServer(state.posts);
        renderAll(state.posts, true);
        updateResultsBar();
        fetchMeta(state.posts);
      } catch(e) {}
    }
  }

  if (!state.deepLinked) window.prerenderReady = true;
}

// ── Escape key handler ──
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (document.getElementById('lightbox').classList.contains('active')) { closeLightbox(); return; }
    const s = Alpine.store('app');
    if (s.reportOpen) closeReport();
    else if (s.notifOpen) { s.notifOpen = false; }
    else if (s.mdHelpOpen) closeMdHelp();
    else if (s.locationOpen) closeLocationPicker();
    else if (s.votePopupOpen) closeVotePopup();
    else if (s.editorOpen) confirmCloseEditor();
    else if (s.loginOpen) closeLogin();
    else if (s.signupOpen) closeSignup();
    else if (s.settingsOpen) closeSettingsModal();
    else if (s.modalOpen) closeModal();
  }
});

// ── Unstick filter bar when viewport too short for 2.5 hexes of content ──
function checkFilterBarFit() {
  const header = document.querySelector('.header');
  const bar = document.getElementById('filters-bar');
  if (!header || !bar) return;
  const { h } = hexMetrics();
  const filtersEl = document.querySelector('.filters');
  const headerH = header.offsetHeight;
  const barH = bar.offsetHeight;
  const filtersH = bar.classList.contains('expanded') && filtersEl ? filtersEl.offsetHeight : 0;
  const remaining = window.innerHeight - headerH - barH - filtersH;
  const unstick = remaining < h * 2.5;
  bar.classList.toggle('viewport-short', unstick);
  if (filtersEl) filtersEl.classList.toggle('viewport-short', unstick);
}

// ── Resize handler ──
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    Alpine.store('app').layoutMode = getEffectiveLayout();
    syncHexPositions(state.posts.length);
    checkFilterBarFit();
  }, 200);
});

// ── Visibility change (live updates) ──
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    hiddenSince = Date.now();
    clearInterval(liveTimer);
    liveTimer = null;
  } else {
    pollNewPosts();
    startLiveUpdates();
    if (Alpine.store('app').currentUser) {
      startNotifPolling();
      if (Alpine.store('app').notifOpen) fetchNotifications();
      _refreshUserCommunities(Alpine.store('app').currentUser);
      fetchFollowedList();
      fetchMutedList();
    }
  }
});

// ── Back to top ──
window.addEventListener('scroll', () => {
  const btn = document.getElementById('back-to-top');
  btn.classList.toggle('visible', window.scrollY > 600);
}, { passive: true });

// ── Event delegation ──
document.addEventListener('click', e => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  switch (action) {
    // Login
    case 'close-login': closeLogin(); break;
    case 'login': doLogin(); break;
    case 'open-signup': e.preventDefault(); openSignup(); break;
    case 'close-signup': closeSignup(); break;
    // Vote popup
    case 'confirm-manual-vote': confirmManualVote(); break;
    case 'close-vote-popup': closeVotePopup(); break;
    // Filters bar
    case 'toggle-filters-bar': toggleFiltersBar(); break;
    case 'stop-propagation': e.stopPropagation(); break;
    case 'toggle-my-communities': toggleMyCommunities(); break;
    case 'toggle-following': toggleFollowing(); break;
    case 'set-layout': setLayout(el.dataset.layout); break;
    case 'toggle-section': toggleSection(el.dataset.section); break;
    case 'save-preferences': savePreferences(); break;
    case 'reset-filters': resetFilters(); break;
    case 'clear-filters': clearFilters(); break;
    // Settings modal
    case 'skip-settings': skipSettings(); break;
    case 'save-settings': saveSettings(); break;
    case 'settings-main-tab': {
      const tab = el.dataset.tab;
      document.querySelectorAll('.settings-main-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
      document.querySelectorAll('.settings-main-panel').forEach(p => p.style.display = 'none');
      document.getElementById('settings-main-' + tab).style.display = '';
      break;
    }
    case 'settings-tab': {
      const tab = el.dataset.tab;
      document.querySelectorAll('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
      document.querySelectorAll('.settings-tab-panel').forEach(p => p.style.display = 'none');
      document.getElementById('settings-tab-' + tab).style.display = '';
      break;
    }
    // Post modal
    case 'close-modal': closeModal(); break;
    case 'copy-post-link': copyPostLink(); break;
    // Report
    case 'open-report': openReport(); break;
    case 'close-report': closeReport(); break;
    case 'submit-report': submitReport(); break;
    // Editor
    case 'open-editor': openEditor(); break;
    case 'close-editor': confirmCloseEditor(); break;
    case 'open-location-picker': openLocationPicker(); break;
    case 'editor-tab': showEditorTab(el.dataset.tab); break;
    case 'editor-insert': editorInsert(el.dataset.before || '', el.dataset.after || ''); break;
    case 'editor-insert-line': editorInsertLine(el.dataset.prefix || ''); break;
    case 'editor-insert-link': editorInsertLink(); break;
    case 'editor-insert-image': editorInsertImage(); break;
    case 'editor-insert-table': editorInsertTable(); break;
    case 'editor-insert-columns': editorInsertColumns(); break;
    case 'show-md-help': showMarkdownHelp(); break;
    case 'publish-post': publishPost(); break;
    // Location picker
    case 'close-location-picker': closeLocationPicker(); break;
    case 'use-my-location': useMyLocation(); break;
    case 'confirm-location': confirmLocation(); break;
    // Markdown help
    case 'close-md-help': closeMdHelp(); break;
    // Notifications
    case 'toggle-notifications': toggleNotifications(); break;
    case 'mark-all-read': markAllRead(); break;
    // Theme
    case 'toggle-theme': toggleTheme(); break;
    // Back to top
    case 'back-to-top': window.scrollTo({top:0,behavior:'smooth'}); break;
    // Comments
    case 'comment-vote': handleCommentVote(el.dataset.author, el.dataset.permlink, el); break;
    case 'comment-reply': openReplyForm(el.dataset.author, el.dataset.permlink, el); break;
    case 'toggle-comment-children': toggleCommentChildren(el); break;
    case 'comment-preview': toggleCommentPreview(el.dataset.formId); break;
    case 'close-reply-form': closeReplyForm(); break;
    case 'submit-comment': submitComment(el.dataset.parentAuthor, el.dataset.parentPermlink, el.dataset.formId); break;
    case 'navigate-post': e.preventDefault(); closeModal(true); openModal({author: el.dataset.author, permlink: el.dataset.permlink}); break;
    case 'filter-community': e.preventDefault(); e.stopPropagation(); filterByCommunity(el.dataset.community); break;
    case 'clear-author-filter': clearAuthorFilter(); scheduleFilter(); break;
    case 'unmute-user': handleUnmuteUser(el.dataset.user); break;
    case 'unfollow-user': handleUnfollowUser(el.dataset.user); break;
    case 'remove-editor-tag': removeEditorTag(parseInt(el.dataset.index)); break;
    case 'remove-location': e.stopPropagation(); removeLocation(); break;
    // Curation mode
    case 'toggle-curation-mode': break; // handled via change event
    case 'curation-age-dec': {
      const s = document.getElementById('curation-age-slider');
      if (s && Number(s.value) > 0) { s.value = Number(s.value) - 1; handleCurationAgeChange(s); }
      break;
    }
    case 'curation-age-inc': {
      const s = document.getElementById('curation-age-slider');
      if (s && Number(s.value) < 30) { s.value = Number(s.value) + 1; handleCurationAgeChange(s); }
      break;
    }
  }
});

document.addEventListener('keydown', e => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  if (action === 'login-enter' && e.key === 'Enter') doLogin();
  if (action === 'toggle-filters-bar' && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault(); toggleFiltersBar();
  }
  if (action === 'stop-propagation') e.stopPropagation();
  if (action === 'editor-tags-input') handleTagKey(e);
  if (action === 'editor-body-input') handleMentionKeydown(el, e);
});

// ── @-mention support for dynamic comment textareas ──
document.addEventListener('input', e => {
  if (e.target.classList.contains('comment-textarea')) handleMentionInput(e.target);
}, true);
document.addEventListener('keydown', e => {
  if (e.target.classList.contains('comment-textarea')) handleMentionKeydown(e.target, e);
}, true);

function handleCurationAgeChange(slider) {
  const step = Number(slider.value);
  state.curationMaxAge = getCurationAgeValue(step);
  document.getElementById('curation-age-label').textContent = getCurationAgeLabel(step);
  scheduleFilter();
}

document.addEventListener('input', e => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  switch (action) {
    case 'vote-weight-slider':
      document.getElementById('vote-weight-val').textContent = el.value + '%'; break;
    case 'vote-floor-slider':
      document.getElementById('settings-vote-floor-val').textContent = el.value + '%'; updateVoteEstimate(); break;
    case 'vote-max-slider':
      document.getElementById('settings-vote-max-val').textContent = el.value + '%'; updateVoteEstimate(); break;
    case 'editor-title-input': updateEditorTitleCount(); autoSaveDraft(); break;
    case 'editor-desc-input': updateEditorDescCount(); autoSaveDraft(); break;
    case 'editor-body-input': autoSaveDraft(); handleMentionInput(el); break;
    case 'editor-tags-input': showTagSuggestions(); break;
    case 'report-reason-input': updateReportCount(); break;
    case 'location-desc-input': _locationAutoFilled = !el.value.trim(); break;
    case 'curation-age-input': handleCurationAgeChange(el); break;
    case 'curation-payout-input':
      state.curationMaxPayout = el.value;
      scheduleFilter();
      break;
  }
});

document.addEventListener('change', e => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  if (action === 'sort-select') applySort(el.value);
  if (action === 'toggle-manual-voting') {
    document.getElementById('auto-vote-settings').style.display = el.checked ? 'none' : '';
    // Show/hide curation mode option (only available when manual voting is on)
    const curationRow = document.getElementById('settings-curation-row');
    if (curationRow) curationRow.style.display = el.checked ? '' : 'none';
    // If manual voting turned off, disable curation mode too
    if (!el.checked) {
      const curationCb = document.getElementById('settings-curation-mode');
      if (curationCb) curationCb.checked = false;
      setCurationMode(false);
    }
  }
  if (action === 'community-select') onCommunitySelect();
  if (action === 'curation-votes-select') { state.curationVotes = el.value; scheduleFilter(); }
  if (action === 'curation-sort-select') { state.curationSort = el.value; scheduleFilter(); }
  if (action === 'toggle-curation-mode') setCurationMode(el.checked);
});

init().then(startLiveUpdates);
