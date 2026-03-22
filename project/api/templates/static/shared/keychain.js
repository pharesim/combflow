// ── Hive Keychain Broadcasting ──
// Depends on: getStoredAuth(), isKeychainInstalled() from shared.js

function keychainBroadcast(ops, keyType) {
  return new Promise((resolve, reject) => {
    const auth = getStoredAuth();
    if (!auth) return reject(new Error('Not logged in'));
    if (!isKeychainInstalled()) return reject(new Error('Hive Keychain not installed'));
    window.hive_keychain.requestBroadcast(auth.username, ops, keyType || 'Posting', (response) => {
      if (response.success) resolve(response);
      else reject(new Error(response.message || 'Broadcast failed'));
    });
  });
}

function generatePermlink(parentAuthor, parentPermlink) {
  const trunc = parentPermlink.slice(0, 200);
  const ts = new Date().toISOString().replace(/[-:T.Z]/g, '').slice(0, 14).toLowerCase();
  return `re-${parentAuthor}-${trunc}-${ts}z`;
}

function slugify(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 200);
}

function broadcastComment(parentAuthor, parentPermlink, body) {
  const auth = getStoredAuth();
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
  return keychainBroadcast([op]).then(() => ({ author: auth.username, permlink }));
}

function broadcastPost(title, body, tags, communityId, description) {
  const auth = getStoredAuth();
  const timestamp = Date.now().toString(16).slice(-8);
  const permlink = slugify(title) + '-' + timestamp;
  const parentPermlink = communityId || (tags.length > 0 ? tags[0] : 'hive');

  const imgMatch = body.match(/!\[[^\]]*\]\(([^)]+)\)/) || body.match(/https?:\/\/\S+\.(?:jpg|jpeg|png|gif|webp)/i);
  const images = imgMatch ? [imgMatch[1] || imgMatch[0]] : [];

  const metadata = {
    tags: tags,
    image: images,
    app: 'honeycomb/1.0',
  };
  if (description) metadata.description = description;
  if (communityId) metadata.community = communityId;

  const ops = [
    ['comment', {
      parent_author: '',
      parent_permlink: parentPermlink,
      author: auth.username,
      permlink: permlink,
      title: title,
      body: body,
      json_metadata: JSON.stringify(metadata),
    }],
    ['comment_options', {
      author: auth.username,
      permlink: permlink,
      max_accepted_payout: '1000000.000 HBD',
      percent_hbd: 0,
      allow_votes: true,
      allow_curation_rewards: true,
      extensions: [],
    }],
  ];

  return keychainBroadcast(ops).then(() => ({ author: auth.username, permlink }));
}

function broadcastCrossPost(author, permlink) {
  const op = ['custom_json', {
    required_auths: [],
    required_posting_auths: [getStoredAuth().username],
    id: 'follow',
    json: JSON.stringify(['reblog', {
      account: getStoredAuth().username,
      author: author,
      permlink: permlink,
    }]),
  }];
  return keychainBroadcast([op]).then(() => {});
}

function subscribeCommunity(communityId) {
  const op = ['custom_json', {
    required_auths: [],
    required_posting_auths: [getStoredAuth().username],
    id: 'community',
    json: JSON.stringify(['subscribe', { community: communityId }]),
  }];
  return keychainBroadcast([op]).then(() => {});
}

function unsubscribeCommunity(communityId) {
  const op = ['custom_json', {
    required_auths: [],
    required_posting_auths: [getStoredAuth().username],
    id: 'community',
    json: JSON.stringify(['unsubscribe', { community: communityId }]),
  }];
  return keychainBroadcast([op]).then(() => {});
}

function broadcastVote(author, permlink, weight) {
  const op = ['vote', {
    voter: getStoredAuth().username,
    author: author,
    permlink: permlink,
    weight: weight,
  }];
  return keychainBroadcast([op]).then(() => {});
}

// Voting mana calculation (client-side)
function computeCurrentMana(manabar, maxMana) {
  const now = Math.floor(Date.now() / 1000);
  const elapsed = now - manabar.last_update_time;
  const regen = elapsed * maxMana / 432000;
  return Math.min(Number(manabar.current_mana) + regen, maxMana);
}

function manaToPercent(currentMana, maxMana) {
  if (!maxMana || maxMana <= 0) return 100;
  return (currentMana / maxMana) * 100;
}

function calculateVoteWeight(manaPercent, floor, maxWeight) {
  if (manaPercent <= floor) return 100;
  const ratio = (manaPercent - floor) / (100 - floor);
  const weight = (1 - Math.pow(1 - ratio, 1.2)) * maxWeight;
  return Math.max(100, Math.round(weight * 100));
}

function broadcastFollow(targetUser) {
  const op = ['custom_json', {
    required_auths: [],
    required_posting_auths: [getStoredAuth().username],
    id: 'follow',
    json: JSON.stringify(['follow', {
      follower: getStoredAuth().username,
      following: targetUser,
      what: ['blog'],
    }]),
  }];
  return keychainBroadcast([op]).then(() => {});
}

function broadcastUnfollow(targetUser) {
  const op = ['custom_json', {
    required_auths: [],
    required_posting_auths: [getStoredAuth().username],
    id: 'follow',
    json: JSON.stringify(['follow', {
      follower: getStoredAuth().username,
      following: targetUser,
      what: [],
    }]),
  }];
  return keychainBroadcast([op]).then(() => {});
}

function broadcastMute(targetUser) {
  const op = ['custom_json', {
    required_auths: [],
    required_posting_auths: [getStoredAuth().username],
    id: 'follow',
    json: JSON.stringify(['follow', {
      follower: getStoredAuth().username,
      following: targetUser,
      what: ['ignore'],
    }]),
  }];
  return keychainBroadcast([op]).then(() => {});
}

function broadcastUnmute(targetUser) {
  const op = ['custom_json', {
    required_auths: [],
    required_posting_auths: [getStoredAuth().username],
    id: 'follow',
    json: JSON.stringify(['follow', {
      follower: getStoredAuth().username,
      following: targetUser,
      what: [],
    }]),
  }];
  return keychainBroadcast([op]).then(() => {});
}
