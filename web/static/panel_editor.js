/* 🎨 Customize: a visual editor for a page's panels (core/panels.py).
 *
 * In edit mode every panel gets a toolbar: drag ⠿ (or ‹ ›) to move it, pick a
 * width, hide it. Clicking a panel selects it so the drawer can color it. The
 * drawer also colors the page background and adds panels from the library.
 * Save stores the layout for this user only; Cancel puts everything back.
 */
(function () {
  "use strict";
  var board = document.querySelector("[data-panel-page]");
  var button = document.querySelector("[data-layout-edit]");
  if (!board || !button) return;
  var page = board.getAttribute("data-panel-page");
  var main = document.querySelector("main.container");
  var pageInfo = document.querySelector("[data-page-layout]");
  var bgStyle = document.getElementById("page-background");
  var reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  var PAGE_COLORS = ["#0f172a", "#111827", "#1e1b4b", "#052e16", "#3b0764", "#f8fafc", "#fef3c7", "#e0f2fe"];
  var PANEL_COLORS = ["#1e293b", "#172554", "#14532d", "#4c0519", "#422006", "#3b0764", "#f1f5f9", "#fef9c3", "#dcfce7"];
  var WIDTHS = [["third", "⅓", "A third of the row", "Third"], ["two-thirds", "⅔", "Two thirds of the row", "Two thirds"],
                ["full", "▭", "The full row", "Full row"]];

  var editing = false, original = null, selected = null, dirty = false;
  var drawer = null, inerted = [];
  var background = pageInfo ? pageInfo.getAttribute("data-background") || null : null;
  // With no background of their own, the page takes the pipeline stage's color.
  var stageBackground = pageInfo ? pageInfo.getAttribute("data-stage-background") || null : null;
  var stage = pageInfo ? (pageInfo.getAttribute("data-stage") || "").replace(/_/g, " ") : "";

  // ── Layout state lives in the DOM: order, data-width, data-panel-color, hidden ──
  function cards() { return Array.prototype.slice.call(board.querySelectorAll(":scope > [data-panel]")); }
  function ordered() { return cards().sort(function (a, b) { return (+a.style.order || 0) - (+b.style.order || 0); }); }
  function label(card) { return card.getAttribute("data-panel-label"); }

  function snapshot() {
    var list = ordered();
    return {
      background: background,
      panels: list.filter(function (c) { return !c.hidden; }).map(function (c) {
        return { id: c.getAttribute("data-panel"), width: c.getAttribute("data-width"),
                 color: c.getAttribute("data-panel-color") || null };
      }),
      hidden: list.filter(function (c) { return c.hidden; }).map(function (c) { return c.getAttribute("data-panel"); }),
    };
  }

  function apply(layout) {
    var byId = {};
    cards().forEach(function (c) { byId[c.getAttribute("data-panel")] = c; });
    var order = layout.panels.map(function (p) { return p.id; }).concat(layout.hidden);
    order.forEach(function (id, i) { if (byId[id]) byId[id].style.order = i; });
    layout.panels.forEach(function (p) {
      var c = byId[p.id];
      if (!c) return;
      c.hidden = false;
      c.setAttribute("data-width", p.width);
      setColor(c, p.color);
    });
    layout.hidden.forEach(function (id) { if (byId[id]) byId[id].hidden = true; });
    setBackground(layout.background);
  }

  // Same rule as core/panels.py tone(): which text color reads on a color.
  function tone(hex) {
    var lin = [1, 3, 5].map(function (i) {
      var c = parseInt(hex.slice(i, i + 2), 16) / 255;
      return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2] > 0.18 ? "light" : "dark";
  }

  function setColor(card, color) {
    if (color) {
      card.style.setProperty("--panel-color", color);
      card.setAttribute("data-panel-color", color);
      card.setAttribute("data-panel-tone", tone(color));
    } else {
      card.style.removeProperty("--panel-color");
      card.removeAttribute("data-panel-color");
      card.removeAttribute("data-panel-tone");
    }
  }

  function setBackground(color) {
    background = color || null;
    var shown = background || stageBackground;
    bgStyle.textContent = shown ? "body { background: " + shown + "; }" : "";
    if (background) main.setAttribute("data-page-tone", tone(background));
    else main.removeAttribute("data-page-tone");
  }

  function changed() { dirty = true; status(""); renderDrawer(); }

  // Move panels with a short slide (FLIP) instead of a jump.
  function reorder(list) {
    var before = new Map();
    list.forEach(function (c) { if (!c.hidden) before.set(c, c.getBoundingClientRect()); });
    list.forEach(function (c, i) { c.style.order = i; });
    if (reduced) return;
    board.classList.remove("panel-animating");
    before.forEach(function (rect, c) {
      var now = c.getBoundingClientRect();
      var dx = rect.left - now.left, dy = rect.top - now.top;
      if (dx || dy) c.style.transform = "translate(" + dx + "px," + dy + "px)";
    });
    board.offsetWidth;  // eslint-disable-line no-unused-expressions -- commit the start position
    board.classList.add("panel-animating");
    before.forEach(function (rect, c) { c.style.transform = ""; });
    setTimeout(function () { board.classList.remove("panel-animating"); }, 220);
  }

  function move(card, step) {
    var list = ordered().filter(function (c) { return !c.hidden; });
    var i = list.indexOf(card), j = i + step;
    if (j < 0 || j >= list.length) return;
    list.splice(i, 1);
    list.splice(j, 0, card);
    reorder(list.concat(ordered().filter(function (c) { return c.hidden; })));
    changed();
  }

  function hide(card) {
    card.hidden = true;
    reorder(ordered().filter(function (c) { return c !== card; }).concat([card]));
    if (selected === card) selected = null;
    changed();
  }

  function add(card) {
    var visible = ordered().filter(function (c) { return !c.hidden; });
    card.hidden = false;
    reorder(visible.concat([card], ordered().filter(function (c) { return c.hidden; })));
    decorate(card);
    select(card);
    changed();
    card.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" });
    card.classList.remove("panel-flash");
    void card.offsetWidth;
    card.classList.add("panel-flash");
  }

  function select(card) {
    if (selected) selected.classList.remove("panel-selected");
    selected = card;
    if (card) card.classList.add("panel-selected");
    renderDrawer();
  }

  // ── Toolbars ──
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text) e.textContent = text;
    return e;
  }

  function decorate(card) {
    if (card.querySelector(":scope > .panel-toolbar")) return;
    Array.prototype.forEach.call(card.children, function (child) {
      if (!child.inert) { child.inert = true; inerted.push(child); }
    });
    var bar = el("div", { class: "panel-toolbar", role: "toolbar", "aria-label": label(card) });
    var handle = el("button", { type: "button", class: "panel-handle", title: "Drag to move", "aria-label": "Drag to move " + label(card) }, "⠿");
    handle.addEventListener("pointerdown", function (e) { startDrag(e, card); });
    bar.appendChild(handle);
    bar.appendChild(el("span", { class: "panel-name", title: label(card) }, label(card)));
    [["‹", -1, "Move earlier"], ["›", 1, "Move later"]].forEach(function (m) {
      var b = el("button", { type: "button", title: m[2], "aria-label": m[2] }, m[0]);
      b.addEventListener("click", function () { move(card, m[1]); });
      bar.appendChild(b);
    });
    WIDTHS.forEach(function (w) {
      var b = el("button", { type: "button", title: w[2], "aria-label": w[2], "data-width-set": w[0] }, w[1]);
      b.addEventListener("click", function () { card.setAttribute("data-width", w[0]); reorder(ordered()); changed(); });
      bar.appendChild(b);
    });
    var x = el("button", { type: "button", title: "Hide this panel", "aria-label": "Hide " + label(card) }, "✕");
    x.addEventListener("click", function () { hide(card); });
    bar.appendChild(x);
    card.insertBefore(bar, card.firstChild);
    syncToolbar(card);
  }

  function syncToolbar(card) {
    var w = card.getAttribute("data-width");
    card.querySelectorAll(":scope > .panel-toolbar [data-width-set]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-width-set") === w));
    });
  }

  // ── Drag and drop (pointer events: mouse, pen and touch) ──
  var drag = null;
  function startDrag(e, card) {
    if (e.button) return;
    e.preventDefault();
    select(card);
    var ghost = el("div", { class: "panel-ghost" }, label(card));
    document.body.appendChild(ghost);
    drag = { card: card, ghost: ghost, lock: 0, x: e.clientX, y: e.clientY };
    card.classList.add("panel-dragging");
    placeGhost(e);
    document.addEventListener("pointermove", onDrag);
    document.addEventListener("pointerup", endDrag);
    document.addEventListener("pointercancel", endDrag);
    requestAnimationFrame(autoScroll);
  }
  function placeGhost(e) { drag.ghost.style.left = e.clientX + "px"; drag.ghost.style.top = e.clientY + "px"; }
  function onDrag(e) {
    if (!drag) return;
    drag.x = e.clientX; drag.y = e.clientY;
    placeGhost(e);
    if (Date.now() < drag.lock) return;  // let the last move finish before deciding again
    var over = document.elementFromPoint(e.clientX, e.clientY);
    var target = over && over.closest(".panel-board > [data-panel]");
    if (!target || target === drag.card || target.hidden) return;
    var r = target.getBoundingClientRect();
    var after = target.getAttribute("data-width") === "full" || r.width > board.clientWidth * 0.8
      ? e.clientY > r.top + r.height / 2
      : e.clientX > r.left + r.width / 2;
    var list = ordered().filter(function (c) { return c !== drag.card; });
    list.splice(list.indexOf(target) + (after ? 1 : 0), 0, drag.card);
    var now = ordered();
    if (list.every(function (c, i) { return c === now[i]; })) return;
    reorder(list);
    drag.lock = Date.now() + 230;
    dirty = true;
  }
  function autoScroll() {
    if (!drag) return;
    var edge = 70, speed = 0;
    if (drag.y < edge + 56) speed = -14;
    else if (drag.y > innerHeight - edge) speed = 14;
    if (speed) window.scrollBy(0, speed);
    requestAnimationFrame(autoScroll);
  }
  function endDrag() {
    if (!drag) return;
    drag.card.classList.remove("panel-dragging");
    drag.ghost.remove();
    drag = null;
    document.removeEventListener("pointermove", onDrag);
    document.removeEventListener("pointerup", endDrag);
    document.removeEventListener("pointercancel", endDrag);
    changed();
  }

  // ── The drawer ──
  function swatches(colors, current, onPick, noneLabel) {
    var wrap = el("div", { class: "swatches" });
    var none = el("button", { type: "button", class: "swatch swatch-none", title: noneLabel, "aria-label": noneLabel,
                              "aria-pressed": String(!current) });
    none.addEventListener("click", function () { onPick(null); });
    wrap.appendChild(none);
    colors.forEach(function (c) {
      var b = el("button", { type: "button", class: "swatch", title: c, "aria-label": "Color " + c,
                             "aria-pressed": String(current === c), style: "--swatch: " + c });
      b.addEventListener("click", function () { onPick(c); });
      wrap.appendChild(b);
    });
    var custom = el("label", { class: "swatch swatch-custom", title: "Any color" });
    var input = el("input", { type: "color", "aria-label": "Pick any color" });
    input.value = current || "#3b82f6";
    input.addEventListener("input", function () { onPick(input.value.toLowerCase(), true); });
    input.addEventListener("change", function () { renderDrawer(); });
    custom.appendChild(input);
    wrap.appendChild(custom);
    return wrap;
  }

  function section(title) {
    var s = el("section");
    s.appendChild(el("h3", {}, title));
    return s;
  }

  function renderDrawer() {
    if (!drawer) return;
    cards().forEach(syncToolbar);
    var body = drawer.querySelector(".drawer-body");
    body.textContent = "";

    var bg = section("Page background");
    bg.appendChild(swatches(PAGE_COLORS, background, function (c, live) {
      setBackground(c); dirty = true; if (!live) renderDrawer();
    }, stageBackground ? "Match the stage (" + stage + ")" : "Default background"));
    if (stageBackground && !background) bg.appendChild(el("p", { class: "muted" }, "Following the stage: " + stage + ". Pick a color to use your own instead."));
    body.appendChild(bg);

    var sel = section(selected ? "Selected: " + label(selected) : "Panel");
    sel.classList.add("panel-settings");
    if (!selected) {
      sel.appendChild(el("p", { class: "muted" }, "Click a panel to change its size or color."));
    } else {
      var size = el("div", { class: "setting" });
      size.appendChild(el("label", {}, "Width"));
      var seg = el("div", { class: "segmented", role: "group", "aria-label": "Width" });
      WIDTHS.forEach(function (w) {
        var b = el("button", { type: "button", "aria-pressed": String(selected.getAttribute("data-width") === w[0]) },
                   w[3]);
        b.addEventListener("click", function () { selected.setAttribute("data-width", w[0]); reorder(ordered()); changed(); });
        seg.appendChild(b);
      });
      size.appendChild(seg);
      sel.appendChild(size);
      var color = el("div", { class: "setting" });
      color.appendChild(el("label", {}, "Color"));
      color.appendChild(swatches(PANEL_COLORS, selected.getAttribute("data-panel-color"), function (c, live) {
        setColor(selected, c); dirty = true; if (!live) renderDrawer();
      }, "Default color"));
      sel.appendChild(color);
      var hideBtn = el("button", { type: "button", class: "btn btn-sm btn-secondary" }, "Hide this panel");
      hideBtn.addEventListener("click", function () { hide(selected); });
      sel.appendChild(hideBtn);
    }
    body.appendChild(sel);

    var lib = section("Add panels");
    var hidden = ordered().filter(function (c) { return c.hidden; });
    if (!hidden.length) lib.appendChild(el("p", { class: "muted" }, "Every panel is on the page."));
    hidden.forEach(function (card) {
      var item = el("div", { class: "library-item" });
      var text = el("div");
      text.appendChild(el("strong", {}, label(card)));
      if (card.getAttribute("data-panel-about")) text.appendChild(el("span", {}, card.getAttribute("data-panel-about")));
      item.appendChild(text);
      var b = el("button", { type: "button", class: "btn btn-sm", "aria-label": "Add " + label(card) }, "+ Add");
      b.addEventListener("click", function () { add(card); });
      item.appendChild(b);
      lib.appendChild(item);
    });
    body.appendChild(lib);
  }

  function status(text, error) {
    if (!drawer) return;
    var s = drawer.querySelector(".layout-status");
    s.textContent = text;
    s.classList.toggle("error", !!error);
  }

  function buildDrawer() {
    drawer = el("aside", { class: "layout-drawer", "aria-label": "Customize this page" });
    var head = el("header");
    head.appendChild(el("h2", {}, "Customize this page"));
    head.appendChild(el("p", {}, "Drag ⠿ to move panels, click one to size or color it. Only you see your layout."));
    drawer.appendChild(head);
    drawer.appendChild(el("div", { class: "drawer-body" }));
    var foot = el("footer");
    var save = el("button", { type: "button", class: "btn btn-sm" }, "Save");
    save.addEventListener("click", function () { persist(snapshot(), "Saved."); });
    var cancel = el("button", { type: "button", class: "btn btn-sm btn-secondary" }, "Cancel");
    cancel.addEventListener("click", function () { apply(original); stop(); });
    var reset = el("button", { type: "button", class: "btn btn-sm btn-secondary btn-link" }, "Reset to default");
    reset.addEventListener("click", function () {
      if (confirm("Go back to the standard layout for this page?")) persist(null, "Back to the default layout.", true);
    });
    foot.appendChild(save);
    foot.appendChild(cancel);
    foot.appendChild(reset);
    foot.appendChild(el("div", { class: "layout-status", role: "status", style: "flex-basis: 100%" }));
    drawer.appendChild(foot);
    document.body.appendChild(drawer);
  }

  function persist(layout, message, reload) {
    status("Saving…");
    fetch("/api/layouts/" + page, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-AOS-Layout": "1" },
      body: JSON.stringify({ layout: layout }),
    }).then(function (r) { return r.json().catch(function () { return { ok: false, error: "Couldn't save (" + r.status + ")" }; }); })
      .then(function (data) {
        if (!data.ok) { status(data.error || "Couldn't save.", true); return; }
        if (reload) { dirty = false; location.reload(); return; }
        stop();
        toast(message);
      })
      .catch(function () { status("Couldn't reach the server. Your changes are still here; try Save again.", true); });
  }

  function toast(text) {
    var t = el("div", { class: "save-notice", role: "status", style: "position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); z-index: 950;" }, text);
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 2500);
  }

  // ── Entering and leaving edit mode ──
  function start() {
    if (editing) return;
    if (window.aosScriptPanel) window.aosScriptPanel.hide();  // both use the right side
    editing = true;
    dirty = false;
    original = snapshot();
    document.body.classList.add("layout-editing");
    button.setAttribute("aria-pressed", "true");
    cards().forEach(function (c) { if (!c.hidden) decorate(c); });
    buildDrawer();
    renderDrawer();
  }

  function stop() {
    editing = false;
    dirty = false;
    endDrag();
    select(null);
    inerted.forEach(function (child) { child.inert = false; });
    inerted = [];
    board.querySelectorAll(".panel-toolbar").forEach(function (t) { t.remove(); });
    if (drawer) { drawer.remove(); drawer = null; }
    document.body.classList.remove("layout-editing");
    button.setAttribute("aria-pressed", "false");
  }

  button.addEventListener("click", function () { if (editing) { apply(original); stop(); } else start(); });
  board.addEventListener("click", function (e) {
    if (!editing || e.target.closest(".panel-toolbar button")) return;
    var card = e.target.closest(".panel-board > [data-panel]");
    if (card) select(card);
  });
  document.addEventListener("keydown", function (e) {
    if (editing && e.key === "Escape" && selected) select(null);
  });
  window.addEventListener("beforeunload", function (e) {
    if (editing && dirty) { e.preventDefault(); e.returnValue = ""; }
  });
})();
