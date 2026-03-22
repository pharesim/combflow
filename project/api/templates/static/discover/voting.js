// ── Voting ──
function loadVotedPosts() {
  try { state.votedPosts = JSON.parse(sessionStorage.getItem('honeycomb_voted') || '{}'); } catch(e) { state.votedPosts = {}; }
}
function saveVotedPost(key) {
  state.votedPosts[key] = true;
  sessionStorage.setItem('honeycomb_voted', JSON.stringify(state.votedPosts));
}
function removeVotedPost(key) {
  delete state.votedPosts[key];
  sessionStorage.setItem('honeycomb_voted', JSON.stringify(state.votedPosts));
}

async function fetchManaPercent() {
  if (state.manaCache && Date.now() - state.manaCache.fetchedAt < MANA_CACHE_TTL) return state.manaCache.manaPercent;
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
  state.manaCache = { manaPercent: pct, fetchedAt: Date.now() };
  return pct;
}

function getVotePrefs() {
  // Read from on-chain prefs cached in settings, or use defaults
  return {
    floor: Number(localStorage.getItem('honeycomb_voteFloor') || 50),
    maxWeight: Number(localStorage.getItem('honeycomb_voteMaxWeight') || 100),
    manual: localStorage.getItem('honeycomb_voteManual') === 'true',
  };
}

let _pendingManualVote = null;
function openVotePopup(author, permlink, btn) {
  _pendingManualVote = { author, permlink, btn };
  const lastWeight = Number(localStorage.getItem('honeycomb_lastVoteWeight') || 50);
  document.getElementById('vote-weight-slider').value = lastWeight;
  document.getElementById('vote-weight-val').textContent = lastWeight + '%';
  Alpine.store('app').votePopupOpen = true;
}
function closeVotePopup() {
  Alpine.store('app').votePopupOpen = false;
  if (_pendingManualVote) _pendingManualVote.btn.disabled = false;
  _pendingManualVote = null;
}
async function confirmManualVote() {
  if (!_pendingManualVote) return;
  const { author, permlink, btn } = _pendingManualVote;
  const sliderVal = Number(document.getElementById('vote-weight-slider').value);
  localStorage.setItem('honeycomb_lastVoteWeight', sliderVal);
  const weight = sliderVal * 100;
  closeVotePopup();
  btn.disabled = true;
  const key = `${author}/${permlink}`;
  try {
    await broadcastVote(author, permlink, weight);
    saveVotedPost(key);
    state.manaCache = null;
    updateVoteButtons(key, true);
    bumpVoteCount(key, 1);
    showToast('Voted!', 'success');
  } catch(e) {
    showToast(e.message || 'Vote failed', 'error');
  }
  btn.disabled = false;
}

async function handleVote(author, permlink, btn) {
  const auth = getStoredAuth();
  if (!auth) { showLoginPrompt(); return; }
  const key = `${author}/${permlink}`;
  if (state.votedPosts[key]) {
    if (!confirm('Remove your vote from this post?')) return;
    btn.disabled = true;
    try {
      await broadcastVote(author, permlink, 0);
      removeVotedPost(key);
      updateVoteButtons(key, false);
      bumpVoteCount(key, -1);
    } catch(e) {
      showToast(e.message || 'Unvote failed', 'error');
    }
    btn.disabled = false;
    return;
  }

  const prefs = getVotePrefs();
  if (prefs.manual) {
    btn.disabled = true;
    openVotePopup(author, permlink, btn);
    return;
  }

  btn.disabled = true;
  try {
    const manaPercent = await fetchManaPercent();
    const weight = calculateVoteWeight(manaPercent, prefs.floor, prefs.maxWeight);
    await broadcastVote(author, permlink, weight);
    saveVotedPost(key);
    state.manaCache = null;
    updateVoteButtons(key, true);
    bumpVoteCount(key, 1);
    showToast('Voted!', 'success');
  } catch(e) {
    showToast(e.message || 'Vote failed', 'error');
  }
  btn.disabled = false;
}

function bumpVoteCount(key, delta) {
  const cached = state.metaCache[key];
  if (cached && cached.votes != null) {
    cached.votes = Math.max(0, cached.votes + delta);
    Alpine.store('app').metaRev++;
  }
  document.querySelectorAll(`.vote-count[data-vote-key="${CSS.escape(key)}"]`).forEach(el => {
    el.classList.remove('vote-bump');
    void el.offsetWidth;
    el.classList.add('vote-bump');
  });
  const modalCount = document.getElementById('modal-vote-count');
  if (modalCount && Alpine.store('app').modalOpen) {
    const cur = parseInt(modalCount.textContent) || 0;
    modalCount.textContent = Math.max(0, cur + delta);
    modalCount.classList.remove('vote-bump');
    void modalCount.offsetWidth;
    modalCount.classList.add('vote-bump');
  }
}

// Check if user already voted on a post (from bridge.get_post active_votes)
function checkExistingVote(postResult, username) {
  if (!postResult || !username) return false;
  const votes = postResult.active_votes || [];
  return votes.some(v => v.voter === username);
}
