{% load static %}
const CACHE_NAME = "gestao-cd-pwa-{{ app_version|escapejs }}";
const OFFLINE_URL = "/offline/";
const CORE_ASSETS = [
  OFFLINE_URL,
  "{% static 'painel/img/gestao-cd-192.png' %}",
  "{% static 'painel/img/gestao-cd-512.png' %}"
];

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(CACHE_NAME).then(function (cache) {
    return cache.addAll(CORE_ASSETS);
  }).then(function () {
    return self.skipWaiting();
  }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (key) {
      return (key.startsWith("krill-pwa-") || key.startsWith("modelo-teste-pwa-") || key.startsWith("gestao-cd-pwa-")) && key !== CACHE_NAME;
    }).map(function (key) {
      return caches.delete(key);
    }));
  }).then(function () {
    return self.clients.claim();
  }));
});

self.addEventListener("fetch", function (event) {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request).catch(function () {
      return caches.match(OFFLINE_URL);
    }));
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(caches.match(event.request).then(function (cached) {
      if (cached) return cached;
      return fetch(event.request).then(function (response) {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(function (cache) { cache.put(event.request, copy); });
        }
        return response;
      });
    }));
  }
});

self.addEventListener("push", function (event) {
  let payload = {};
  if (event.data) {
    try {
      payload = event.data.json();
    } catch (error) {
      payload = { title: "Gestão CD", body: event.data.text() };
    }
  }
  const title = payload.title || "Gestão CD";
  const options = {
    body: payload.body || "Existe uma atualização no sistema.",
    icon: "{% static 'painel/img/gestao-cd-192.png' %}",
    badge: "{% static 'painel/img/gestao-cd-192.png' %}",
    tag: payload.tag || "gestao-cd",
    data: {
      url: payload.url || "/",
      notification_id: payload.notification_id || null
    }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  const targetUrl = event.notification.data && event.notification.data.url ? event.notification.data.url : "/";
  event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (clientList) {
    for (const client of clientList) {
      if ("focus" in client) {
        client.navigate(targetUrl);
        return client.focus();
      }
    }
    if (clients.openWindow) return clients.openWindow(targetUrl);
    return null;
  }));
});
