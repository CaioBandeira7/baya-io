"use strict";
(function () {
  const form = document.getElementById("pairForm");
  const pin = document.getElementById("pin");
  const err = document.getElementById("err");
  pin.focus();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    err.textContent = "";
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ pin: pin.value.trim() }),
      });
      if (res.ok) { window.location.replace("/"); return; }
      const body = await res.json().catch(() => ({}));
      err.textContent = body.detail || "Pairing failed.";
      pin.select();
    } catch (e) {
      err.textContent = "Cannot reach your PC. Check that both devices are on the same Wi-Fi.";
    }
  });
})();
