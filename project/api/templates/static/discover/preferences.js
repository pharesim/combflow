// ── Theme ──
function toggleTheme() {
  const isLight = document.documentElement.dataset.theme === 'light';
  const next = isLight ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('theme', next);
}

function getValidCategorySlugs() {
  const slugs = new Set();
  document.querySelectorAll('#cat-chips .chip:not(.cat-parent)').forEach(c => {
    if (c.dataset.cat) slugs.add(c.dataset.cat);
  });
  return slugs;
}

function cleanStaleCategorySlugs(prefs) {
  if (!prefs.default_categories || prefs.default_categories.length === 0) return prefs;
  const valid = getValidCategorySlugs();
  if (valid.size === 0) return prefs; // chips not built yet, skip cleanup
  const cleaned = prefs.default_categories.filter(s => valid.has(s));
  if (cleaned.length !== prefs.default_categories.length) {
    prefs.default_categories = cleaned;
    // Update localStorage so stale slugs don't persist
    const cached = localStorage.getItem('honeycomb_filterPrefs');
    if (cached) {
      try {
        const fp = JSON.parse(cached);
        fp.default_categories = cleaned;
        localStorage.setItem('honeycomb_filterPrefs', JSON.stringify(fp));
      } catch(e) {}
    }
  }
  return prefs;
}

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
  cleanStaleCategorySlugs(prefs);
  // Cache vote settings locally
  if (prefs.voteFloor != null) localStorage.setItem('honeycomb_voteFloor', prefs.voteFloor);
  if (prefs.voteMaxWeight != null) localStorage.setItem('honeycomb_voteMaxWeight', prefs.voteMaxWeight);
  if (prefs.voteManual != null) localStorage.setItem('honeycomb_voteManual', prefs.voteManual);
  if (prefs.nsfwMode != null) {
    localStorage.setItem('honeycomb_nsfwMode', prefs.nsfwMode);
    applyNsfwMode(prefs.nsfwMode);
  }
  if (prefs.payoutType != null) localStorage.setItem('honeycomb_payoutType', prefs.payoutType);
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
  // Track whether user has saved default filters
  const hasDefaults = (prefs.default_categories?.length > 0)
    || (prefs.default_languages?.length > 0)
    || !!prefs.default_sentiment;
  Alpine.store('app').hasDefaultFilters = hasDefaults;
  updateFilterCounts();
  syncAllChipsDom();
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
    Alpine.store('app').hasDefaultFilters = cats.length > 0 || langs.length > 0 || sentiments.length === 1;
    checkFiltersMatchDefault();

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

// NSFW filter easter egg: click "Show NSFW posts" 10 times to reveal filter option
let _nsfwShowClicks = 0;

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

  // Easter egg: clicking "Show NSFW posts" radio 10 times reveals filter option
  document.getElementById('nsfw-show-label').addEventListener('click', () => {
    _nsfwShowClicks++;
    if (_nsfwShowClicks >= 10) {
      document.getElementById('nsfw-filter-label').style.display = '';
    }
  });
}

function estimateVotes(floor, maxWeight, startMana) {
  let mana = startMana;
  const target = floor + (100 - floor) * 0.1;
  if (mana <= target) return 0;
  let votes = 0;
  while (mana > target && votes < 10000) {
    const ratio = (mana - floor) / (100 - floor);
    const weight = Math.max(1, (1 - Math.pow(1 - ratio, 1.2)) * maxWeight);
    mana -= (weight / 100) * 2;
    votes++;
  }
  return votes;
}

async function updateVoteEstimate() {
  const floor = Number(document.getElementById('settings-vote-floor').value);
  const maxWeight = Number(document.getElementById('settings-vote-max').value);
  const mana = await fetchManaPercent();
  const el = document.getElementById('vote-estimate');
  if (!el) return;
  el.textContent = '~' + estimateVotes(floor, maxWeight, mana) +
    ' votes before reaching mana floor (current mana: ' + Math.round(mana) + '%)';
}

async function showSettingsModal() {
  const modal = document.getElementById('settings-modal');

  // Read from localStorage (updated immediately on save) to avoid on-chain propagation delay
  let savedPrefs = {};
  const auth = getStoredAuth();
  if (auth) {
    const cached = localStorage.getItem('honeycomb_filterPrefs');
    if (cached) {
      try { savedPrefs = JSON.parse(cached); } catch(e) {}
    }
    const vf = localStorage.getItem('honeycomb_voteFloor');
    const vm = localStorage.getItem('honeycomb_voteMaxWeight');
    const vman = localStorage.getItem('honeycomb_voteManual');
    const nsfw = localStorage.getItem('honeycomb_nsfwMode');
    const payout = localStorage.getItem('honeycomb_payoutType');
    if (vf != null) savedPrefs.voteFloor = Number(vf);
    if (vm != null) savedPrefs.voteMaxWeight = Number(vm);
    if (vman != null) savedPrefs.voteManual = vman === 'true';
    if (nsfw != null) savedPrefs.nsfwMode = nsfw;
    if (payout != null) savedPrefs.payoutType = payout;

    // Fall back to on-chain fetch if no localStorage data at all
    if (!cached && !vf) {
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

  // Set NSFW mode radio
  const nsfwMode = savedPrefs.nsfwMode || localStorage.getItem('honeycomb_nsfwMode') || 'hide';
  document.querySelectorAll('input[name="settings-nsfw"]').forEach(r => {
    r.checked = r.value === nsfwMode;
  });
  // Show filter option if it's the saved pref, otherwise hide until easter egg
  const filterLabel = document.getElementById('nsfw-filter-label');
  if (nsfwMode === 'filter') {
    filterLabel.style.display = '';
  } else {
    filterLabel.style.display = 'none';
    _nsfwShowClicks = 0;
  }

  // Set payout preference
  const payoutSelect = document.getElementById('settings-payout');
  if (payoutSelect) {
    payoutSelect.value = savedPrefs.payoutType || localStorage.getItem('honeycomb_payoutType') || 'powerup';
  }

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

  // Show/hide Users top-level tab; reset to Filters tab
  const hasUsers = state.mutedUsers.size > 0 || state.followedUsers.size > 0;
  document.getElementById('settings-main-tab-users').style.display = hasUsers ? '' : 'none';
  document.querySelectorAll('.settings-main-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'filters'));
  document.querySelectorAll('.settings-main-panel').forEach(p => p.style.display = 'none');
  document.getElementById('settings-main-filters').style.display = '';
  // Reset user sub-tabs to muted
  document.querySelectorAll('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'muted'));
  document.getElementById('settings-tab-muted').style.display = '';
  document.getElementById('settings-tab-followed').style.display = 'none';

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
        const nsfwMode = document.querySelector('input[name="settings-nsfw"]:checked')?.value || 'hide';
        const payoutType = document.getElementById('settings-payout').value;
        const votePrefs = {
          voteFloor: voteFloor,
          voteMaxWeight: voteMax,
          voteManual: voteManual,
          nsfwMode: nsfwMode,
          payoutType: payoutType,
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
        localStorage.setItem('honeycomb_nsfwMode', nsfwMode);
        localStorage.setItem('honeycomb_payoutType', payoutType);
        applyNsfwMode(nsfwMode);
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

// ── NSFW mode ──
function getNsfwMode() {
  return localStorage.getItem('honeycomb_nsfwMode') || 'hide';
}

function applyNsfwMode(mode) {
  const chip = document.getElementById('nsfw-filter-chip');
  if (chip) chip.style.display = mode === 'filter' ? '' : 'none';
  // If switching away from filter mode, clear any active NSFW filter
  if (mode !== 'filter') {
    const f = Alpine.store('filters');
    f.remove('sentiments', 'nsfw');
  }
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
    item.innerHTML = `<span class="muted-user-name">@${esc(user)}</span><button type="button" class="btn btn-ghost muted-user-unmute" data-action="unmute-user" data-user="${esc(user)}">Unmute</button>`;
    container.appendChild(item);
  });
}
