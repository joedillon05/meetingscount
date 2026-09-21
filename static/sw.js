// Service worker for the participant app. Served from /sw.js so its scope is
// the whole site, but it only ever touches the app shell.
//
// Network first, cache as the fallback: people always get the newest code
// when they have a signal, and the app still opens (and says it is offline)
// when they don't. API responses are never cached. They hold personal data,
// and a check-in against a stale meeting list would be worse than none.

const CACHE = 'mc-shell-v2';
const SHELL = [
  '/',
  '/static/app/tokens.css',
  '/static/app/base.css',
  '/static/app/participant.css',
  '/static/app/participant.js',
  '/static/app/lib.js',
  '/static/icons/favicon.svg',
  '/static/icons/icon-192.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

function isShell(url) {
  return url.origin === location.origin && (
    url.pathname === '/' || url.pathname === '/manifest.webmanifest' ||
    url.pathname.startsWith('/static/app/') || url.pathname.startsWith('/static/icons/'));
}

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || !isShell(url)) return;      // everything else: straight to the network
  e.respondWith(
    fetch(e.request).then(res => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(url.pathname === '/' ? '/' : e.request, copy));
      }
      return res;
    }).catch(() => caches.match(url.pathname === '/' ? '/' : e.request))
  );
});
