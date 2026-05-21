// Real end-to-end prerender healthcheck.
//
// Fires an actual render request through the local prerender server and exits
// with status 1 if the response doesn't arrive within the timeout. This catches
// the failure mode where the HTTP server still answers but the Chromium worker
// pool is stuck — which exactly matches what we hit on prod (Googlebot timeouts
// while the prerender container's TCP port was still open).
//
// Used by docker-compose healthcheck. Combined with the autoheal sidecar,
// docker restarts the container automatically when this fails N times.
const http = require('http');

const TARGET = 'http://localhost:3000/http://caddy:8080/health';
const TIMEOUT_MS = 15000;

const req = http.get(TARGET, { timeout: TIMEOUT_MS }, (res) => {
  let body = '';
  res.on('data', (chunk) => { body += chunk; });
  res.on('end', () => {
    // /health returns JSON; success if the rendered page contains the
    // marker. (Chromium renders JSON as a text page; the marker still
    // appears in the resulting HTML body.)
    if (body.includes('"status":"ok"')) {
      process.exit(0);
    } else {
      console.error('healthcheck: missing marker in response');
      process.exit(1);
    }
  });
});
req.on('timeout', () => {
  console.error(`healthcheck: timed out after ${TIMEOUT_MS}ms`);
  req.destroy();
  process.exit(1);
});
req.on('error', (err) => {
  console.error('healthcheck: request error', err.message);
  process.exit(1);
});
