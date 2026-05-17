// ── Modal ──
async function openModal(post, skipPush) {
  markRead(`${post.author}/${post.permlink}`);
  if (!skipPush) {
    history.pushState({ author: post.author, permlink: post.permlink },
                      '', `/@${post.author}/${post.permlink}`);
  }
  document.getElementById('modal-title').textContent = 'Loading...';
  document.getElementById('modal-author').innerHTML = `${avatarHtml(post.author, 28)}<a class="clickable-author" href="/@${esc(post.author)}">@${esc(post.author)}</a>`;
  const commEl = document.getElementById('modal-community');
  if (post.community_name && post.community_id) {
    commEl.textContent = post.community_name;
    commEl.onclick = () => { filterByCommunity(post.community_id); closeModal(); };
    commEl.style.display = '';
  } else {
    commEl.onclick = null;
    commEl.style.display = 'none';
  }
  const dateEl = document.getElementById('modal-date');
  dateEl.textContent = post.created ? new Date(post.created).toLocaleDateString('en', {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
  document.getElementById('modal-body').innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-dim)"><div class="spinner" style="width:28px;height:28px;border:3px solid var(--card);border-top-color:var(--hive-red);border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 8px"></div>Loading content...</div>';

  const tagsEl = document.getElementById('modal-tags');
  // Preserve report button before clearing tags (innerHTML would destroy it)
  const reportBtn = tagsEl.querySelector('.report-btn-sm') || document.querySelector('.report-btn-sm');
  if (reportBtn) reportBtn.remove();
  tagsEl.innerHTML = '';
  document.getElementById('modal-post-tags').innerHTML = '';
  (post.categories||[]).forEach(c => {
    const t = document.createElement('span');
    t.className = 'tag'; t.textContent = c; tagsEl.appendChild(t);
  });
  // Insert report button between categories and sentiment (visible only when logged in)
  if (reportBtn) {
    reportBtn.style.display = Alpine.store('app').currentUser ? '' : 'none';
    tagsEl.appendChild(reportBtn);
  }
  if (post.sentiment && safeSentiment(post.sentiment)) {
    const s = document.createElement('span');
    s.className = `tag sentiment-${safeSentiment(post.sentiment)}`;
    s.textContent = `${post.sentiment} (${(post.sentiment_score||0).toFixed(2)})`;
    tagsEl.appendChild(s);
  }
  (post.languages||[]).forEach(lang => {
    const l = document.createElement('span');
    l.className = 'tag'; l.textContent = lang.toUpperCase(); tagsEl.appendChild(l);
  });

  document.getElementById('modal-score').textContent = '';
  document.getElementById('modal-vote-count').textContent = '';
  document.getElementById('modal-hivelink').href = `https://hivel.ink/@${post.author}/${post.permlink}`;
  document.getElementById('cross-post-banner').style.display = 'none';

  // Vote button in modal — may be re-targeted to original post below
  let voteAuthor = post.author, votePermlink = post.permlink;
  const voteKey = `${post.author}/${post.permlink}`;
  const modalVoteBtn = document.getElementById('modal-vote-btn');
  modalVoteBtn.className = 'vote-btn modal-vote-btn' + (state.votedPosts[voteKey] ? ' voted' : '');
  modalVoteBtn.setAttribute('data-vote-key', voteKey);
  modalVoteBtn.setAttribute('aria-label', state.votedPosts[voteKey] ? 'Voted' : 'Vote');
  modalVoteBtn.onclick = () => handleVote(voteAuthor, votePermlink, modalVoteBtn);

  // Follow/Mute buttons in modal
  const auth = getStoredAuth();
  const followBtn = document.getElementById('modal-follow-btn');
  const muteBtn = document.getElementById('modal-mute-btn');
  if (auth && auth.username !== post.author) {
    if (state.followedUsers.has(post.author)) {
      followBtn.textContent = `Unfollow @${post.author}`;
      followBtn.onclick = () => handleUnfollowUser(post.author);
    } else {
      followBtn.textContent = `Follow @${post.author}`;
      followBtn.onclick = () => handleFollowUser(post.author);
    }
    followBtn.style.display = '';
    muteBtn.textContent = `Mute @${post.author}`;
    muteBtn.onclick = () => handleMuteUser(post.author);
    muteBtn.style.display = '';
  } else {
    followBtn.style.display = 'none';
    muteBtn.style.display = 'none';
  }

  const modalEl = document.getElementById('modal');
  Alpine.store('app').modalPost = { author: post.author, permlink: post.permlink };
  Alpine.store('app').modalOpen = true;
  trapFocus(modalEl.querySelector('.modal'));

  // Pause background meta fetches so modal RPC gets full bandwidth
  state.metaPaused = true;

  // Fetch comments in parallel with post body
  fetchComments(post.author, post.permlink);

  // Reuse cached metadata if fetchSingleMeta already fetched body
  const cacheKey = `${post.author}/${post.permlink}`;
  const cached = state.metaCache[cacheKey];
  let result;
  if (cached && cached.body) {
    // Build a result-like object from cached data
    result = {
      title: cached.title,
      body: cached.body,
      json_metadata: cached.json_metadata,
      stats: { total_votes: cached.votes },
      children: cached.children,
      pending_payout_value: cached.payout != null ? cached.payout + ' HBD' : null,
      cross_post_key: null,
    };
    // Parse cross_post_key from json_metadata
    try {
      const meta = typeof cached.json_metadata === 'string' ? JSON.parse(cached.json_metadata) : cached.json_metadata || {};
      if (meta.cross_post_key) result.cross_post_key = meta.cross_post_key;
      else if (meta.original_author && meta.original_permlink) result.cross_post_key = meta.original_author + '/' + meta.original_permlink;
    } catch(e) {}
  } else {
    result = await hiveRpc('bridge.get_post', {author: post.author, permlink: post.permlink});
    normalizeCrossPostKey(result);
  }
  // Resume background meta fetches now that modal content is loaded
  resumeMeta();
  if (result) {
    document.getElementById('modal-title').textContent = result.title || post.permlink;
    document.getElementById('modal-body').innerHTML = renderHiveBody(result.body || '');
    // Fill date from Hive result if not already set (e.g. comment deep links)
    if (result.created) {
      const dateEl = document.getElementById('modal-date');
      if (!dateEl.textContent) {
        dateEl.textContent = new Date(result.created + 'Z').toLocaleDateString('en', {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
      }
    }
    // Comment navigation — show parent/root links when viewing a comment
    if (result.parent_author) {
      // Traverse up to find the root post
      let rootAuthor = null, rootPermlink = null;
      let cur = result;
      while (cur && cur.parent_author) {
        const parent = await hiveRpc('bridge.get_post', {author: cur.parent_author, permlink: cur.parent_permlink});
        if (!parent) break;
        rootAuthor = parent.author;
        rootPermlink = parent.permlink;
        cur = parent;
      }
      let navHtml = '<div class="comment-nav">';
      const isDirectReply = !rootAuthor || (result.parent_author === rootAuthor && result.parent_permlink === rootPermlink);
      if (!isDirectReply) {
        navHtml += '<a href="/@' + encodeURIComponent(result.parent_author) + '/' + encodeURIComponent(result.parent_permlink) + '" data-action="navigate-post" data-author="' + esc(result.parent_author) + '" data-permlink="' + esc(result.parent_permlink) + '">Parent comment</a>';
      }
      if (rootAuthor && rootPermlink) {
        navHtml += '<a href="/@' + encodeURIComponent(rootAuthor) + '/' + encodeURIComponent(rootPermlink) + '" data-action="navigate-post" data-author="' + esc(rootAuthor) + '" data-permlink="' + esc(rootPermlink) + '">Original post</a>';
      }
      navHtml += '</div>';
      if (navHtml !== '<div class="comment-nav"></div>') {
        document.getElementById('modal-body').insertAdjacentHTML('afterbegin', navHtml);
      }
    }
    // Vote count in modal
    const voteCount = (result.stats && result.stats.total_votes) || (result.active_votes || []).length;
    document.getElementById('modal-vote-count').textContent = voteCount;
    cacheMetaEntry(`${post.author}/${post.permlink}`, {
      ...state.metaCache[`${post.author}/${post.permlink}`],
      votes: voteCount,
      children: result.children || 0,
    });
    Alpine.store('app').metaRev++;
    // Show post tags
    const postTagsEl = document.getElementById('modal-post-tags');
    postTagsEl.innerHTML = '';
    let postTags = [];
    try { postTags = (typeof result.json_metadata === 'string' ? JSON.parse(result.json_metadata) : result.json_metadata)?.tags || []; } catch(e) {}
    postTags.forEach(tag => {
      const t = document.createElement('span');
      t.className = 'tag';
      t.textContent = '#' + tag;
      postTagsEl.appendChild(t);
    });
    // Show mini map if post has worldmappin location
    const wmMatch = (result.body || '').match(/\[\/\/\]:#\s*\(!worldmappin\s+([\d.-]+)\s+lat\s+([\d.-]+)\s+long\s*(.*?)\s*d3scr\)/);
    if (wmMatch) {
      const lat = parseFloat(wmMatch[1]), lng = parseFloat(wmMatch[2]), desc = wmMatch[3].trim() || 'Location';
      const mapLink = document.createElement('a');
      mapLink.href = 'https://worldmappin.com/p/' + post.permlink;
      mapLink.target = '_blank';
      mapLink.rel = 'noopener';
      mapLink.style.cssText = 'display:block;cursor:pointer';
      const mapDiv = document.createElement('div');
      mapDiv.id = 'modal-minimap';
      mapDiv.style.cssText = 'height:200px;border-radius:8px;margin:12px 0;z-index:0';
      mapLink.appendChild(mapDiv);
      document.getElementById('modal-body').appendChild(mapLink);
      _loadLeaflet().then(() => {
        const map = L.map('modal-minimap', { scrollWheelZoom: false, dragging: false, zoomControl: false, attributionControl: false }).setView([lat, lng], 12);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18 }).addTo(map);
        L.marker([lat, lng]).addTo(map).bindPopup(desc).openPopup();
      });
    }
    // Cross-post detection: show banner, fetch original content, re-route votes
    if (result.cross_post_key) {
      const cpParts = result.cross_post_key.split('/');
      if (cpParts.length === 2 && cpParts[0] && cpParts[1]) {
        const [cpAuthor, cpPermlink] = cpParts;
        voteAuthor = cpAuthor;
        votePermlink = cpPermlink;
        modalVoteBtn.onclick = () => handleVote(cpAuthor, cpPermlink, modalVoteBtn);
        const banner = document.getElementById('cross-post-banner');
        const communityName = result.community_title || result.category || '';
        banner.innerHTML = `Cross-posted by <a href="/@${esc(post.author)}">@${esc(post.author)}</a>`
          + (communityName ? ` in ${esc(communityName)}` : '')
          + ` · <a href="/@${esc(cpAuthor)}/${esc(cpPermlink)}" data-action="navigate-post" data-author="${esc(cpAuthor)}" data-permlink="${esc(cpPermlink)}">View original</a>`;
        banner.style.display = '';
        // Fetch and render the original post content
        try {
          const original = await hiveRpc('bridge.get_post', {author: cpAuthor, permlink: cpPermlink});
          if (original) {
            document.getElementById('modal-title').textContent = original.title || cpPermlink;
            document.getElementById('modal-body').innerHTML = renderHiveBody(original.body || '');
          }
        } catch(e) {}
      }
    }
    // Check if user already voted
    if (auth && checkExistingVote(result, auth.username) && !state.votedPosts[voteKey]) {
      saveVotedPost(voteKey);
      updateVoteButtons(voteKey, true);
    }
  }
  if (!result) {
    document.getElementById('modal-title').textContent = post.permlink;
    document.getElementById('modal-body').textContent = '(Could not load post content)';
  }
  window.prerenderReady = true;
}

function closeModal(skipPush) {
  resumeMeta();
  const modalEl = document.getElementById('modal');
  releaseFocus(modalEl.querySelector('.modal'));
  Alpine.store('app').modalOpen = false;
  Alpine.store('app').modalPost = null;
  Alpine.store('app').reportOpen = false;
  if (!skipPush && !state.deepLinked && window.location.pathname !== '/') {
    history.pushState(null, '', '/');
  }
}

// ── Copy post link ──
function copyPostLink() {
  const url = window.location.href;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(() => {
      showToast('Link copied', 'success');
    }).catch(() => _fallbackCopy(url));
  } else {
    _fallbackCopy(url);
  }
}
function _fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand('copy');
    showToast('Link copied', 'success');
  } catch { showToast('Could not copy link', 'error'); }
  document.body.removeChild(ta);
}

// ── Anchor links inside modal ──
document.getElementById('modal-body').addEventListener('click', function(e) {
  const a = e.target.closest('a[href^="#"]');
  if (!a) return;
  e.preventDefault();
  e.stopPropagation();
  const target = document.getElementById(a.getAttribute('href').slice(1));
  if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
});

// ── Image lightbox ──
const _lb = { images: [], index: 0, touchX: 0 };

function _lbCollect(clickedImg) {
  const container = clickedImg.closest('.rendered-body');
  if (!container) return [clickedImg.src];
  return Array.from(container.querySelectorAll('img')).filter(i => !i.closest('a')).map(i => i.src);
}

function _lbShow() {
  document.getElementById('lightbox-img').src = _lb.images[_lb.index];
  const n = _lb.images.length;
  document.getElementById('lightbox-prev')[n > 1 ? 'removeAttribute' : 'setAttribute']('hidden', '');
  document.getElementById('lightbox-next')[n > 1 ? 'removeAttribute' : 'setAttribute']('hidden', '');
  document.getElementById('lightbox-counter').textContent = n > 1 ? (_lb.index + 1) + ' / ' + n : '';
}

function openLightbox(clickedImg) {
  _lb.images = _lbCollect(clickedImg);
  _lb.index = Math.max(0, _lb.images.indexOf(clickedImg.src));
  const overlay = document.getElementById('lightbox');
  overlay.style.display = 'flex';
  _lbShow();
  // Force reflow so opacity transition fires
  overlay.offsetHeight;
  overlay.classList.add('active');
  trapFocus(overlay);
}

function closeLightbox() {
  const overlay = document.getElementById('lightbox');
  releaseFocus(overlay);
  overlay.classList.remove('active');
  setTimeout(() => { if (!overlay.classList.contains('active')) overlay.style.display = 'none'; }, 200);
}

function lightboxPrev() { if (_lb.images.length > 1) { _lb.index = (_lb.index - 1 + _lb.images.length) % _lb.images.length; _lbShow(); } }
function lightboxNext() { if (_lb.images.length > 1) { _lb.index = (_lb.index + 1) % _lb.images.length; _lbShow(); } }

document.getElementById('lightbox').addEventListener('click', function(e) {
  if (e.target.id === 'lightbox-img' || e.target.closest('.lightbox-nav')) return;
  closeLightbox();
});
document.getElementById('lightbox-prev').addEventListener('click', lightboxPrev);
document.getElementById('lightbox-next').addEventListener('click', lightboxNext);

// Arrow keys
document.addEventListener('keydown', function(e) {
  if (!document.getElementById('lightbox').classList.contains('active')) return;
  if (e.key === 'ArrowLeft') { e.preventDefault(); lightboxPrev(); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); lightboxNext(); }
});

// Touch swipe
document.getElementById('lightbox').addEventListener('touchstart', function(e) { _lb.touchX = e.changedTouches[0].clientX; }, { passive: true });
document.getElementById('lightbox').addEventListener('touchend', function(e) {
  const dx = e.changedTouches[0].clientX - _lb.touchX;
  if (Math.abs(dx) > 50) { dx < 0 ? lightboxNext() : lightboxPrev(); }
}, { passive: true });

// Delegate image clicks inside rendered bodies (post modal + comments)
document.addEventListener('click', function(e) {
  const img = e.target.closest('.rendered-body img');
  if (!img) return;
  if (img.closest('a')) return;
  e.preventDefault();
  openLightbox(img);
});

// ── Popstate (browser back/forward) ──
window.addEventListener('popstate', e => {
  if (e.state && e.state.author) {
    openModal({ author: e.state.author, permlink: e.state.permlink }, true);
  } else if (e.state && e.state.authorFilter) {
    filterByAuthor(e.state.authorFilter);
  } else {
    closeModal(true);
    if (state.authorFilterUser) { clearAuthorFilter(); scheduleFilter(); }
  }
});
