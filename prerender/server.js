const prerender = require('prerender');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const CACHE_DIR = process.env.CACHE_ROOT_DIR || '/cache';

function isPostUrl(url) {
  try {
    const p = new URL(url).pathname;
    return /^\/@[^/]+\/.+/.test(p);
  } catch { return false; }
}

function cachePath(url) {
  const hash = crypto.createHash('sha1').update(url).digest('hex');
  const sub = isPostUrl(url) ? 'posts' : 'pages';
  return path.join(CACHE_DIR, sub, hash.slice(0, 2), hash + '.html');
}

const server = prerender({
  chromeLocation: process.env.CHROME_BIN || '/usr/bin/chromium',
  extraChromeFlags: [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-extensions',
    '--disable-background-networking',
    '--disable-default-apps',
    '--disable-sync',
    '--disable-translate',
    '--mute-audio',
    '--no-first-run',
    '--safebrowsing-disable-auto-update'
  ],
  pageLoadTimeout: 20000,
  waitAfterLastRequest: 1000
});

server.use({
  requestReceived: function(req, res, next) {
    if (req.method !== 'GET') return next();
    const file = cachePath(req.prerender.url);
    fs.readFile(file, 'utf8', function(err, data) {
      if (!err && data) {
        console.info('cache hit for: ' + req.prerender.url);
        res.send(200, data);
      } else {
        next();
      }
    });
  },
  beforeSend: function(req, res, next) {
    if (req.prerender.statusCode === 200 && req.prerender.content) {
      const file = cachePath(req.prerender.url);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const tagged = '<!-- ' + req.prerender.url + ' -->\n' + req.prerender.content;
      fs.writeFile(file, tagged, function(err) {
        if (err) console.error('cache write error:', err.message);
      });
    }
    next();
  }
});

server.start();
