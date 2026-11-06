/* Oi Nanda: recebe as notificações (Web Push) e abre a conversa certa ao tocar. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
const IOS = /iPhone|iPad|iPod/.test(self.navigator.userAgent);
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { title: 'Oi Nanda', body: e.data ? e.data.text() : '' }; }
  e.waitUntil((async () => {
    const cs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    // a pessoa já está olhando o Oi Nanda neste aparelho: não repete (no iPhone sempre mostra, regra da Apple)
    if (!IOS && cs.some(c => c.visibilityState === 'visible' && c.focused)) { cs.forEach(c => c.postMessage({ push: d })); return; }
    await self.registration.showNotification(d.title || 'Oi Nanda', {
      body: d.body || '', icon: '/icon-192.png', badge: '/badge.png', tag: d.tag || 'oinanda', renotify: true, data: { url: d.url || '/conversas' }
    });
  })());
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/conversas';
  e.waitUntil((async () => {
    const cs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of cs) { if ('focus' in c) { c.postMessage({ open: url }); return c.focus(); } }
    return self.clients.openWindow(url);
  })());
});
