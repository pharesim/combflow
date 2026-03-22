// ── Hive Keychain Auth ──
// JWT is stored in an httpOnly cookie (set by the server).
// localStorage only holds the username and expiry for UI display/checks.
const AUTH_USER_KEY = 'honeycomb_user';
const AUTH_EXPIRES_KEY = 'honeycomb_expires';

function isKeychainInstalled() {
  return typeof window.hive_keychain !== 'undefined';
}

function getStoredAuth() {
  const user = localStorage.getItem(AUTH_USER_KEY);
  const expires = localStorage.getItem(AUTH_EXPIRES_KEY);
  if (!user || !expires) return null;
  if (new Date(expires).getTime() < Date.now()) {
    logout();
    return null;
  }
  return { username: user };
}

function authHeaders() {
  // JWT is sent automatically via httpOnly cookie — no manual header needed
  return {};
}

async function loginWithKeychain(username, _retry) {
  const chalRes = await fetch('/api/auth/challenge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username }),
  });
  if (!chalRes.ok) throw new Error('Could not get challenge from server');
  const { challenge } = await chalRes.json();

  return new Promise((resolve, reject) => {
    if (!isKeychainInstalled()) return reject(new Error('Hive Keychain not installed'));
    window.hive_keychain.requestSignBuffer(username, challenge, 'Posting', (response) => {
      if (response.success) {
        fetch('/api/auth/verify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username,
            challenge,
            signature: response.result,
          }),
        })
        .then(async r => {
          if (!r.ok && r.status === 400 && r.headers.get('X-Retry') && !_retry) {
            return loginWithKeychain(username, true).then(resolve, reject);
          }
          if (!r.ok) {
            let msg;
            try { msg = (await r.json()).detail; } catch(e) {}
            if (!msg) {
              const defaults = {
                403: 'Login denied \u2014 your account reputation is too low',
                429: 'Too many login attempts \u2014 please wait a moment',
                503: 'Authentication service temporarily unavailable \u2014 please try again',
              };
              msg = defaults[r.status] || 'Login failed \u2014 please try again';
            }
            throw new Error(msg);
          }
          return r.json();
        })
        .then(data => {
          if (!data) return; // handled by retry branch
          // Server sets httpOnly cookie with the JWT.
          // We only store username + expiry for UI purposes.
          localStorage.setItem(AUTH_USER_KEY, data.username);
          localStorage.setItem(AUTH_EXPIRES_KEY, data.expires_at);
          resolve(data);
        })
        .catch(reject);
      } else {
        reject(new Error(response.message || 'Keychain signing cancelled'));
      }
    });
  });
}

async function logout() {
  // Clear the httpOnly cookie via server endpoint
  try { await fetch('/api/auth/logout', { method: 'POST' }); } catch(e) {}
  localStorage.removeItem(AUTH_USER_KEY);
  localStorage.removeItem(AUTH_EXPIRES_KEY);
}

// ── Read post tracking ──
const READ_KEY = 'honeycomb_read';
const READ_MAX = 500;
let _readSet = null;

function getReadSet() {
  if (_readSet) return _readSet;
  try {
    _readSet = new Set(JSON.parse(localStorage.getItem(READ_KEY) || '[]'));
  } catch(e) {
    _readSet = new Set();
  }
  return _readSet;
}

function markRead(key) {
  const s = getReadSet();
  if (s.has(key)) return;
  s.add(key);
  // Cap to most recent entries
  const arr = Array.from(s);
  if (arr.length > READ_MAX) {
    _readSet = new Set(arr.slice(arr.length - READ_MAX));
  }
  localStorage.setItem(READ_KEY, JSON.stringify(Array.from(_readSet)));
  // Apply .read class to matching elements
  document.querySelectorAll(`[data-key="${CSS.escape(key)}"]`).forEach(el => el.classList.add('read'));
}

function isRead(key) {
  return getReadSet().has(key);
}

// ── Validation helpers ──
const VALID_SENTIMENTS = new Set(['positive', 'negative', 'neutral']);
function safeSentiment(s) {
  return VALID_SENTIMENTS.has(s) ? s : '';
}

function safeCssUrl(url) {
  if (!url) return '';
  if (!/^https?:\/\//i.test(url)) return '';
  return url.replace(/[')(\\]/g, encodeURIComponent);
}

// ── HTML escaping (safe for both content and attribute contexts) ──
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function avatarHtml(author, size) {
  size = size || 16;
  return '<img class="author-avatar" src="https://images.hive.blog/u/' + encodeURIComponent(author) + '/avatar/small" alt="" width="' + size + '" height="' + size + '">';
}

// ── Focus trap for modals ──
function trapFocus(container) {
  container._prevFocus = document.activeElement;
  const focusable = container.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])');
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  container._trapHandler = function(e) {
    if (e.key !== 'Tab') return;
    if (e.shiftKey) {
      if (document.activeElement === first) { e.preventDefault(); last.focus(); }
    } else {
      if (document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  };
  container.addEventListener('keydown', container._trapHandler);
  first.focus();
}

function releaseFocus(container) {
  if (container._trapHandler) {
    container.removeEventListener('keydown', container._trapHandler);
    delete container._trapHandler;
  }
  if (container._prevFocus && container._prevFocus.focus) {
    container._prevFocus.focus();
    delete container._prevFocus;
  }
}

// ── Screen reader announcements ──
let _srLive = null;
function announceToSR(text) {
  if (!_srLive) {
    _srLive = document.createElement('div');
    _srLive.setAttribute('aria-live', 'polite');
    _srLive.setAttribute('role', 'status');
    _srLive.className = 'sr-only';
    document.body.appendChild(_srLive);
  }
  _srLive.textContent = '';
  requestAnimationFrame(() => { _srLive.textContent = text; });
}

// ── Toast notifications ──
let _toastContainer = null;
function showToast(message, type, duration) {
  type = type || 'info';
  duration = duration || 3000;
  if (!_toastContainer) {
    _toastContainer = document.createElement('div');
    _toastContainer.className = 'toast-container';
    document.body.appendChild(_toastContainer);
  }
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
  toast.textContent = message;
  _toastContainer.appendChild(toast);
  setTimeout(() => { toast.remove(); }, duration);
}
