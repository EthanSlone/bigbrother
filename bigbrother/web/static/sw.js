// Minimal service worker: makes the app installable. Nothing is cached: camera
// footage and events must always be live, and nothing private is kept on the device.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
