// ── Preferences ──
async function loadAndApplyPreferences() {
  const auth = getStoredAuth();
  if (!auth) return null;
  // Try localStorage first for instant UI
  const cached = localStorage.getItem('honeycomb_filterPrefs');
  if (cached) {
    try {
      const prefs = JSON.parse(cached);
      applyPreferenceFilters(prefs);
      return prefs;
    } catch(e) {}
  }
  // No cache — fetch from on-chain and seed localStorage
  try {
    const accounts = await hiveRpc('condenser_api.get_accounts', [[auth.username]]);
    const account = accounts?.[0];
    if (!account) return null;
    let meta = {};
    try { meta = JSON.parse(account.posting_json_metadata || '{}'); } catch(e) {}
    const prefs = meta.combflow || {};
    const filterPrefs = {
      default_categories: prefs.default_categories || [],
      default_languages: prefs.default_languages || [],
      default_sentiment: prefs.default_sentiment || null,
    };
    localStorage.setItem('honeycomb_filterPrefs', JSON.stringify(filterPrefs));
    applyPreferenceFilters(prefs);
    return prefs;
  } catch(e) { return null; }
}

function applyPreferenceFilters(prefs) {
  // Cache vote settings locally
  if (prefs.voteFloor != null) localStorage.setItem('honeycomb_voteFloor', prefs.voteFloor);
  if (prefs.voteMaxWeight != null) localStorage.setItem('honeycomb_voteMaxWeight', prefs.voteMaxWeight);
  if (prefs.voteManual != null) localStorage.setItem('honeycomb_voteManual', prefs.voteManual);
  // Set Alpine filter store from preferences
  const f = Alpine.store('filters');
  if (prefs.default_categories && prefs.default_categories.length > 0) {
    f.setAll('categories', prefs.default_categories);
  }
  if (prefs.default_languages && prefs.default_languages.length > 0) {
    f.setAll('languages', prefs.default_languages);
  }
  if (prefs.default_sentiment) {
    f.setAll('sentiments', [prefs.default_sentiment]);
  }
  // DOM sync happens via Alpine.effect() in filters.js
  updateFilterCounts();
}

async function savePreferences() {
  const auth = getStoredAuth();
  if (!auth) return;

  const f = Alpine.store('filters');
  const cats = Array.from(f.categories);
  const langs = Array.from(f.languages);
  const sentiments = Array.from(f.sentiments);

  try {
    // Read current posting_json_metadata to merge
    const accounts = await hiveRpc('condenser_api.get_accounts', [[auth.username]]);
    const account = accounts?.[0];
    if (!account) { showToast('Could not read account', 'error'); return; }

    let postingMeta = {};
    try { postingMeta = JSON.parse(account.posting_json_metadata || '{}'); } catch(e) {}

    const filterPrefs = {
      default_categories: cats,
      default_languages: langs,
      default_sentiment: sentiments.length === 1 ? sentiments[0] : null,
    };

    // Cache filter prefs to localStorage for instant load
    localStorage.setItem('honeycomb_filterPrefs', JSON.stringify(filterPrefs));

    // Merge with existing on-chain prefs (preserve vote settings etc.)
    const existing = postingMeta.combflow || {};
    postingMeta.combflow = { ...existing, ...filterPrefs };

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
      && !prefs.default_sentiment
      && prefs.voteFloor == null
      && prefs.voteMaxWeight == null
      && prefs.voteManual == null;
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

function estimateVotes(floor, maxWeight) {
  let mana = 100;
  const target = floor + (100 - floor) * 0.1;
  let votes = 0;
  while (mana > target && votes < 10000) {
    const ratio = (mana - floor) / (100 - floor);
    const weight = Math.max(1, (1 - Math.pow(1 - ratio, 1.2)) * maxWeight);
    mana -= (weight / 100) * 2;
    votes++;
  }
  return votes;
}

function updateVoteEstimate() {
  const floor = Number(document.getElementById('settings-vote-floor').value);
  const maxWeight = Number(document.getElementById('settings-vote-max').value);
  document.getElementById('vote-estimate').textContent =
    '~' + estimateVotes(floor, maxWeight) + ' votes before reaching mana floor';
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
  const voteManualInput = document.getElementById('settings-vote-manual');
  if (voteFloorInput) {
    const vf = savedPrefs.voteFloor != null ? savedPrefs.voteFloor : 50;
    const vm = savedPrefs.voteMaxWeight != null ? savedPrefs.voteMaxWeight : 100;
    const manual = savedPrefs.voteManual || false;
    voteFloorInput.value = vf;
    document.getElementById('settings-vote-floor-val').textContent = vf + '%';
    voteMaxInput.value = vm;
    document.getElementById('settings-vote-max-val').textContent = vm + '%';
    voteManualInput.checked = manual;
    document.getElementById('auto-vote-settings').style.display = manual ? 'none' : '';
    updateVoteEstimate();
  }

  // Render muted + followed users
  renderMutedUsersList();
  renderFollowedUsersList();

  // Show user tabs area if either list has content, reset to muted tab
  const area = document.getElementById('settings-users-area');
  if (area) {
    area.style.display = (state.mutedUsers.size > 0 || state.followedUsers.size > 0) ? '' : 'none';
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'muted'));
    document.getElementById('settings-tab-muted').style.display = '';
    document.getElementById('settings-tab-followed').style.display = 'none';
  }

  wireSettingsOnce();

  Alpine.store('app').settingsOpen = true;
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
        const voteManual = document.getElementById('settings-vote-manual').checked;
        const votePrefs = {
          voteFloor: voteFloor,
          voteMaxWeight: voteMax,
          voteManual: voteManual,
        };
        const filterPrefs = {
          default_categories: cats,
          default_languages: langs,
          default_sentiment: sentiments.length === 1 ? sentiments[0] : null,
        };
        // Cache filter prefs to localStorage for instant load
        localStorage.setItem('honeycomb_filterPrefs', JSON.stringify(filterPrefs));
        // Cache vote prefs locally for immediate use
        localStorage.setItem('honeycomb_voteFloor', voteFloor);
        localStorage.setItem('honeycomb_voteMaxWeight', voteMax);
        localStorage.setItem('honeycomb_voteManual', voteManual);
        // Merge with existing on-chain prefs
        const existing = postingMeta.combflow || {};
        postingMeta.combflow = { ...existing, ...filterPrefs, ...votePrefs };
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
  Alpine.store('app').settingsOpen = false;
}

// ── Muted users list (in settings modal) ──
function renderMutedUsersList() {
  const container = document.getElementById('settings-muted');
  if (!container) return;
  if (state.mutedUsers.size === 0) {
    container.innerHTML = '<p style="color:var(--text-dim);font-size:13px">No muted users.</p>';
    return;
  }
  container.innerHTML = '';
  state.mutedUsers.forEach(user => {
    const item = document.createElement('div');
    item.className = 'muted-user-item';
    item.innerHTML = `<span class="muted-user-name">@${esc(user)}</span><button type="button" class="btn btn-ghost muted-user-unmute" onclick="handleUnmuteUser('${esc(user)}')">Unmute</button>`;
    container.appendChild(item);
  });
}
