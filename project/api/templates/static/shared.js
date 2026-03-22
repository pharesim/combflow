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

// ── Hive Keychain Broadcasting ──
function generatePermlink(parentAuthor, parentPermlink) {
  const trunc = parentPermlink.slice(0, 200);
  const ts = new Date().toISOString().replace(/[-:T.Z]/g, '').slice(0, 14).toLowerCase();
  return `re-${parentAuthor}-${trunc}-${ts}z`;
}

function broadcastComment(parentAuthor, parentPermlink, body) {
  return new Promise((resolve, reject) => {
    const auth = getStoredAuth();
    if (!auth) return reject(new Error('Not logged in'));
    if (!isKeychainInstalled()) return reject(new Error('Hive Keychain not installed'));

    const permlink = generatePermlink(parentAuthor, parentPermlink);
    const op = ['comment', {
      parent_author: parentAuthor,
      parent_permlink: parentPermlink,
      author: auth.username,
      permlink: permlink,
      title: '',
      body: body,
      json_metadata: JSON.stringify({ app: 'honeycomb' }),
    }];

    window.hive_keychain.requestBroadcast(auth.username, [op], 'Posting', (response) => {
      if (response.success) {
        resolve({ author: auth.username, permlink });
      } else {
        reject(new Error(response.message || 'Broadcast failed'));
      }
    });
  });
}

function slugify(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 200);
}

function broadcastPost(title, body, tags, communityId) {
  return new Promise((resolve, reject) => {
    const auth = getStoredAuth();
    if (!auth) return reject(new Error('Not logged in'));
    if (!isKeychainInstalled()) return reject(new Error('Hive Keychain not installed'));

    const timestamp = Date.now().toString(16).slice(-8);
    const permlink = slugify(title) + '-' + timestamp;
    const parentPermlink = communityId || (tags.length > 0 ? tags[0] : 'hive');

    // Extract first image from body for json_metadata
    const imgMatch = body.match(/!\[[^\]]*\]\(([^)]+)\)/) || body.match(/https?:\/\/\S+\.(?:jpg|jpeg|png|gif|webp)/i);
    const images = imgMatch ? [imgMatch[1] || imgMatch[0]] : [];

    const metadata = {
      tags: tags,
      image: images,
      app: 'honeycomb/1.0',
    };
    if (communityId) metadata.community = communityId;

    const commentOp = ['comment', {
      parent_author: '',
      parent_permlink: parentPermlink,
      author: auth.username,
      permlink: permlink,
      title: title,
      body: body,
      json_metadata: JSON.stringify(metadata),
    }];

    const ops = [commentOp];

    ops.push(['comment_options', {
      author: auth.username,
      permlink: permlink,
      max_accepted_payout: '1000000.000 HBD',
      percent_hbd: 0,
      allow_votes: true,
      allow_curation_rewards: true,
      extensions: [],
    }]);

    window.hive_keychain.requestBroadcast(auth.username, ops, 'Posting', (response) => {
      if (response.success) {
        resolve({ author: auth.username, permlink });
      } else {
        reject(new Error(response.message || 'Broadcast failed'));
      }
    });
  });
}

function broadcastCrossPost(author, permlink, communityId) {
  return new Promise((resolve, reject) => {
    const auth = getStoredAuth();
    if (!auth) return reject(new Error('Not logged in'));
    if (!isKeychainInstalled()) return reject(new Error('Hive Keychain not installed'));

    const op = ['custom_json', {
      required_auths: [],
      required_posting_auths: [auth.username],
      id: 'community',
      json: JSON.stringify(['reblog', {
        account: auth.username,
        author: author,
        permlink: permlink,
        community: communityId,
      }]),
    }];

    window.hive_keychain.requestBroadcast(auth.username, [op], 'Posting', (response) => {
      if (response.success) {
        resolve();
      } else {
        reject(new Error(response.message || 'Cross-post failed'));
      }
    });
  });
}

// ── Hive Community Subscribe/Unsubscribe ──
function subscribeCommunity(communityId) {
  return new Promise((resolve, reject) => {
    const auth = getStoredAuth();
    if (!auth) return reject(new Error('Not logged in'));
    if (!isKeychainInstalled()) return reject(new Error('Hive Keychain not installed'));

    const op = ['custom_json', {
      required_auths: [],
      required_posting_auths: [auth.username],
      id: 'community',
      json: JSON.stringify(['subscribe', { community: communityId }]),
    }];

    window.hive_keychain.requestBroadcast(auth.username, [op], 'Posting', (response) => {
      if (response.success) {
        resolve();
      } else {
        reject(new Error(response.message || 'Subscribe failed'));
      }
    });
  });
}

function unsubscribeCommunity(communityId) {
  return new Promise((resolve, reject) => {
    const auth = getStoredAuth();
    if (!auth) return reject(new Error('Not logged in'));
    if (!isKeychainInstalled()) return reject(new Error('Hive Keychain not installed'));

    const op = ['custom_json', {
      required_auths: [],
      required_posting_auths: [auth.username],
      id: 'community',
      json: JSON.stringify(['unsubscribe', { community: communityId }]),
    }];

    window.hive_keychain.requestBroadcast(auth.username, [op], 'Posting', (response) => {
      if (response.success) {
        resolve();
      } else {
        reject(new Error(response.message || 'Unsubscribe failed'));
      }
    });
  });
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

// ── Hive markdown rendering ──
// Requires: marked.js, DOMPurify (loaded before this script)
const IFRAME_ALLOW = [
  'youtube.com', 'www.youtube.com',
  'youtube-nocookie.com', 'www.youtube-nocookie.com',
  'player.vimeo.com',
  'play.3speak.tv',
  'www.instagram.com',
  'www.skatehype.com',
  'odysee.com',
  'rumble.com',
  'open.spotify.com',
  'w.soundcloud.com',
  'emb.d.tube',
  'lbry.tv',
];

const ALLOWED_CSS_PROPS = new Set([
  'position', 'padding-bottom', 'height', 'overflow',
  'top', 'left', 'width', 'max-width',
]);

// Global delegated error handler for image proxy fallback.
// onerror function properties don't survive innerHTML serialization,
// so we use data-direct-src attributes + capture-phase listener instead.
document.addEventListener('error', function(e) {
  const img = e.target;
  if (img.tagName === 'IMG' && img.dataset.directSrc) {
    img.src = img.dataset.directSrc;
    delete img.dataset.directSrc;
  }
}, true);

function renderHiveBody(raw) {
  let md = raw;
  md = md.replace(/(^|[\s(])@([a-z0-9][a-z0-9.-]{1,15})(?=[\s,.;:!?)}\]]|$)/gim,
    '$1[@$2](https://peakd.com/@$2)');
  md = md.replace(/(https?:\/\/[^\s<>"\[\]]+\.(?:jpe?g|png|gif|webp|svg)(?:\?[^\s<>"\[\]]*)?)\[(?:Source|source)\]\(([^)]+)\)/gim,
    '![]($1)');
  md = md.replace(/(^|>|\n)\s*(https?:\/\/[^\s<>"]+\.(?:jpe?g|png|gif|webp|svg)(?:\?[^\s<>"]*)?)\s*(?=<|\n|$)/gim,
    '$1\n![]($2)\n');
  // Convert all markdown images to <img> before marked parses.
  // marked would do the same for non-HTML-block images; this also fixes
  // images inside HTML blocks (e.g. <center>) which marked treats as raw text.
  md = md.replace(/!\[([^\]]*)\]\((https?:\/\/[^)]+)\)/gim, '<img alt="$1" src="$2">');
  // Convert [<img src="...">](url) to <a><img></a> — marked won't parse
  // markdown links inside HTML blocks like <center>
  md = md.replace(/\[(<img\s[^>]*>)\]\((https?:\/\/[^)]+)\)/gim, '<a href="$2">$1</a>');
  const html = marked.parse(md, { breaks: true, gfm: true });
  DOMPurify.addHook('uponSanitizeElement', (node, data) => {
    if (data.tagName === 'iframe') {
      const src = node.getAttribute('src') || '';
      try {
        const host = new URL(src).hostname;
        if (!IFRAME_ALLOW.some(d => host === d || host.endsWith('.' + d))) {
          node.remove();
        }
      } catch {
        node.remove();
      }
    }
  });
  const clean = DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [
      'p','br','strong','b','em','i','u','s','del','strike',
      'h1','h2','h3','h4','h5','h6',
      'ul','ol','li',
      'blockquote','pre','code',
      'a','img','iframe',
      'table','thead','tbody','tr','th','td',
      'hr','div','span','sub','sup',
      'center',
    ],
    ALLOWED_ATTR: ['href','src','alt','title','class','width','height',
                   'style','allowfullscreen','frameborder'],
    ALLOW_DATA_ATTR: false,
    ADD_ATTR: ['target', 'data-direct-src'],
  });
  DOMPurify.removeAllHooks();
  const div = document.createElement('div');
  div.innerHTML = clean;
  // Sandbox all inline iframes and set lazy loading
  div.querySelectorAll('iframe').forEach(iframe => {
    iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-popups');
    iframe.loading = 'lazy';
  });
  // Sanitize style attributes — only allow layout-related CSS properties
  div.querySelectorAll('[style]').forEach(el => {
    const raw = el.getAttribute('style');
    const safe = raw.split(';').map(s => s.trim()).filter(s => {
      const prop = s.split(':')[0]?.trim().toLowerCase();
      return prop && ALLOWED_CSS_PROPS.has(prop);
    }).join('; ');
    if (safe) {
      el.setAttribute('style', safe);
    } else {
      el.removeAttribute('style');
    }
  });
  div.querySelectorAll('img').forEach(img => {
    const src = img.getAttribute('src') || '';
    if (src && !src.startsWith('data:')) {
      const fixedSrc = src.replace(/^https?:\/\/(?:cdn\.)?steemitimages\.com\//, 'https://images.hive.blog/');
      if (fixedSrc !== src) img.src = fixedSrc;
      img.dataset.directSrc = fixedSrc;
      if (!/images\.hive\.blog/.test(fixedSrc)) {
        img.src = 'https://images.hive.blog/768x0/' + fixedSrc;
      }
    }
    img.removeAttribute('width');
    img.removeAttribute('height');
    img.loading = 'lazy';
  });
  // Video embeds + external link attributes
  const embeddedVideos = new Set();
  div.querySelectorAll('a').forEach(a => {
    const href = a.getAttribute('href') || '';
    const ytMatch = href.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)/);
    if (ytMatch) {
      const videoKey = `yt:${ytMatch[1]}`;
      if (embeddedVideos.has(videoKey)) {
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
        return;
      }
      embeddedVideos.add(videoKey);
      const embed = document.createElement('div');
      embed.className = 'video-embed';
      const iframe = document.createElement('iframe');
      iframe.src = `https://www.youtube.com/embed/${encodeURIComponent(ytMatch[1])}`;
      iframe.allowFullscreen = true;
      iframe.allow = 'autoplay';
      iframe.loading = 'lazy';
      embed.appendChild(iframe);
      a.replaceWith(embed);
      return;
    }
    const tsMatch = href.match(/3speak\.tv\/watch\?v=([\w.-]+)\/([\w-]+)/);
    if (tsMatch) {
      const videoKey = `3s:${tsMatch[1]}/${tsMatch[2]}`;
      if (embeddedVideos.has(videoKey)) {
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
        return;
      }
      embeddedVideos.add(videoKey);
      const embed = document.createElement('div');
      embed.className = 'video-embed';
      const iframe = document.createElement('iframe');
      iframe.src = `https://play.3speak.tv/watch?v=${encodeURIComponent(tsMatch[1])}/${encodeURIComponent(tsMatch[2])}&mode=iframe&layout=desktop`;
      iframe.allowFullscreen = true;
      iframe.allow = 'autoplay';
      iframe.loading = 'lazy';
      iframe.sandbox = 'allow-scripts allow-same-origin allow-popups';
      embed.appendChild(iframe);
      a.replaceWith(embed);
      return;
    }
    const igMatch = href.match(/instagram\.com\/reel\/([\w-]+)/);
    if (igMatch) {
      const videoKey = `ig:${igMatch[1]}`;
      if (embeddedVideos.has(videoKey)) {
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
        return;
      }
      embeddedVideos.add(videoKey);
      const embed = document.createElement('div');
      embed.className = 'video-embed ig-embed';
      const iframe = document.createElement('iframe');
      iframe.src = `https://www.instagram.com/reel/${encodeURIComponent(igMatch[1])}/embed/`;
      iframe.allowFullscreen = true;
      iframe.loading = 'lazy';
      iframe.sandbox = 'allow-scripts allow-same-origin allow-popups';
      embed.appendChild(iframe);
      a.replaceWith(embed);
      return;
    }
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
  });
  return div.innerHTML;
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
