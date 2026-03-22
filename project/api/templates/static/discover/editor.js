// ── Post Editor ──
const DRAFT_KEY = 'honeycomb_draft';
let _draftTimer = null;
let _categoryLeafs = []; // populated from /categories response

function populateEditorCommunities(communities) {
  const select = document.getElementById('editor-community-select');
  // Remove all options except "My Blog"
  while (select.options.length > 1) select.remove(1);
  if (!communities || communities.length === 0) return;
  communities.forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.id;
    opt.textContent = `${c.name} (${c.role})`;
    select.appendChild(opt);
  });
}

function onCommunitySelect() {
  const select = document.getElementById('editor-community-select');
  const crosspostLabel = document.getElementById('editor-crosspost-label');
  if (select.value) {
    crosspostLabel.style.display = '';
  } else {
    crosspostLabel.style.display = 'none';
    document.getElementById('editor-crosspost').checked = false;
  }
  autoSaveDraft();
}

// ── Smart community suggestions based on tags ──
function updateCommunitySuggestion() {
  const hint = document.getElementById('editor-community-hint');
  const select = document.getElementById('editor-community-select');
  const tags = Alpine.store('app').editorTags;
  if (!state.userCommunities || !state.userCommunities.length || select.value) {
    hint.style.display = 'none';
    return;
  }
  // Check if any tag matches a community name
  for (const tag of tags) {
    const tagLower = tag.toLowerCase();
    const match = state.userCommunities.find(c =>
      c.name.toLowerCase().includes(tagLower) || tagLower.includes(c.name.toLowerCase().split(' ')[0])
    );
    if (match) {
      hint.textContent = `Post to ${match.name}?`;
      hint.onclick = () => {
        select.value = match.id;
        onCommunitySelect();
        hint.style.display = 'none';
      };
      hint.style.display = '';
      return;
    }
  }
  hint.style.display = 'none';
}

async function openEditor() {
  const auth = getStoredAuth();
  if (!auth) { showLoginPrompt(); return; }
  const s = Alpine.store('app');
  const modal = document.getElementById('editor-modal');
  // Restore draft
  try {
    const draft = JSON.parse(localStorage.getItem(DRAFT_KEY));
    if (draft) {
      document.getElementById('editor-title').value = draft.title || '';
      document.getElementById('editor-body').value = draft.body || '';
      document.getElementById('editor-description').value = draft.description || '';
      s.editorTags = draft.tags || [];
      if (draft.communityId) {
        document.getElementById('editor-community-select').value = draft.communityId;
      }
      if (draft.location) {
        _selectedLocation = draft.location;
        document.getElementById('editor-location-btn').classList.add('has-location');
        _updateLocationBadge();
      }
    }
  } catch(e) {}
  updateEditorTitleCount();
  updateEditorDescCount();
  s.editorTab = 'write';
  showEditorTab('write');
  s.editorOpen = true;
  trapFocus(modal.querySelector('.modal'));

  // Fetch communities in background
  const loading = document.getElementById('editor-community-loading');
  loading.style.display = '';
  state.userCommunities = await fetchUserCommunities(auth.username);
  loading.style.display = 'none';
  if (state.userCommunities) {
    populateEditorCommunities(state.userCommunities);
    // Restore draft community selection
    try {
      const draft = JSON.parse(localStorage.getItem(DRAFT_KEY));
      if (draft && draft.communityId) {
        document.getElementById('editor-community-select').value = draft.communityId;
        onCommunitySelect();
      }
    } catch(e) {}
  }
}

function confirmCloseEditor() {
  const title = document.getElementById('editor-title').value.trim();
  const body = document.getElementById('editor-body').value.trim();
  const tags = Alpine.store('app').editorTags;
  if (title || body || tags.length > 0) {
    saveDraft();
    showToast('Draft saved', 'info');
  }
  closeEditor();
}

function closeEditor() {
  const modal = document.getElementById('editor-modal');
  releaseFocus(modal.querySelector('.modal'));
  Alpine.store('app').editorOpen = false;
  document.getElementById('editor-tag-suggestions').style.display = 'none';
  document.getElementById('editor-community-hint').style.display = 'none';
}

function updateEditorTitleCount() {
  const input = document.getElementById('editor-title');
  document.getElementById('editor-title-count').textContent = input.value.length + '/256';
}

function updateEditorDescCount() {
  const input = document.getElementById('editor-description');
  document.getElementById('editor-desc-count').textContent = input.value.length + '/120';
}

// ── Editor toolbar actions ──
function editorInsert(before, after) {
  const ta = document.getElementById('editor-body');
  const start = ta.selectionStart, end = ta.selectionEnd;
  const sel = ta.value.substring(start, end);
  const replacement = before + (sel || 'text') + after;
  ta.setRangeText(replacement, start, end, 'select');
  ta.selectionStart = start + before.length;
  ta.selectionEnd = start + before.length + (sel || 'text').length;
  ta.focus();
  autoSaveDraft();
}

function editorInsertLine(prefix) {
  const ta = document.getElementById('editor-body');
  const start = ta.selectionStart;
  // Find start of current line
  const lineStart = ta.value.lastIndexOf('\n', start - 1) + 1;
  const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
  const text = sel || '';
  ta.setRangeText(prefix + text, lineStart === start ? start : start, ta.selectionEnd, 'end');
  ta.focus();
  autoSaveDraft();
}

function editorInsertLink() {
  const ta = document.getElementById('editor-body');
  const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
  const isUrl = /^https?:\/\//.test(sel);
  if (isUrl) {
    editorInsert('[link text](', ')');
  } else {
    editorInsert('[' + (sel || 'link text') + '](', ')');
    if (sel) {
      // Place cursor inside the url part
      const pos = ta.selectionEnd - 1;
      ta.selectionStart = pos;
      ta.selectionEnd = pos;
    }
  }
}

function editorInsertImage() {
  document.getElementById('editor-image-input').click();
}

// ── Image upload to Hive image hosting ──
let _imageUploading = false;

async function uploadImageToHive(file) {
  const auth = getStoredAuth();
  if (!auth) { showToast('Log in to upload images', 'error'); return null; }
  if (_imageUploading) { showToast('Upload already in progress', 'info'); return null; }
  if (file.size > 10 * 1024 * 1024) { showToast('Image too large (max 10 MB)', 'error'); return null; }

  _imageUploading = true;
  const ta = document.getElementById('editor-body');
  ta.disabled = true;
  showToast('Uploading image...', 'info');

  try {
    const buf = await file.arrayBuffer();
    const prefix = new TextEncoder().encode('ImageSigningChallenge');
    const combined = new Uint8Array(prefix.length + buf.byteLength);
    combined.set(prefix, 0);
    combined.set(new Uint8Array(buf), prefix.length);
    const hashBuf = await crypto.subtle.digest('SHA-256', combined);
    const hashHex = Array.from(new Uint8Array(hashBuf)).map(b => b.toString(16).padStart(2, '0')).join('');

    const signature = await new Promise((resolve, reject) => {
      if (!window.hive_keychain) { reject(new Error('Hive Keychain not found')); return; }
      window.hive_keychain.requestSignBuffer(auth.username, hashHex, 'Posting', res => {
        if (res.success) resolve(res.result);
        else reject(new Error(res.message || 'Signature rejected'));
      });
    });

    const form = new FormData();
    form.append('file', file);
    const resp = await fetch(`https://images.hive.blog/${auth.username}/${signature}`, {
      method: 'POST',
      body: form,
    });
    if (!resp.ok) throw new Error('Upload failed (' + resp.status + ')');
    const data = await resp.json();
    if (!data.url) throw new Error('No URL in upload response');

    showToast('Image uploaded', 'success');
    return data.url;
  } catch (e) {
    showToast(e.message || 'Image upload failed', 'error');
    return null;
  } finally {
    _imageUploading = false;
    ta.disabled = false;
    ta.focus();
  }
}

function insertImageMarkdown(url) {
  const ta = document.getElementById('editor-body');
  const pos = ta.selectionStart;
  const md = `![image](${url})\n`;
  ta.setRangeText(md, pos, pos, 'end');
  ta.focus();
  autoSaveDraft();
}

async function handleImageFile(file) {
  if (!file || !file.type.startsWith('image/')) return;
  const url = await uploadImageToHive(file);
  if (url) insertImageMarkdown(url);
}

function onEditorPaste(e) {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      e.preventDefault();
      handleImageFile(item.getAsFile());
      return;
    }
  }
}

function onEditorDragOver(e) {
  if (e.dataTransfer && e.dataTransfer.types.includes('Files')) {
    e.preventDefault();
    e.currentTarget.classList.add('drag-over');
  }
}

function onEditorDragLeave(e) {
  e.currentTarget.classList.remove('drag-over');
}

async function onEditorDrop(e) {
  e.preventDefault();
  e.currentTarget.classList.remove('drag-over');
  const files = e.dataTransfer && e.dataTransfer.files;
  if (!files) return;
  for (const file of files) {
    if (file.type.startsWith('image/')) {
      await handleImageFile(file);
    }
  }
}

function onEditorImagePick(e) {
  const files = e.target.files;
  if (!files || !files.length) return;
  for (const file of files) handleImageFile(file);
  e.target.value = '';
}

function editorInsertTable() {
  editorInsert('\n| Column 1 | Column 2 | Column 3 |\n|----------|----------|----------|\n| ', ' |  |  |\n');
}

// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
  const ta = document.getElementById('editor-body');
  if (!ta || document.activeElement !== ta) return;
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey) {
    if (e.key === 'b') { e.preventDefault(); editorInsert('**', '**'); }
    else if (e.key === 'i') { e.preventDefault(); editorInsert('*', '*'); }
    else if (e.key === 'k') { e.preventDefault(); editorInsertLink(); }
  }
});

function showMarkdownHelp() {
  Alpine.store('app').mdHelpOpen = true;
  trapFocus(document.querySelector('#md-help-modal .modal'));
}
function closeMdHelp() {
  const modal = document.getElementById('md-help-modal');
  releaseFocus(modal.querySelector('.modal'));
  Alpine.store('app').mdHelpOpen = false;
}

function showEditorTab(tab) {
  const s = Alpine.store('app');
  s.editorTab = tab;
  const textarea = document.getElementById('editor-body');
  const preview = document.getElementById('editor-preview');
  const toolbar = document.getElementById('editor-toolbar');
  if (tab === 'preview') {
    preview.innerHTML = renderHiveBody(textarea.value || '');
    preview.style.display = '';
    textarea.style.display = 'none';
    toolbar.style.display = 'none';
  } else {
    preview.style.display = 'none';
    textarea.style.display = '';
    toolbar.style.display = '';
  }
}

// ── Editor tag rendering (Alpine x-html) ──
function renderEditorTagsHtml() {
  const tags = Alpine.store('app').editorTags;
  let html = '';
  tags.forEach((tag, i) => {
    html += '<span class="editor-tag">' + esc(tag) + '<button type="button" onclick="removeEditorTag(' + i + ')" aria-label="Remove tag">&times;</button></span>';
  });
  return html;
}

function addEditorTag(tag) {
  const s = Alpine.store('app');
  tag = tag.toLowerCase().replace(/[^a-z0-9-]/g, '').slice(0, 50);
  if (!tag || s.editorTags.includes(tag) || s.editorTags.length >= 10) return;
  s.editorTags = [...s.editorTags, tag];
  document.getElementById('editor-tags-input').value = '';
  document.getElementById('editor-tag-suggestions').style.display = 'none';
  autoSaveDraft();
  updateCommunitySuggestion();
}

function removeEditorTag(i) {
  const s = Alpine.store('app');
  const tags = [...s.editorTags];
  tags.splice(i, 1);
  s.editorTags = tags;
  autoSaveDraft();
  updateCommunitySuggestion();
}

function handleTagKey(e) {
  if (e.key === 'Enter' || e.key === ',') {
    e.preventDefault();
    const val = e.target.value.trim().replace(/,$/,'');
    if (val) addEditorTag(val);
  }
}

function showTagSuggestions() {
  const input = document.getElementById('editor-tags-input');
  const sugBox = document.getElementById('editor-tag-suggestions');
  const q = input.value.trim().toLowerCase();
  const tags = Alpine.store('app').editorTags;
  if (!q || q.length < 2) { sugBox.style.display = 'none'; return; }
  const matches = _categoryLeafs.filter(c =>
    !tags.includes(c) && (c.startsWith(q) || c.includes(q))
  ).slice(0, 8);
  if (!matches.length) { sugBox.style.display = 'none'; return; }
  sugBox.innerHTML = '';
  matches.forEach(m => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'editor-tag-suggestion';
    btn.textContent = m;
    btn.onclick = () => addEditorTag(m);
    sugBox.appendChild(btn);
  });
  sugBox.style.display = '';
}

function saveDraft() {
  localStorage.setItem(DRAFT_KEY, JSON.stringify({
    title: document.getElementById('editor-title').value,
    body: document.getElementById('editor-body').value,
    description: document.getElementById('editor-description').value,
    tags: Alpine.store('app').editorTags,
    communityId: document.getElementById('editor-community-select').value || null,
    location: _selectedLocation || null,
  }));
}

function autoSaveDraft() {
  clearTimeout(_draftTimer);
  _draftTimer = setTimeout(saveDraft, 10000);
}

function clearDraft() {
  localStorage.removeItem(DRAFT_KEY);
  clearTimeout(_draftTimer);
}

async function publishPost() {
  const s = Alpine.store('app');
  const title = document.getElementById('editor-title').value.trim();
  const body = document.getElementById('editor-body').value.trim();
  const description = document.getElementById('editor-description').value.trim();
  const communityId = document.getElementById('editor-community-select').value || null;
  const crossPost = communityId && document.getElementById('editor-crosspost').checked;
  const btn = document.getElementById('editor-publish-btn');

  if (!title) { showToast('Title is required', 'error'); return; }
  if (!body) { showToast('Body is required', 'error'); return; }

  btn.disabled = true;
  btn.textContent = 'Publishing...';
  try {
    const result = await broadcastPost(title, body, s.editorTags, communityId, description);
    showToast('Post published!', 'success');

    // Cross-post to blog if requested
    if (crossPost) {
      try {
        await broadcastCrossPost(result.author, result.permlink, communityId);
        showToast('Cross-posted to my blog', 'success');
      } catch(e) {
        showToast('Post published to community. Cross-post failed \u2014 you can reblog manually.', 'info', 5000);
      }
    }

    clearDraft();
    document.getElementById('editor-title').value = '';
    document.getElementById('editor-body').value = '';
    document.getElementById('editor-description').value = '';
    document.getElementById('editor-community-select').value = '';
    document.getElementById('editor-crosspost').checked = false;
    document.getElementById('editor-crosspost-label').style.display = 'none';
    document.getElementById('editor-community-hint').style.display = 'none';
    _selectedLocation = null;
    _locationAutoFilled = false;
    if (_locationMarker && _locationMap) { _locationMap.removeLayer(_locationMarker); _locationMarker = null; }
    document.getElementById('editor-location-btn').classList.remove('has-location');
    _updateLocationBadge();
    s.editorTags = [];
    closeEditor();
    // Navigate to the new post
    showToast('Your post will appear in the feed once processed', 'info', 5000);
    history.pushState({ author: result.author, permlink: result.permlink },
      '', `/@${result.author}/${result.permlink}`);
    openModal({ author: result.author, permlink: result.permlink }, true);
  } catch(e) {
    showToast(e.message || 'Could not publish post', 'error');
  }
  btn.disabled = false;
  btn.textContent = 'Publish';
}

// Warn on navigation if draft exists
window.addEventListener('beforeunload', e => {
  const title = document.getElementById('editor-title');
  const body = document.getElementById('editor-body');
  if (Alpine.store('app').editorOpen &&
      (title.value.trim() || body.value.trim())) {
    saveDraft();
    e.preventDefault();
  }
});
