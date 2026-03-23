// ── Muting ──
function loadMutedUsers() {
  try { state.mutedUsers = new Set(JSON.parse(localStorage.getItem(MUTED_KEY) || '[]')); } catch(e) { state.mutedUsers = new Set(); }
}
function saveMutedUsers() {
  localStorage.setItem(MUTED_KEY, JSON.stringify(Array.from(state.mutedUsers)));
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
    state.mutedUsers = new Set(allMuted);
    saveMutedUsers();
  } catch(e) {}
}

async function handleMuteUser(username) {
  const auth = getStoredAuth();
  if (!auth) { showLoginPrompt(); return; }
  if (state.mutedUsers.has(username)) { showToast(`@${username} is already muted`, 'info'); return; }

  // Show confirmation
  if (!confirm(`Mute @${username}? Their posts will be hidden.`)) return;

  try {
    await broadcastMute(username);
    state.mutedUsers.add(username);
    saveMutedUsers();
    showToast(`Muted @${username}`, 'success');
    closeModal();
    // Remove muted user's posts from view
    state.posts = state.posts.filter(p => p.author !== username);
    renderAll(state.posts, true);
    updateResultsBar();
  } catch(e) {
    showToast(e.message || 'Could not mute user', 'error');
  }
}

async function handleUnmuteUser(username) {
  try {
    await broadcastUnmute(username);
    state.mutedUsers.delete(username);
    saveMutedUsers();
    showToast(`Unmuted @${username}`, 'success');
    renderMutedUsersList();
    // Show/hide user tabs area based on content
    const area = document.getElementById('settings-users-area');
    if (area) {
      area.style.display = (state.mutedUsers.size > 0 || state.followedUsers.size > 0) ? '' : 'none';
    }
  } catch(e) {
    showToast(e.message || 'Could not unmute user', 'error');
  }
}

function filterMutedPosts(posts) {
  if (state.mutedUsers.size === 0) return posts;
  return posts.filter(p => !state.mutedUsers.has(p.author));
}

// ── Followed users ──
function loadFollowedUsers() {
  try { state.followedUsers = new Set(JSON.parse(localStorage.getItem(FOLLOWED_KEY) || '[]')); } catch(e) { state.followedUsers = new Set(); }
}
function saveFollowedUsers() {
  localStorage.setItem(FOLLOWED_KEY, JSON.stringify(Array.from(state.followedUsers)));
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
    state.followedUsers = new Set(allFollowed);
    saveFollowedUsers();
  } catch(e) {}
}

async function handleFollowUser(username) {
  const auth = getStoredAuth();
  if (!auth) { showLoginPrompt(); return; }
  try {
    await broadcastFollow(username);
    state.followedUsers.add(username);
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
    state.followedUsers.delete(username);
    saveFollowedUsers();
    showToast(`Unfollowed @${username}`, 'success');
    // Update modal button if open
    const btn = document.getElementById('modal-follow-btn');
    if (btn && btn.style.display !== 'none') { btn.textContent = `Follow @${username}`; btn.onclick = () => handleFollowUser(username); }
    // Re-render followed list in settings if open
    renderFollowedUsersList();
    // Show/hide user tabs area based on content
    const area = document.getElementById('settings-users-area');
    if (area) {
      area.style.display = (state.mutedUsers.size > 0 || state.followedUsers.size > 0) ? '' : 'none';
    }
  } catch(e) {
    showToast(e.message || 'Could not unfollow user', 'error');
  }
}

function renderFollowedUsersList() {
  const container = document.getElementById('settings-followed');
  if (!container) return;
  if (state.followedUsers.size === 0) {
    container.innerHTML = '<p style="color:var(--text-dim);font-size:13px">No followed users.</p>';
    return;
  }
  container.innerHTML = '';
  state.followedUsers.forEach(user => {
    const item = document.createElement('div');
    item.className = 'followed-user-item';
    item.innerHTML = `<span class="followed-user-name">@${esc(user)}</span><button type="button" class="btn btn-ghost followed-user-unfollow" onclick="handleUnfollowUser('${esc(user)}')">Unfollow</button>`;
    container.appendChild(item);
  });
}
