// Subscribing a device. Everything here happens once, on a button press, and
// only the browser can do it — which is why there is no server-side path to
// "add my phone": a subscription is minted by the browser or not at all.
(function () {
  var button = document.getElementById("enable-notifications");
  if (!button) return;

  var status = document.getElementById("notification-status");

  function say(message) {
    if (status) status.textContent = message;
  }

  // base64url → the Uint8Array the Push API wants for applicationServerKey.
  function decodeKey(value) {
    var padded = (value + "=".repeat((4 - (value.length % 4)) % 4))
      .replace(/-/g, "+")
      .replace(/_/g, "/");
    var raw = atob(padded);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  function encodeKey(buffer) {
    var bytes = new Uint8Array(buffer);
    var binary = "";
    for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  // Waits for the worker this registration installed, rather than for
  // `navigator.serviceWorker.ready` — which resolves only once a worker
  // controls *this page*, and so never resolved at all while the worker was
  // scoped to /static. Subscribing needs an active worker, not a controlling
  // one, and this waits for exactly that.
  function activated(registration) {
    if (registration.active) return Promise.resolve(registration);
    var worker = registration.installing || registration.waiting;
    if (!worker) return Promise.resolve(registration);
    return new Promise(function (resolve, reject) {
      var timer = window.setTimeout(function () {
        reject(new Error("The service worker did not start. Try reloading the page."));
      }, 10000);
      worker.addEventListener("statechange", function () {
        if (worker.state === "activated") {
          window.clearTimeout(timer);
          resolve(registration);
        }
        if (worker.state === "redundant") {
          window.clearTimeout(timer);
          reject(new Error("The service worker failed to install."));
        }
      });
    });
  }

  button.addEventListener("click", function () {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      say("This browser cannot receive push notifications.");
      return;
    }

    button.disabled = true;
    say("Asking permission…");

    Notification.requestPermission()
      .then(function (permission) {
        if (permission !== "granted") {
          throw new Error(
            "Notifications are blocked for this site. Allow them in your browser settings."
          );
        }
        // Served from the root, so its scope is the whole app rather than
        // /static — see the route that serves it.
        return navigator.serviceWorker.register("/sw.js");
      })
      .then(activated)
      .then(function (registration) {
        return registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: decodeKey(button.dataset.vapidKey),
        });
      })
      .then(function (subscription) {
        var body = new URLSearchParams({
          endpoint: subscription.endpoint,
          p256dh: encodeKey(subscription.getKey("p256dh")),
          auth: encodeKey(subscription.getKey("auth")),
          label: button.dataset.deviceLabel || "This device",
        });
        return fetch(button.dataset.subscribeUrl, { method: "POST", body: body });
      })
      .then(function (response) {
        if (!response.ok) throw new Error("Workbench did not accept the subscription.");
        // Reload rather than patching the list in: the device row is rendered
        // by the server like everything else on this page.
        window.location.reload();
      })
      .catch(function (error) {
        button.disabled = false;
        say(error.message || String(error));
      });
  });
})();
