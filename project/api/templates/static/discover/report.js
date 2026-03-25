// ── Misclassification reporting ──

let _reportPost = null;
let _reportBusy = false;

function openReport() {
  const s = Alpine.store('app');
  if (!s.currentUser || !s.modalPost) return;
  _reportPost = s.modalPost;
  document.getElementById('report-reason').value = '';
  document.getElementById('report-char-count').textContent = '0 / 1000';
  document.getElementById('report-error').textContent = '';
  document.getElementById('report-submit-btn').disabled = false;
  _reportBusy = false;
  s.reportOpen = true;
}

function closeReport() {
  Alpine.store('app').reportOpen = false;
  _reportPost = null;
}

function updateReportCount() {
  const len = document.getElementById('report-reason').value.length;
  document.getElementById('report-char-count').textContent = len + ' / 1000';
}

async function submitReport() {
  if (_reportBusy || !_reportPost) return;
  const s = Alpine.store('app');
  const reason = document.getElementById('report-reason').value.trim();
  if (!reason) {
    document.getElementById('report-error').textContent = 'Please enter a reason.';
    return;
  }
  if (reason.length > 1000) {
    document.getElementById('report-error').textContent = 'Reason must be 1000 characters or less.';
    return;
  }
  if (!isKeychainInstalled()) {
    document.getElementById('report-error').textContent = 'Hive Keychain is required.';
    return;
  }

  _reportBusy = true;
  document.getElementById('report-submit-btn').disabled = true;
  document.getElementById('report-error').textContent = '';

  const author = _reportPost.author;
  const permlink = _reportPost.permlink;
  const username = s.currentUser;
  const message = `combflow_report_${author}/${permlink}_${reason}`;

  window.hive_keychain.requestSignBuffer(username, message, 'Posting', async (response) => {
    if (!response.success) {
      document.getElementById('report-error').textContent = response.message || 'Signing failed.';
      document.getElementById('report-submit-btn').disabled = false;
      _reportBusy = false;
      return;
    }

    try {
      const res = await fetch(`/api/posts/${encodeURIComponent(author)}/${encodeURIComponent(permlink)}/report`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username,
          reason,
          signature: response.result,
        }),
      });

      if (res.status === 201) {
        closeReport();
        showToast('Report submitted. Thank you!', 'success');
      } else if (res.status === 409) {
        document.getElementById('report-error').textContent = 'You have already reported this post.';
      } else {
        const data = await res.json().catch(() => ({}));
        document.getElementById('report-error').textContent = data.detail || 'Failed to submit report.';
      }
    } catch (e) {
      document.getElementById('report-error').textContent = 'Network error. Please try again.';
    }

    document.getElementById('report-submit-btn').disabled = false;
    _reportBusy = false;
  });
}
