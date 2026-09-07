// The service worker exists for one reason: a push arrives when no page is
// open, so something has to be running that is not a page. It is deliberately
// the smallest thing that can be — this app renders on the server, and a
// service worker that started caching or intercepting fetches would quietly
// become a second, stale copy of it.

self.addEventListener("push", function (event) {
  var payload = { title: "Workbench", body: "", url: "/" };
  try {
    if (event.data) payload = Object.assign(payload, event.data.json());
  } catch (error) {
    // A push with no readable payload is still worth showing: something
    // happened, and saying so beats saying nothing.
  }

  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      // Same tag per run collapses repeats rather than stacking them.
      tag: payload.url,
      data: { url: payload.url },
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/favicon-32.png",
    })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/";

  // Focus a tab that is already here rather than opening a third one. On a
  // phone this is the difference between the app you had open and a new copy
  // of it.
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (windows) {
      for (var i = 0; i < windows.length; i++) {
        if (windows[i].url.indexOf(url) !== -1 && "focus" in windows[i]) {
          return windows[i].focus();
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
