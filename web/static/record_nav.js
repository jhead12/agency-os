/* Move between prospect records without going back to the list.
 *
 * The prospect list remembers its filters and sort (sessionStorage, per tab).
 * The detail page asks the server for the previous/next prospect in that list
 * and lets the user move with the ‹ › buttons, the ← → keys, a swipe on a phone,
 * or a sideways two-finger scroll on a trackpad. The page slides out and the
 * next record slides in.
 */
(function () {
  "use strict";
  var LIST_KEY = "aos.prospectList";
  var ENTER_KEY = "aos.recordEnter";
  var LIST_PARAMS = ["q", "source", "stage", "cities", "campaign", "sort", "dir"];

  function store(key, value) {
    try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value); } catch (e) {}
  }
  function load(key) {
    try { return sessionStorage.getItem(key); } catch (e) { return null; }
  }

  // ── On the list: remember the filters and sort the user is working ──
  if (document.querySelector("[data-prospect-list]")) {
    var here = new URLSearchParams(location.search), keep = new URLSearchParams();
    LIST_PARAMS.forEach(function (k) { if (here.get(k)) keep.set(k, here.get(k)); });
    store(LIST_KEY, JSON.stringify({ query: keep.toString(), url: location.pathname + location.search }));
    return;
  }

  var nav = document.querySelector("[data-record-nav]");
  if (!nav) return;
  var id = nav.getAttribute("data-record-nav");
  var main = document.querySelector("main.container");
  var reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var list = {};
  try { list = JSON.parse(load(LIST_KEY) || "{}"); } catch (e) {}
  var targets = { prev: null, next: null };
  var leaving = false;

  if (list.url) {
    var back = document.querySelector("[data-record-back]");
    if (back) back.href = list.url;
  }

  fetch("/api/prospects/" + id + "/neighbors?" + (list.query || ""), { credentials: "same-origin" })
    .then(function (r) { return r.ok ? r.json() : { in_list: false }; })
    .then(function (data) {
      if (!data.in_list || data.total < 2) return;
      targets.prev = data.prev;
      targets.next = data.next;
      nav.querySelector("[data-record-pos]").textContent = data.position + " of " + data.total;
      ["prev", "next"].forEach(function (dir) {
        var a = nav.querySelector('[data-record-go="' + dir + '"]');
        if (targets[dir]) {
          a.href = "/prospects/" + targets[dir];
          a.removeAttribute("aria-disabled");
          var link = document.createElement("link");
          link.rel = "prefetch";
          link.href = a.href;
          document.head.appendChild(link);
        }
      });
      nav.hidden = false;
    })
    .catch(function () {});

  // Typed-but-unsaved changes would be lost by moving on: ask first.
  function unsaved() {
    var fields = main.querySelectorAll("input, textarea, select");
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      if (f.type === "hidden" || f.type === "submit" || f.disabled) continue;
      if (f.type === "checkbox" || f.type === "radio") { if (f.checked !== f.defaultChecked) return true; }
      else if (f.tagName === "SELECT") {
        // With no option marked selected, the first one is the default.
        var opts = f.options, marked = false;
        for (var j = 0; j < opts.length; j++) if (opts[j].defaultSelected) marked = true;
        for (j = 0; j < opts.length; j++) {
          if (opts[j].selected !== (marked ? opts[j].defaultSelected : j === 0 && !f.multiple)) return true;
        }
      } else if (f.value !== f.defaultValue) return true;
    }
    return false;
  }

  function go(dir) {
    if (leaving || !targets[dir]) { settle(); return false; }
    if (document.body.classList.contains("player-active")) { settle(); return false; }  // a workflow is driving
    if (unsaved() && !confirm("You have unsaved changes on this record. Leave without saving?")) { settle(); return false; }
    leaving = true;
    store(ENTER_KEY, dir);
    var url = "/prospects/" + targets[dir];
    if (reduced) { location.href = url; return true; }
    main.classList.add("record-moving");
    main.style.transform = "translateX(" + (dir === "next" ? -64 : 64) + "px)";
    main.style.opacity = "0";
    setTimeout(function () { location.href = url; }, 180);
    return true;
  }

  // Follow the finger/trackpad, then snap back if the user lets go early.
  function drag(dx) {
    if (reduced || leaving) return;
    main.classList.remove("record-moving");
    var damped = dx * 0.5;
    if (dx > 0 && !targets.prev || dx < 0 && !targets.next) damped = dx * 0.15;  // nothing there: resist
    main.style.transform = "translateX(" + damped + "px)";
    main.style.opacity = String(1 - Math.min(Math.abs(damped) / 400, 0.35));
  }
  function settle() {
    if (leaving) return;
    main.classList.add("record-moving");
    main.style.transform = "";
    main.style.opacity = "";
  }

  nav.addEventListener("click", function (e) {
    var a = e.target.closest("[data-record-go]");
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;  // new tab: let the browser do it
    e.preventDefault();
    go(a.getAttribute("data-record-go"));
  });

  function typing(el) {
    return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  }
  document.addEventListener("keydown", function (e) {
    if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (typing(document.activeElement) || document.activeElement.closest("#console-dock")) return;
    if (e.key === "ArrowRight" && go("next")) e.preventDefault();
    else if (e.key === "ArrowLeft" && go("prev")) e.preventDefault();
  });

  // Gestures that start in a field, the console, the player, or something
  // that scrolls sideways itself (a wide table) belong to that element.
  function ownsSideways(el) {
    for (; el && el !== document.body; el = el.parentElement) {
      if (typing(el) || el.id === "console-dock" || /\bplayer-/.test(el.className || "")) return true;
      if (el.scrollWidth > el.clientWidth + 1 && /(auto|scroll)/.test(getComputedStyle(el).overflowX)) return true;
    }
    return false;
  }

  // Phone: swipe left for the next record, right for the previous one.
  var touch = null;
  document.addEventListener("touchstart", function (e) {
    if (e.touches.length !== 1 || ownsSideways(e.target)) { touch = null; return; }
    touch = { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now(), sideways: null };
  }, { passive: true });
  document.addEventListener("touchmove", function (e) {
    if (!touch) return;
    var dx = e.touches[0].clientX - touch.x, dy = e.touches[0].clientY - touch.y;
    if (touch.sideways === null && (Math.abs(dx) > 10 || Math.abs(dy) > 10)) touch.sideways = Math.abs(dx) > Math.abs(dy) * 1.5;
    if (touch.sideways) drag(dx);
  }, { passive: true });
  document.addEventListener("touchend", function (e) {
    if (!touch || !touch.sideways) { touch = null; return; }
    var dx = e.changedTouches[0].clientX - touch.x;
    var fast = Math.abs(dx) / Math.max(Date.now() - touch.t, 1) > 0.5;
    touch = null;
    if (Math.abs(dx) > 90 || (fast && Math.abs(dx) > 40)) go(dx < 0 ? "next" : "prev");
    else settle();
  });
  document.addEventListener("touchcancel", function () { touch = null; settle(); });

  // Trackpad: a sideways two-finger scroll. Taking it also stops the
  // browser's own back/forward swipe from leaving the record.
  var wheel = 0, wheelTimer = null;
  document.addEventListener("wheel", function (e) {
    if (leaving || Math.abs(e.deltaX) <= Math.abs(e.deltaY) || ownsSideways(e.target)) return;
    e.preventDefault();
    wheel += e.deltaX;
    drag(-wheel);
    clearTimeout(wheelTimer);
    if (Math.abs(wheel) > 160) {
      var dir = wheel > 0 ? "next" : "prev";
      wheel = 0;
      go(dir);
      return;
    }
    wheelTimer = setTimeout(function () { wheel = 0; settle(); }, 160);
  }, { passive: false });

  // Coming back with the browser's back button: don't show a half-gone page.
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) { leaving = false; settle(); }
  });
})();
