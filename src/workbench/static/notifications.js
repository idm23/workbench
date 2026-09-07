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
        return navigator.serviceWorker.register("/static/sw.js");
      })
      .then(function (registration) {
        return navigator.serviceWorker.ready.then(function () {
          return registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: decodeKey(button.dataset.vapidKey),
          });
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
