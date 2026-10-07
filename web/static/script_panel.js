/* 📞 Call Scripts in a side panel on the prospect page, so the rep reads the
 * script next to the record and the Log a Call form instead of flipping
 * between pages. It stays open while moving to the next record (sessionStorage),
 * already filled in for the new prospect. Ctrl/⌘-click opens the full page.
 */
(function () {
  "use strict";
  var link = document.querySelector("[data-script-panel]");
  if (!link) return;
  var OPEN_KEY = "aos.scriptPanel";
  var prospectId = link.getAttribute("data-script-panel");
  var stage = link.getAttribute("data-stage") || "";
  var drawer = null, lastFocus = null;

  function remember(open) { try { if (open) sessionStorage.setItem(OPEN_KEY, "1"); else sessionStorage.removeItem(OPEN_KEY); } catch (e) {} }
  function wasOpen() { try { return sessionStorage.getItem(OPEN_KEY) === "1"; } catch (e) { return false; } }

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text) e.textContent = text;
    return e;
  }

  function build() {
    drawer = el("aside", { class: "script-drawer", "aria-label": "Call script", tabindex: "-1" });
    var head = el("header");
    head.appendChild(el("h2", {}, "📞 Call script"));
    var full = el("a", { href: link.href, class: "btn btn-sm btn-secondary", title: "Open the Call Scripts page" }, "Full page ↗");
    var close = el("button", { type: "button", class: "btn btn-sm btn-secondary", "aria-label": "Close the script panel", title: "Close (Esc)" }, "✕");
    close.addEventListener("click", function () { hide(); });
    head.appendChild(full);
    head.appendChild(close);
    drawer.appendChild(head);
    drawer.appendChild(el("div", { class: "script-drawer-body", "aria-live": "polite" }));
    // Stage tabs inside the panel reload the panel, not the page.
    drawer.addEventListener("click", function (e) {
      var tab = e.target.closest(".stage-tabs a");
      if (!tab || e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      load(new URL(tab.href, location.href).searchParams.get("stage") || "");
    });
    document.body.appendChild(drawer);
  }

  function load(forStage, fallback) {
    var body = drawer.querySelector(".script-drawer-body");
    body.textContent = "Loading the script…";
    var url = "/call-scripts?prospect_id=" + encodeURIComponent(prospectId) + (forStage ? "&stage=" + encodeURIComponent(forStage) : "");
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var cards = doc.querySelectorAll(".script-card");
        if (!cards.length && forStage && fallback !== false) { load("", false); return; }  // none for this stage: show all
        body.textContent = "";
        var tabs = doc.querySelector(".stage-tabs");
        if (tabs) body.appendChild(document.importNode(tabs, true));
        if (!cards.length) body.appendChild(el("p", { class: "empty-state" }, "No phone scripts for this campaign yet."));
        cards.forEach(function (c) { body.appendChild(document.importNode(c, true)); });
      })
      .catch(function () {
        body.textContent = "";
        body.appendChild(el("p", { class: "form-error" }, "Couldn't load the script. "));
        body.firstChild.appendChild(el("a", { href: url }, "Open it as a page instead"));
      });
  }

  function show(focus) {
    if (document.body.classList.contains("layout-editing")) return;
    if (!drawer) { build(); load(stage); }
    lastFocus = document.activeElement;
    document.body.classList.add("script-panel-open");
    link.setAttribute("aria-expanded", "true");
    remember(true);
    if (focus) drawer.focus();
    window.dispatchEvent(new Event("resize"));  // the floating Prev/Next re-aligns
  }

  function hide() {
    document.body.classList.remove("script-panel-open");
    link.setAttribute("aria-expanded", "false");
    remember(false);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    window.dispatchEvent(new Event("resize"));
  }

  link.setAttribute("aria-expanded", "false");
  link.addEventListener("click", function (e) {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;  // new tab: the full page
    e.preventDefault();
    if (document.body.classList.contains("script-panel-open")) hide(); else show(true);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape" || !document.body.classList.contains("script-panel-open")) return;
    var a = document.activeElement;
    if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && !drawer.contains(a)) return;
    hide();
  });
  window.aosScriptPanel = { show: show, hide: hide };
  if (wasOpen()) show(false);
})();
