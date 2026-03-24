const prerender = require('prerender');

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

server.use(require('prerender-filesystem-cache'));

server.start();
