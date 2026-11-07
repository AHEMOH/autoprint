/* AutoPrint dashboard – drag & drop upload, preview overlay, confirmations, live status. */
(function () {
  "use strict";

  // ---- Bestätigungsdialoge ------------------------------------------------
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      if (!window.confirm(form.dataset.confirm)) {
        ev.preventDefault();
        return;
      }
      var btn = form.querySelector("button[type=submit]");
      if (btn) { btn.disabled = true; }
    });
  });

  // ---- Drag & Drop Upload --------------------------------------------------
  var zone   = document.getElementById("dropzone");
  var input  = document.getElementById("file-input");
  var form   = document.getElementById("upload-form");
  var status = document.getElementById("drop-status");

  function submitFiles(files) {
    if (!files || !files.length) { return; }
    var tooBig = Array.prototype.filter.call(files, function (f) { return f.size > 10 * 1024 * 1024; });
    if (tooBig.length) {
      status.textContent = "Zu groß (max. 10 MB): " + tooBig.map(function (f) { return f.name; }).join(", ");
      return;
    }
    status.textContent = files.length === 1
      ? "Lade " + files[0].name + " hoch …"
      : "Lade " + files.length + " Dateien hoch …";
    zone.classList.add("busy");
    form.submit();
  }

  if (zone && input && form) {
    ["dragenter", "dragover"].forEach(function (t) {
      zone.addEventListener(t, function (ev) { ev.preventDefault(); zone.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (t) {
      zone.addEventListener(t, function (ev) { ev.preventDefault(); zone.classList.remove("over"); });
    });
    zone.addEventListener("drop", function (ev) {
      var files = ev.dataTransfer && ev.dataTransfer.files;
      if (!files || !files.length) { return; }
      try {
        input.files = files;              // modern browsers allow assigning a FileList
      } catch (e) {
        status.textContent = "Drag & Drop wird von diesem Browser nicht unterstützt – bitte klicken.";
        return;
      }
      submitFiles(input.files);
    });
    input.addEventListener("change", function () { submitFiles(input.files); });

    // Auch Ablegen irgendwo auf der Seite landet in der Dropzone.
    document.addEventListener("dragover", function (ev) { ev.preventDefault(); });
    document.addEventListener("drop", function (ev) {
      if (zone.contains(ev.target)) { return; }
      ev.preventDefault();
      var files = ev.dataTransfer && ev.dataTransfer.files;
      if (files && files.length) {
        try { input.files = files; submitFiles(input.files); } catch (e) { /* ignore */ }
      }
    });
  }

  // ---- Vorschau-Overlay ----------------------------------------------------
  var overlay = document.getElementById("overlay");
  var img     = document.getElementById("overlay-img");
  var spinner = document.getElementById("overlay-spinner");
  var closeBtn = document.getElementById("overlay-close");

  function openPreview(url) {
    overlay.hidden = false;
    img.hidden = true;
    spinner.hidden = false;
    img.onload = function () { spinner.hidden = true; img.hidden = false; };
    img.onerror = function () { spinner.hidden = true; img.hidden = false; img.alt = "Vorschau konnte nicht geladen werden."; };
    img.src = url + (url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now();
    document.body.style.overflow = "hidden";
  }
  function closePreview() {
    overlay.hidden = true;
    img.removeAttribute("src");
    document.body.style.overflow = "";
  }
  document.querySelectorAll("[data-preview]").forEach(function (btn) {
    btn.addEventListener("click", function () { openPreview(btn.dataset.preview); });
  });
  if (overlay) {
    overlay.addEventListener("click", function (ev) { if (ev.target === overlay) { closePreview(); } });
    closeBtn.addEventListener("click", closePreview);
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape" && !overlay.hidden) { closePreview(); } });
  }

  // ---- Live-Status (Drucker, nächster Druck) alle 60 s ---------------------
  var chipPrinter = document.getElementById("chip-printer");
  var chipNext    = document.getElementById("chip-next");
  var kpiNext     = document.getElementById("kpi-next");
  var kpiNextRel  = document.getElementById("kpi-next-rel");

  function refreshStatus() {
    fetch("/status", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) {
        if (!s) { return; }
        if (chipPrinter) {
          chipPrinter.classList.toggle("chip-ok", !!s.printer_reachable);
          chipPrinter.classList.toggle("chip-err", !s.printer_reachable);
          chipPrinter.querySelector(".chip-text").textContent =
            s.printer_reachable ? "Drucker erreichbar" : "Drucker nicht erreichbar";
        }
        if (s.next_run && s.next_run.label && chipNext) {
          chipNext.querySelector(".chip-text").textContent =
            "Nächster Druck: " + s.next_run.label + (s.next_run.relative ? " · " + s.next_run.relative : "");
          if (kpiNext) { kpiNext.textContent = s.next_run.label; }
          if (kpiNextRel && s.next_run.relative) { kpiNextRel.textContent = s.next_run.relative; }
        }
      })
      .catch(function () { /* offline – keep what we have */ });
  }
  window.setInterval(refreshStatus, 60000);

  // Nach einem Druckauftrag die Seite nach kurzer Zeit neu laden, damit der
  // Verlauf den Ausgang zeigt (Druck läuft im Hintergrund-Thread).
  var flashes = document.querySelector(".flash-ok");
  if (flashes && /Druckauftrag gestartet/.test(flashes.textContent)) {
    window.setTimeout(function () { window.location.replace("/"); }, 20000);
  }
})();
