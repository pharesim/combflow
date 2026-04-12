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

// Rewrite public URL → internal Docker address to avoid round-tripping
// through Cloudflare. The rendered HTML still contains the public URL
// (set server-side via SITE_URL config).
const INTERNAL_URL = process.env.INTERNAL_RENDER_URL; // e.g. http://caddy:8080

const server = prerender({
  chromeLocation: process.env.CHROME_BIN || '/usr/bin/chromium',
  extraChromeFlags: [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--ignore-certificate-errors',
    '--disable-extensions',
    '--disable-background-networking',
    '--disable-default-apps',
    '--disable-sync',
    '--disable-translate',
    '--mute-audio',
    '--no-first-run',
    '--safebrowsing-disable-auto-update',
    '--disk-cache-dir=/tmp/chrome-cache'
  ],
  pageLoadTimeout: 20000,
  waitAfterLastRequest: 1000
});

if (INTERNAL_URL) {
  server.use({
    requestReceived: function(req, res, next) {
      req.prerender.originalUrl = req.prerender.url;
      try {
        const parsed = new URL(req.prerender.url);
        const internal = new URL(INTERNAL_URL);
        parsed.protocol = internal.protocol;
        parsed.host = internal.host;
        req.prerender.url = parsed.toString();
      } catch (e) {
        console.warn('URL rewrite failed for:', req.prerender.url, e.message);
      }
      next();
    }
  });
}

// Block images, CSS, fonts, and media — prerender only needs the rendered DOM
const BLOCKED_TYPES = new Set(['Stylesheet', 'Image', 'Font', 'Media']);
server.use({
  tabCreated: function(req, res, next) {
    const tab = req.prerender.tab;
    tab.Fetch.enable({
      patterns: Array.from(BLOCKED_TYPES, resourceType => ({
        resourceType, requestStage: 'Request'
      }))
    }).then(() => {
      tab.Fetch.requestPaused(({ requestId }) => {
        tab.Fetch.failRequest({ requestId, errorReason: 'Aborted' }).catch(() => {});
      });
    }).catch(err => {
      console.warn('request interception setup failed:', err.message);
    });
    next();
  }
});

server.use({
  requestReceived: function(req, res, next) {
    if (req.method !== 'GET') return next();
    const cacheUrl = req.prerender.originalUrl || req.prerender.url;
    const file = cachePath(cacheUrl);
    fs.readFile(file, 'utf8', function(err, data) {
      if (!err && data) {
        console.info('cache hit for: ' + cacheUrl);
        res.send(200, data);
      } else {
        next();
      }
    });
  },
  beforeSend: function(req, res, next) {
    if (req.prerender.statusCode === 200 && req.prerender.content) {
      const publicUrl = req.prerender.originalUrl || req.prerender.url;
      // Skip caching empty shell pages (no meaningful content rendered)
      if (/<body>\s*<\/body>/i.test(req.prerender.content)) {
        console.warn('skipping cache for empty page: ' + publicUrl);
        next();
        return;
      }
      const file = cachePath(publicUrl);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const tagged = '<!-- ' + publicUrl + ' -->\n' + req.prerender.content;
      fs.writeFile(file, tagged, function(err) {
        if (err) console.error('cache write error:', err.message);
      });
    }
    next();
  }
});

server.start();
