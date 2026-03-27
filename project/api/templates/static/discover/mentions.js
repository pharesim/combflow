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

function _positionDropdown(textarea) {
  const dd = _getMentionDropdown();
  const rect = textarea.getBoundingClientRect();
  // Position below the textarea (simple approach — cursor positioning in textareas is complex)
  const top = rect.bottom + window.scrollY + 4;
  const left = rect.left + window.scrollX;
  dd.style.position = 'absolute';
  dd.style.top = top + 'px';
  dd.style.left = left + 'px';
  dd.style.width = Math.min(rect.width, 280) + 'px';
  dd.style.zIndex = '10001';
}

function _renderMentionResults(results) {
  const dd = _getMentionDropdown();
  dd.innerHTML = '';
  if (!results.length) { dd.style.display = 'none'; return; }
  results.forEach((name, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'mention-item' + (i === _mentionSelectedIdx ? ' selected' : '');
    btn.textContent = '@' + name;
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
    _positionDropdown(textarea);
    _renderMentionResults(results);
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

// Close dropdown when clicking outside
document.addEventListener('click', e => {
  if (_mentionDropdown && !_mentionDropdown.contains(e.target)) {
    closeMentionDropdown();
  }
}, true);
