"use strict";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// ponytail: red privada sin caché; agregar una pantalla offline solo si llega a ser necesaria.
self.addEventListener("fetch", () => {});
