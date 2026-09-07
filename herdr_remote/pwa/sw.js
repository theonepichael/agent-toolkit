// Install-only service worker — no request-intercepting handler on purpose
// (iron-logbook precedent): installability does not require one, and a
// passthrough handler would intercept every request through the worker
// thread for no benefit.
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});
