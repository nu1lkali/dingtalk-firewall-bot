/* ============================================================
   动态云防火墙 · Service Worker
   只缓存静态外壳，API 一律走网络（鉴权数据不落缓存）
   ============================================================ */
const VERSION = 'dcf-v1';
const SHELL = [
  '/static/style.css',
  '/static/app.js',
  '/static/manifest.webmanifest',
  '/static/offline.html',
  '/static/icon-192.png',
  '/static/icon-512.png',
];
const NETWORK_ONLY = ['/api/', '/webhook/', '/dingtalk/', '/metrics', '/health'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      .catch(() => null)
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function isNetworkOnly(url) {
  return NETWORK_ONLY.some((prefix) => url.pathname.startsWith(prefix));
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (isNetworkOnly(url)) return; // 交给浏览器，不走 SW 逻辑

  /* 页面导航：网络优先，断网时给离线提示页 */
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match('/static/offline.html'))
    );
    return;
  }

  /* 静态资源：网络优先 + 缓存兜底
     不用 stale-while-revalidate，是因为管理后台升级后如果先给旧 CSS/JS，
     会出现「刷新一次还是旧界面」的困惑；宁可慢一点也要拿到最新版本。 */
  event.respondWith(
    fetch(req)
      .then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(VERSION).then((cache) => cache.put(req, copy));
        }
        return resp;
      })
      .catch(() => caches.match(req))
  );
});
