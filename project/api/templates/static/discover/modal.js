// ── Modal ──
async function openModal(post, skipPush) {
  markRead(`${post.author}/${post.permlink}`);
  if (!skipPush) {
    history.pushState({ author: post.author, permlink: post.permlink },
                      '', `/@${post.author}/${post.permlink}`);
  }
  document.getElementById('modal-title').textContent = 'Loading...';
  document.getElementById('modal-author').innerHTML = `<img class="author-avatar" src="https://images.hive.blog/u/${encodeURIComponent(post.author)}/avatar/small" alt="" width="28" height="28"><a class="clickable-author" href="/@${esc(post.author)}">@${esc(post.author)}</a>`;
  const commEl = document.getElementById('modal-community');
  if (post.community_name && post.community_id) {
    commEl.textContent = post.community_name;
    commEl.href = `https://peakd.com/c/${encodeURIComponent(post.community_id)}`;
    commEl.style.display = '';
  } else {
    commEl.style.display = 'none';
  }
  const dateEl = document.getElementById('modal-date');
  dateEl.textContent = post.created ? new Date(post.created).toLocaleDateString('en', {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
  document.getElementById('modal-body').innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-dim)"><div class="spinner" style="width:28px;height:28px;border:3px solid var(--card);border-top-color:var(--hive-red);border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 8px"></div>Loading content...</div>';

  const tagsEl = document.getElementById('modal-tags');
  tagsEl.innerHTML = '';
  document.getElementById('modal-post-tags').innerHTML = '';
  (post.categories||[]).forEach(c => {
    const t = document.createElement('span');
    t.className = 'tag'; t.textContent = c; tagsEl.appendChild(t);
  });
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
  Alpine.store('app').modalOpen = true;
  trapFocus(modalEl.querySelector('.modal'));

  // Fetch comments in parallel with post body
  fetchComments(post.author, post.permlink);

  const result = await hiveRpc('bridge.get_post', {author: post.author, permlink: post.permlink});
  if (result) {
    document.getElementById('modal-title').textContent = result.title || post.permlink;
    document.getElementById('modal-body').innerHTML = renderHiveBody(result.body || '');
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
    // Cross-post detection: show banner and re-route votes to original
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
          + ` · <a href="/@${esc(cpAuthor)}/${esc(cpPermlink)}" onclick="event.preventDefault();closeModal();openModal({author:'${esc(cpAuthor)}',permlink:'${esc(cpPermlink)}'})">View original</a>`;
        banner.style.display = '';
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
}

function closeModal(skipPush) {
  const modalEl = document.getElementById('modal');
  releaseFocus(modalEl.querySelector('.modal'));
  Alpine.store('app').modalOpen = false;
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
