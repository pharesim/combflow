// ── Hive Keychain Auth (pure client-side) ──
// Identity is verified by Keychain signing a message. No server auth needed.
const AUTH_USER_KEY = 'honeycomb_user';

function isKeychainInstalled() {
  return typeof window.hive_keychain !== 'undefined';
}

function getStoredAuth() {
  const user = localStorage.getItem(AUTH_USER_KEY);
  if (!user) return null;
  return { username: user };
}

async function loginWithKeychain(username) {
  return new Promise((resolve, reject) => {
    if (!isKeychainInstalled()) return reject(new Error('Hive Keychain not installed'));
    const message = 'hivecomb_login_' + Date.now();
    window.hive_keychain.requestSignBuffer(username, message, 'Posting', (response) => {
      if (response.success) {
        localStorage.setItem(AUTH_USER_KEY, username);
        resolve({ username });
      } else {
        reject(new Error(response.message || 'Keychain signing cancelled'));
      }
    });
  });
}

function logout() {
  localStorage.removeItem(AUTH_USER_KEY);
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
