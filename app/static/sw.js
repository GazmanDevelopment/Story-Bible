/* Story Bible - service worker.
 *
 * Present so the web app is installable on a phone. It deliberately caches
 * nothing and never calls respondWith: every request goes straight to the
 * network exactly as it would without a worker, so a deploy is picked up
 * immediately (see the no-cache policy in app/main.py) and /api/* and sign-in
 * responses can never be served stale. Offline support, if wanted, is a
 * separate piece of work.
 */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => { /* network as normal */ });
