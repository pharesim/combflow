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
    const s = Alpine.store('app');
    s.layoutMode = getEffectiveLayout();
    s.posts = [];
    s.initialLoaded = true;
    // Show connection error in the active grid
    const target = document.getElementById(getEffectiveLayout() === 'card' ? 'card-grid' : 'hex-grid');
    // Clear skeletons
    target.querySelectorAll('.skeleton').forEach(el => el.remove());
    const errDiv = document.createElement('div');
    errDiv.className = 'empty';
    errDiv.innerHTML = '<h3>Connection error</h3><p>Could not reach the API. Please check if the server is running and try refreshing.</p>';
    target.appendChild(errDiv);
    if (getEffectiveLayout() === 'hex') { target.style.height = 'auto'; target.style.width = 'auto'; }
    return;
  }

  state.totalPostCount = statsRes.total_posts || 0;
  state.filteredTotalCount = state.totalPostCount;

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

  state.communityList = (communitiesRes.communities || []).sort((a, b) => (b.post_count || 0) - (a.post_count || 0));

  // Build community filter chips (top 100)
  const comChipWrap = document.getElementById('community-chips');
  state.communityList.slice(0, 100).forEach(c => {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'chip';
    el.dataset.communityId = c.id;
    el.setAttribute('aria-pressed', 'false');
    el.textContent = c.name || c.id;
    comChipWrap.appendChild(el);
  });
  comChipWrap.addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    // Only one community active at a time — toggle via filterByCommunity
    filterByCommunity(chip.dataset.communityId);
  });

  // Wire up filter chip events via Alpine store
  const f = Alpine.store('filters');

  catWrap.addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    if (chip.classList.contains('cat-parent')) {
      // Toggle all children of this parent
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

  langWrap.addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    f.toggle('languages', chip.dataset.lang);
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
  await loadAndApplyPreferences();

  // Fetch suggestions based on active categories (from preferences or manual)
  scheduleSuggestions();

  // If preferences activated filters, re-fetch with those filters
  const fStore = Alpine.store('filters');
  if (fStore.categories.size > 0 || fStore.languages.size > 0 || fStore.sentiments.size > 0) {
    await applyFilters();
  } else {
    const rawPosts = postsRes.posts || [];
    state.posts = filterMutedPosts(rawPosts);
    state.currentOffset = state.posts.length;
    state.lastCursor = postsRes.next_cursor || null;
    state.noMorePosts = rawPosts.length < PAGE_SIZE;
    if (state.posts.length > 0) state.newestCreated = state.posts[0].created;
    seedMetaFromServer(state.posts);
    renderAll(state.posts, true);
    updateResultsBar();
    fetchMeta(state.posts);
  }
  // Enable Alpine filter effect now that chips exist and preferences are applied
  enableFilterEffect();
  setupInfiniteScroll();

  // Editor image paste/drop/pick listeners
  const editorBody = document.getElementById('editor-body');
  editorBody.addEventListener('paste', onEditorPaste);
  editorBody.addEventListener('dragover', onEditorDragOver);
  editorBody.addEventListener('dragleave', onEditorDragLeave);
  editorBody.addEventListener('drop', onEditorDrop);
  document.getElementById('editor-image-input').addEventListener('change', onEditorImagePick);

  // Author profile URL (/@username with no permlink)
  const authorMatch = window.location.pathname.match(/^\/@([^/]+)$/);
  if (authorMatch) {
    filterByAuthor(authorMatch[1]);
  }

  // Open post from URL if present (e.g. /@author/permlink or /prefix/@author/permlink)
  const postMatch = window.location.pathname.match(/^\/@([^/]+)\/(.+)$/)
    || window.location.pathname.match(/^\/[^@][^/]*\/@([^/]+)\/(.+)$/);
  if (postMatch) {
    const [, author, permlink] = postMatch;
    state.deepLinked = true;
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
        state.posts = anchorPosts;
        state.currentOffset = state.posts.length;
        state.noMorePosts = anchorPosts.length < PAGE_SIZE;
        state.lastCursor = anchorData.next_cursor || null;
        seedMetaFromServer(state.posts);
        renderAll(state.posts, true);
        updateResultsBar();
        fetchMeta(state.posts);
      } else {
        // Post not in our DB — still try to open via Hive API
        openModal({ author, permlink }, true);
      }
    } catch(e) {
      openModal({ author, permlink }, true);
    }
  }
}

// ── Escape key handler ──
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const s = Alpine.store('app');
    if (s.notifOpen) { s.notifOpen = false; }
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

// ── Resize handler ──
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    Alpine.store('app').layoutMode = getEffectiveLayout();
    syncHexPositions(state.posts.length);
  }, 200);
});

// ── Visibility change (live updates) ──
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearInterval(liveTimer);
    liveTimer = null;
  } else {
    pollNewPosts();
    startLiveUpdates();
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
    // Back to top
    case 'back-to-top': window.scrollTo({top:0,behavior:'smooth'}); break;
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
});

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
    case 'editor-body-input': autoSaveDraft(); break;
    case 'editor-tags-input': showTagSuggestions(); break;
    case 'location-desc-input': _locationAutoFilled = !el.value.trim(); break;
  }
});

document.addEventListener('change', e => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  if (action === 'sort-select') applySort(el.value);
  if (action === 'toggle-manual-voting') {
    document.getElementById('auto-vote-settings').style.display = el.checked ? 'none' : '';
  }
  if (action === 'community-select') onCommunitySelect();
});

init().then(startLiveUpdates);
