// ── @-Mention Autocomplete ──
// Provides username autocomplete for textareas when typing @prefix.
// Works on #editor-body and dynamically created .comment-textarea elements.

let _mentionDebounceTimer = null;
let _mentionCache = {};       // prefix -> { ts, results }
const MENTION_CACHE_TTL = 10000; // 10s
const MENTION_DEBOUNCE = 280;
const MENTION_MIN_CHARS = 2;
const MENTION_MAX_RESULTS = 8;

let _mentionDropdown = null;
let _mentionTarget = null;    // textarea currently showing mentions
let _mentionStart = -1;       // index of the '@' character
let _mentionSelectedIdx = 0;

function _getMentionDropdown() {
  if (!_mentionDropdown) {
    _mentionDropdown = document.createElement('div');
    _mentionDropdown.className = 'mention-dropdown';
    _mentionDropdown.style.display = 'none';
    // Prevent overscroll from bubbling to parent (which would close the dropdown)
    _mentionDropdown.addEventListener('wheel', e => {
      const el = _mentionDropdown;
      const atTop = el.scrollTop === 0 && e.deltaY < 0;
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight && e.deltaY > 0;
      if (atTop || atBottom) e.preventDefault();
    }, { passive: false });
    document.body.appendChild(_mentionDropdown);
  }
  return _mentionDropdown;
}

function _extractMentionPrefix(textarea) {
  const pos = textarea.selectionStart;
  const text = textarea.value.substring(0, pos);
  // Walk backwards to find @ not preceded by a word char
  const match = text.match(/@(\w{2,})$/);
  if (!match) return null;
  const atIdx = pos - match[0].length;
  // Ensure @ is at start or preceded by whitespace/newline
  if (atIdx > 0 && /\w/.test(text[atIdx - 1])) return null;
  return { prefix: match[1].toLowerCase(), atIdx };
}

function _getCaretCoords(textarea, charIdx) {
  const mirror = document.createElement('div');
  const style = getComputedStyle(textarea);
  for (const prop of ['fontFamily','fontSize','fontWeight','lineHeight','letterSpacing',
    'wordSpacing','textIndent','padding','paddingTop','paddingRight','paddingBottom',
    'paddingLeft','borderWidth','boxSizing','whiteSpace','wordWrap','overflowWrap','tabSize']) {
    mirror.style[prop] = style[prop];
  }
  mirror.style.position = 'absolute';
  mirror.style.left = '-9999px';
  mirror.style.top = '-9999px';
  mirror.style.width = textarea.clientWidth + 'px';
  mirror.style.whiteSpace = 'pre-wrap';
  mirror.style.wordWrap = 'break-word';

  const text = textarea.value.substring(0, charIdx);
  mirror.textContent = text;
  const marker = document.createElement('span');
  marker.textContent = '|';
  mirror.appendChild(marker);
  document.body.appendChild(mirror);

  const coords = { top: marker.offsetTop - textarea.scrollTop, left: marker.offsetLeft };
  document.body.removeChild(mirror);
  return coords;
}

function _positionDropdown(textarea) {
  const dd = _getMentionDropdown();
  const coords = _getCaretCoords(textarea, _mentionStart);
  const rect = textarea.getBoundingClientRect();
  const lineHeight = parseFloat(getComputedStyle(textarea).lineHeight) || 20;
  const ddWidth = Math.min(rect.width, 280);

  let top = rect.top + window.scrollY + coords.top + lineHeight + 4;
  let left = rect.left + window.scrollX + coords.left;

  // Clamp left so dropdown doesn't overflow right edge
  const maxLeft = window.innerWidth + window.scrollX - ddWidth - 8;
  if (left > maxLeft) left = maxLeft;
  if (left < 8) left = 8;

  dd.style.position = 'absolute';
  dd.style.width = ddWidth + 'px';
  dd.style.zIndex = '10001';
  dd.style.left = left + 'px';

  // If dropdown would go below viewport, show above cursor instead
  dd.style.top = top + 'px';
  dd.style.display = '';
  const ddRect = dd.getBoundingClientRect();
  if (ddRect.bottom > window.innerHeight) {
    top = rect.top + window.scrollY + coords.top - dd.offsetHeight - 4;
    dd.style.top = top + 'px';
  }
}

function _renderMentionResults(results) {
  const dd = _getMentionDropdown();
  dd.innerHTML = '';
  if (!results.length) { dd.style.display = 'none'; return; }
  results.forEach((name, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'mention-item' + (i === _mentionSelectedIdx ? ' selected' : '');
    const img = document.createElement('img');
    img.src = `https://images.hive.blog/u/${name}/avatar/small`;
    img.className = 'mention-avatar';
    img.width = 24;
    img.height = 24;
    img.alt = '';
    img.loading = 'lazy';
    img.onerror = function() { this.style.display = 'none'; };
    btn.appendChild(img);
    btn.appendChild(document.createTextNode(name));
    btn.dataset.username = name;
    btn.addEventListener('mousedown', e => {
      e.preventDefault(); // prevent textarea blur
      _insertMention(name);
    });
    dd.appendChild(btn);
  });
  dd.style.display = '';
}

function _insertMention(username) {
  if (!_mentionTarget || _mentionStart < 0) return;
  const ta = _mentionTarget;
  const pos = ta.selectionStart;
  const before = ta.value.substring(0, _mentionStart);
  const after = ta.value.substring(pos);
  const insert = '@' + username + ' ';
  ta.value = before + insert + after;
  const newPos = _mentionStart + insert.length;
  ta.selectionStart = newPos;
  ta.selectionEnd = newPos;
  ta.focus();
  closeMentionDropdown();
  // Trigger draft save for editor
  if (ta.id === 'editor-body') autoSaveDraft();
}

function closeMentionDropdown() {
  const dd = _getMentionDropdown();
  dd.style.display = 'none';
  dd.innerHTML = '';
  _mentionTarget = null;
  _mentionStart = -1;
  _mentionSelectedIdx = 0;
  clearTimeout(_mentionDebounceTimer);
}

async function _lookupAccounts(prefix) {
  // Check cache
  const cached = _mentionCache[prefix];
  if (cached && Date.now() - cached.ts < MENTION_CACHE_TTL) return cached.results;
  const results = await hiveRpc('condenser_api.lookup_accounts', [prefix, MENTION_MAX_RESULTS]);
  if (results && Array.isArray(results)) {
    _mentionCache[prefix] = { ts: Date.now(), results };
    return results;
  }
  return [];
}

function handleMentionInput(textarea) {
  clearTimeout(_mentionDebounceTimer);
  const extracted = _extractMentionPrefix(textarea);
  if (!extracted) { closeMentionDropdown(); return; }

  _mentionTarget = textarea;
  _mentionStart = extracted.atIdx;
  _mentionSelectedIdx = 0;

  _mentionDebounceTimer = setTimeout(async () => {
    const results = await _lookupAccounts(extracted.prefix);
    // Verify textarea is still focused and prefix hasn't changed
    if (_mentionTarget !== textarea) return;
    const current = _extractMentionPrefix(textarea);
    if (!current || current.prefix !== extracted.prefix) return;
    _renderMentionResults(results);
    _positionDropdown(textarea);
  }, MENTION_DEBOUNCE);
}

function handleMentionKeydown(textarea, e) {
  const dd = _getMentionDropdown();
  if (dd.style.display === 'none') return false;
  const items = dd.querySelectorAll('.mention-item');
  if (!items.length) return false;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    _mentionSelectedIdx = Math.min(_mentionSelectedIdx + 1, items.length - 1);
    items.forEach((el, i) => el.classList.toggle('selected', i === _mentionSelectedIdx));
    return true;
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault();
    _mentionSelectedIdx = Math.max(_mentionSelectedIdx - 1, 0);
    items.forEach((el, i) => el.classList.toggle('selected', i === _mentionSelectedIdx));
    return true;
  }
  if (e.key === 'Enter' || e.key === 'Tab') {
    const selected = items[_mentionSelectedIdx];
    if (selected) {
      e.preventDefault();
      _insertMention(selected.dataset.username);
      return true;
    }
  }
  if (e.key === 'Escape') {
    e.preventDefault();
    closeMentionDropdown();
    return true;
  }
  return false;
}

// Close dropdown when clicking outside, scrolling outside, or resizing
document.addEventListener('mousedown', e => {
  if (_mentionDropdown && _mentionDropdown.style.display !== 'none' && !_mentionDropdown.contains(e.target)) {
    closeMentionDropdown();
  }
});
window.addEventListener('resize', () => {
  if (_mentionDropdown && _mentionDropdown.style.display !== 'none') closeMentionDropdown();
});
document.addEventListener('scroll', e => {
  if (_mentionDropdown && _mentionDropdown.style.display !== 'none' &&
      !_mentionDropdown.contains(e.target)) closeMentionDropdown();
}, true);
