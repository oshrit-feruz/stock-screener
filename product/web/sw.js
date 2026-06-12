/* Recovery Detector service worker — basic offline support */
var CACHE = 'recovery-v1';
var SHELL = ['/', '/app.js', '/manifest.json'];

self.addEventListener('install', function (e) {
  e.waitUntil(
    caches.open(CACHE)
      .then(function (c) { return c.addAll(SHELL); })
      .catch(function () { /* ignore cache-priming failures on first install */ })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', function (e) {
  var url = e.request.url;

  /* API calls: network-first, fall back to offline message */
  if (url.includes('/api/')) {
    e.respondWith(
      fetch(e.request).catch(function () {
        return new Response(
          JSON.stringify({ error: 'offline', message: 'You are offline. Last data may be stale.' }),
          { headers: { 'Content-Type': 'application/json' } }
        );
      })
    );
    return;
  }

  /* Static assets: cache-first, fetch & cache on miss */
  e.respondWith(
    caches.match(e.request).then(function (cached) {
      if (cached) return cached;
      return fetch(e.request).then(function (res) {
        if (res.ok) {
          var clone = res.clone();
          caches.open(CACHE).then(function (c) { c.put(e.request, clone); });
        }
        return res;
      }).catch(function () {
        /* Offline fallback for navigation */
        if (e.request.mode === 'navigate') {
          return caches.match('/');
        }
      });
    })
  );
});
