// ── Notifications ──

let _notifTimer = null;

function relativeTime(dateStr) {
  const now = Date.now();
  const then = new Date(dateStr + 'Z').getTime();
  const diff = Math.max(0, now - then);
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'now';
  if (mins < 60) return mins + 'm ago';
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + 'h ago';
  const days = Math.floor(hrs / 24);
  if (days < 30) return days + 'd ago';
  const months = Math.floor(days / 30);
  return months + 'mo ago';
}

function notifTypeIcon(type) {
  switch (type) {
    case 'vote': return '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>';
    case 'reply': case 'reply_comment': return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
    case 'follow': return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/></svg>';
    case 'mention': return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-3.92 7.94"/></svg>';
    case 'reblog': return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>';
    default: return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
  }
}

async function fetchUnreadCount() {
  const user = Alpine.store('app').currentUser;
  if (!user) return;
  const result = await hiveRpc('bridge.unread_notifications', { account: user });
  if (result) {
    Alpine.store('app').unreadCount = result.unread || 0;
    Alpine.store('app').lastRead = result.lastread || null;
  }
}

async function fetchNotifications() {
  const s = Alpine.store('app');
  if (!s.currentUser) return;
  const result = await hiveRpc('bridge.account_notifications', { account: s.currentUser, limit: 50 });
  if (!result) return;
  s.notifications = result;
  // Also refresh unread count
  const unread = await hiveRpc('bridge.unread_notifications', { account: s.currentUser });
  if (unread) {
    s.unreadCount = unread.unread || 0;
    s.lastRead = unread.lastread || null;
  }
  renderNotifications(result, s.lastRead);
}

function renderNotifications(items, lastRead) {
  const list = document.getElementById('notif-list');
  if (!list) return;
  // Clear existing items (preserve Alpine templates if any)
  list.querySelectorAll(':scope > *').forEach(el => el.remove());
  if (!items || items.length === 0) return;
  const lastReadTime = lastRead ? new Date(lastRead + 'Z').getTime() : 0;
  const frag = document.createDocumentFragment();
  items.forEach(n => {
    const itemTime = new Date(n.date + 'Z').getTime();
    const isUnread = itemTime > lastReadTime;
    const row = document.createElement('a');
    row.className = 'notif-item' + (isUnread ? ' unread' : '');
    row.href = n.url ? '/@' + n.url : '#';
    row.innerHTML =
      '<span class="notif-icon">' + notifTypeIcon(n.type) + '</span>' +
      '<span class="notif-body"><span class="notif-msg">' + esc(n.msg) + '</span>' +
      '<span class="notif-time">' + relativeTime(n.date) + '</span></span>';
    frag.appendChild(row);
  });
  list.appendChild(frag);
}

async function markAllRead() {
  const user = Alpine.store('app').currentUser;
  if (!user) return;
  const now = new Date().toISOString().slice(0, 19);
  try {
    await keychainBroadcast([['custom_json', {
      required_auths: [],
      required_posting_auths: [user],
      id: 'notify',
      json: JSON.stringify(['setLastRead', { date: now }]),
    }]]);
    Alpine.store('app').unreadCount = 0;
    Alpine.store('app').lastRead = now;
    // Remove unread highlights
    document.querySelectorAll('.notif-item.unread').forEach(el => el.classList.remove('unread'));
    showToast('Notifications marked as read', 'success');
  } catch (e) {
    showToast(e.message || 'Failed to mark as read', 'error');
  }
}

function toggleNotifications() {
  const s = Alpine.store('app');
  s.notifOpen = !s.notifOpen;
  if (s.notifOpen) fetchNotifications();
}

function startNotifPolling() {
  stopNotifPolling();
  fetchUnreadCount();
  _notifTimer = setInterval(() => {
    if (!document.hidden && Alpine.store('app').currentUser) fetchUnreadCount();
  }, 60000);
}

function stopNotifPolling() {
  if (_notifTimer) { clearInterval(_notifTimer); _notifTimer = null; }
}
