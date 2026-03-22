// ── Auth UI ──

function showLoginPrompt() {
  const overlay = document.getElementById('login-overlay');
  document.getElementById('login-error').textContent = '';
  const hasKeychain = isKeychainInstalled();
  document.getElementById('login-form').style.display = hasKeychain ? '' : 'none';
  document.getElementById('login-onboarding').style.display = hasKeychain ? 'none' : '';
  document.getElementById('login-signup-link').style.display = hasKeychain ? '' : 'none';
  Alpine.store('app').loginOpen = true;
  trapFocus(overlay.querySelector('.login-box'));
  if (hasKeychain) {
    const input = document.getElementById('login-username');
    if (input) input.focus();
  }
}

function closeLogin() {
  const overlay = document.getElementById('login-overlay');
  releaseFocus(overlay.querySelector('.login-box'));
  Alpine.store('app').loginOpen = false;
}

function openSignup() {
  closeLogin();
  document.getElementById('signup-iframe').src = 'https://hivedapps.com/';
  Alpine.store('app').signupOpen = true;
}
function closeSignup() {
  Alpine.store('app').signupOpen = false;
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
    Alpine.store('app').currentUser = input;
    fetchUserCommunities(input).then(list => { state.userCommunities = list; });
    fetchMutedList(); // background fetch
    fetchFollowedList(); // background fetch
    const prefs = await loadAndApplyPreferences();
    if (isFirstLogin(prefs)) {
      showSettingsModal();
    } else {
      const fl = Alpine.store('filters');
      if (fl.categories.size > 0 || fl.languages.size > 0 || fl.sentiments.size > 0) {
        applyFilters();
      }
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
  state.userCommunities = null;
  state.votedPosts = {};
  state.manaCache = null;
  setMyCommunitiesActive(false);
  setFollowingActive(false);
  Alpine.store('app').currentUser = null;
  Alpine.store('app').authDropdownOpen = false;
  resetFilters();
}
