/* Recovery Detector service worker — basic offline support.
 *
 * v2: SHELL (/, /app.js, /manifest.json) is now NETWORK-FIRST, not cache-first.
 * Under v1's cache-first strategy, once a browser cached app.js it NEVER
 * refetched it — a normal page reload still hit the SW's fetch handler, got a
 * cache hit, and served the old file forever. Deploying new app.js changed the
 * file on the server but not this sw.js script, so browsers never even detected
 * an update to install. Users silently kept running stale JS against the live
 * (changed) backend — e.g. old synchronous-fetch Simulator code misreading the
 * new async {job_id,status:"running"} response as a malformed final result.
 * The cache version bump below forces this fix to actually reach existing
 * users immediately; network-first for SHELL prevents the class of bug from
 * recurring on future deploys (cache is now only an offline fallback).
 */
var CACHE = 'recovery-v2';
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
  var path = new URL(url).pathname;

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

  /* App shell (HTML/JS carrying logic that changes every deploy): network-first,
     falling back to cache only when offline — never serve stale logic while online. */
  if (SHELL.indexOf(path) !== -1) {
    e.respondWith(
      fetch(e.request).then(function (res) {
        if (res.ok) {
          var clone = res.clone();
          caches.open(CACHE).then(function (c) { c.put(e.request, clone); });
        }
        return res;
      }).catch(function () {
        return caches.match(e.request).then(function (cached) {
          return cached || (e.request.mode === 'navigate' ? caches.match('/') : undefined);
        });
      })
    );
    return;
  }

  /* Other static assets (icons, etc.): cache-first, fetch & cache on miss */
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
