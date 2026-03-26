// ── Comments ──
// relativeTime() is in shared.js

// Render a single comment + its children recursively as HTML string.
// Called from x-html in the template via renderCommentsHtml().
function renderComment(comment, depth) {
  const maxVisualDepth = 4;
  const flatClass = depth >= maxVisualDepth ? ' comment-children-depth4' : '';
  const bodyHtml = renderHiveBody(comment.body || '');
  const rep = comment.reputation != null ? comment.reputation.toFixed(0) : '';

  let childrenHtml = '';
  if (comment.children && comment.children.length > 0) {
    const collapsed = comment.children.length > 5;
    const toggleId = 'ct-' + comment.author + '-' + comment.permlink.slice(0, 12);
    childrenHtml = '<div class="comment-children' + flatClass + '"' +
      (collapsed ? ' id="' + esc(toggleId) + '" style="display:none"' : '') + '>';
    comment.children.forEach(child => {
      childrenHtml += renderComment(child, depth + 1);
    });
    childrenHtml += '</div>';
    if (collapsed) {
      childrenHtml = '<button type="button" class="comment-toggle" data-action="toggle-comment-children" data-toggle-id="' + esc(toggleId) + '">' +
        comment.children.length + ' replies &#9660;</button>' + childrenHtml;
    }
  }

  const voteKey = comment.author + '/' + comment.permlink;
  const isVoted = comment.voted || state.votedPosts[voteKey];
  const voteBtn = getStoredAuth()
    ? '<button type="button" class="comment-toggle comment-vote-btn' + (isVoted ? ' voted' : '') + '" data-action="comment-vote" data-author="' + esc(comment.author) + '" data-permlink="' + esc(comment.permlink) + '">' +
      '&#9650; <span class="comment-vote-count">' + (comment.net_votes || 0) + '</span></button>'
    : '';

  const replyBtn = getStoredAuth()
    ? '<button type="button" class="comment-toggle comment-reply-btn" data-action="comment-reply" data-author="' + esc(comment.author) + '" data-permlink="' + esc(comment.permlink) + '">Reply</button>'
    : '';

  const permalink = '<a class="comment-toggle comment-permalink" href="/@' + encodeURIComponent(comment.author) + '/' + encodeURIComponent(comment.permlink) + '" title="Direct link">\u{1F517}</a>';

  return '<div class="comment">' +
    '<div class="comment-head">' +
      '<a class="comment-author" href="https://peakd.com/@' + encodeURIComponent(comment.author) + '" target="_blank" rel="noopener noreferrer">@' + esc(comment.author) + '</a>' +
      (rep ? '<span class="comment-rep">' + esc(rep) + '</span>' : '') +
      voteBtn +
      replyBtn +
      permalink +
      '<span class="comment-time">' + esc(relativeTime(comment.created)) + '</span>' +
    '</div>' +
    '<div class="comment-body rendered-body">' + bodyHtml + '</div>' +
    childrenHtml +
  '</div>';
}

function toggleCommentChildren(btn) {
  const id = btn.dataset.toggleId;
  const el = document.getElementById(id);
  if (!el) return;
  const hidden = el.style.display === 'none';
  el.style.display = hidden ? '' : 'none';
  const count = el.querySelectorAll(':scope > .comment').length;
  btn.innerHTML = hidden
    ? count + ' replies &#9650;'
    : count + ' replies &#9660;';
}

let _commentCooldown = false;

// Build the full comment tree HTML (top-level form + all comments).
// Used by x-html binding in the template.
function renderCommentsHtml() {
  const s = Alpine.store('app');
  if (s.commentLoading) {
    let html = '<div class="comment-skeleton">';
    for (let i = 0; i < 3; i++) {
      html += '<div class="comment-skeleton-item">' +
        '<div class="comment-skeleton-avatar skeleton"></div>' +
        '<div class="comment-skeleton-lines">' +
        '<div class="comment-skeleton-line skeleton"></div>' +
        '<div class="comment-skeleton-line skeleton"></div>' +
        '<div class="comment-skeleton-line skeleton"></div>' +
        '</div></div>';
    }
    html += '</div>';
    return html;
  }
  if (s.commentError) {
    return '<div style="color:var(--text-dim);font-size:13px;padding:8px">Could not load comments.</div>';
  }
  const topForm = renderCommentForm(s.commentPostAuthor, s.commentPostPermlink, true);
  if (s.comments.length === 0) {
    return topForm + '<div style="color:var(--text-dim);font-size:13px;padding:8px">No comments yet.</div>';
  }
  let html = topForm;
  s.comments.forEach(c => { html += renderComment(c, 0); });
  return html;
}

function renderCommentForm(parentAuthor, parentPermlink, isTopLevel) {
  const auth = getStoredAuth();
  if (!auth) {
    return '<div class="comment-form-login">Log in with Hive Keychain to comment.</div>';
  }
  const formId = isTopLevel ? 'comment-form-top' : 'comment-form-reply';
  return '<div class="comment-form" id="' + formId + '">' +
    '<textarea class="comment-textarea" id="' + formId + '-textarea" placeholder="Write a comment..." maxlength="64000" rows="3"></textarea>' +
    '<div class="comment-form-actions">' +
      '<button type="button" class="comment-preview-btn" data-action="comment-preview" data-form-id="' + formId + '">Preview</button>' +
      '<div style="flex:1"></div>' +
      (!isTopLevel ? '<button type="button" class="btn btn-ghost comment-cancel-btn" data-action="close-reply-form">Cancel</button>' : '') +
      '<button type="button" class="btn comment-submit-btn" id="' + formId + '-submit" data-action="submit-comment" data-parent-author="' + esc(parentAuthor) + '" data-parent-permlink="' + esc(parentPermlink) + '" data-form-id="' + formId + '">Post Comment</button>' +
    '</div>' +
    '<div class="comment-preview rendered-body" id="' + formId + '-preview" style="display:none"></div>' +
  '</div>';
}

function toggleCommentPreview(formId) {
  const textarea = document.getElementById(formId + '-textarea');
  const preview = document.getElementById(formId + '-preview');
  if (!textarea || !preview) return;
  if (preview.style.display === 'none') {
    preview.innerHTML = renderHiveBody(textarea.value || '');
    preview.style.display = '';
  } else {
    preview.style.display = 'none';
  }
}

let _activeReplyFormParent = null;

function openReplyForm(author, permlink, btn) {
  closeReplyForm();
  const comment = btn.closest('.comment');
  if (!comment) return;
  const formHtml = renderCommentForm(author, permlink, false);
  const formWrapper = document.createElement('div');
  formWrapper.className = 'comment-reply-wrapper';
  formWrapper.innerHTML = formHtml;
  // Insert after comment-body, before children
  const body = comment.querySelector('.comment-body');
  body.insertAdjacentElement('afterend', formWrapper);
  _activeReplyFormParent = formWrapper;
  const textarea = formWrapper.querySelector('textarea');
  if (textarea) textarea.focus();
}

function closeReplyForm() {
  if (_activeReplyFormParent) {
    _activeReplyFormParent.remove();
    _activeReplyFormParent = null;
  }
}

async function submitComment(parentAuthor, parentPermlink, formId) {
  if (_commentCooldown) {
    showToast('Please wait a moment before posting again', 'info');
    return;
  }
  const textarea = document.getElementById(formId + '-textarea');
  const submitBtn = document.getElementById(formId + '-submit');
  if (!textarea || !submitBtn) return;
  const body = textarea.value.trim();
  if (!body) {
    showToast('Comment cannot be empty', 'error');
    return;
  }
  submitBtn.disabled = true;
  submitBtn.textContent = 'Broadcasting...';
  try {
    const s = Alpine.store('app');
    await broadcastComment(parentAuthor, parentPermlink, body);
    showToast('Comment posted!', 'success');
    textarea.value = '';
    closeReplyForm();
    // Cooldown
    _commentCooldown = true;
    setTimeout(() => { _commentCooldown = false; }, 3000);
    // Re-fetch after delay (blockchain confirmation takes ~3s)
    setTimeout(() => fetchComments(s.commentPostAuthor, s.commentPostPermlink), 4000);
  } catch(e) {
    showToast(e.message || 'Could not post comment', 'error');
  }
  submitBtn.disabled = false;
  submitBtn.textContent = 'Post Comment';
}

async function handleCommentVote(author, permlink, btn) {
  const auth = getStoredAuth();
  if (!auth) { showLoginPrompt(); return; }
  const key = `${author}/${permlink}`;
  const countEl = btn.querySelector('.comment-vote-count');

  if (state.votedPosts[key]) {
    if (!confirm('Remove your vote from this comment?')) return;
    btn.disabled = true;
    try {
      await broadcastVote(author, permlink, 0);
      removeVotedPost(key);
      btn.classList.remove('voted');
      if (countEl) countEl.textContent = Math.max(0, (parseInt(countEl.textContent) || 0) - 1);
      state.manaCache = null;
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
    // After manual vote popup completes, update comment button visuals
    const pollId = setInterval(() => {
      if (btn.disabled) return; // still in popup
      clearInterval(pollId);
      if (state.votedPosts[key]) {
        btn.classList.add('voted');
        if (countEl) countEl.textContent = (parseInt(countEl.textContent) || 0) + 1;
      }
    }, 200);
    setTimeout(() => clearInterval(pollId), 15000);
    return;
  }

  btn.disabled = true;
  try {
    const manaPercent = await fetchManaPercent();
    const weight = calculateVoteWeight(manaPercent, prefs.floor, prefs.maxWeight);
    await broadcastVote(author, permlink, weight);
    saveVotedPost(key);
    btn.classList.add('voted');
    if (countEl) countEl.textContent = (parseInt(countEl.textContent) || 0) + 1;
    state.manaCache = null;
    showToast('Voted!', 'success');
  } catch(e) {
    showToast(e.message || 'Vote failed', 'error');
  }
  btn.disabled = false;
}

async function fetchComments(author, permlink) {
  const s = Alpine.store('app');
  s.commentPostAuthor = author;
  s.commentPostPermlink = permlink;
  s.commentLoading = true;
  s.commentError = false;
  s.comments = [];
  s.commentCount = 0;
  s.hiddenCount = 0;

  try {
    const discussion = await hiveRpc('bridge.get_discussion', {author, permlink});
    if (!discussion) {
      s.commentError = true;
      s.commentLoading = false;
      return;
    }

    const rootKey = `${author}/${permlink}`;
    const rootEntry = discussion[rootKey];
    if (!rootEntry || !rootEntry.replies || rootEntry.replies.length === 0) {
      s.commentLoading = false;
      return;
    }

    let hiddenCount = 0;
    function buildTree(key) {
      const entry = discussion[key];
      if (!entry) return null;
      if (entry.author_reputation != null && entry.author_reputation <= 0) {
        hiddenCount++;
        return null;
      }
      const auth = getStoredAuth();
      const votedByMe = auth && Array.isArray(entry.active_votes)
        ? entry.active_votes.some(v => v.voter === auth.username)
        : false;
      return {
        author: entry.author,
        permlink: entry.permlink,
        body: entry.body || '',
        reputation: entry.author_reputation || 0,
        created: entry.created,
        net_votes: (entry.stats && entry.stats.total_votes) || 0,
        voted: votedByMe,
        children: (entry.replies || []).map(buildTree).filter(Boolean)
      };
    }

    const comments = (rootEntry.replies || []).map(buildTree).filter(Boolean);

    let totalVisible = 0;
    function countAll(arr) { arr.forEach(c => { totalVisible++; if (c.children) countAll(c.children); }); }
    countAll(comments);

    s.comments = comments;
    s.commentCount = totalVisible;
    s.hiddenCount = hiddenCount;
    s.commentLoading = false;
  } catch(e) {
    s.commentError = true;
    s.commentLoading = false;
  }
}
