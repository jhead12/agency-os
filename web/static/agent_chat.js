/* The chat robot: talk with a sales persona from any page.
 *
 * On a prospect's page the agent sees that prospect's record. The conversation
 * lives in this tab (sessionStorage), one per agent and prospect, and goes to
 * the server whole on each turn; the server keeps nothing and sends nothing.
 */
(function () {
  "use strict";
  var launcher = document.getElementById("chat-launcher");
  var dock = document.getElementById("chat-dock");
  if (!launcher || !dock) return;
  var log = document.getElementById("chat-log");
  var agentSelect = document.getElementById("chat-agent");
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-input");
  var prospect = dock.dataset.prospect ? Number(dock.dataset.prospect) : null;
  var prospectName = dock.dataset.prospectName || "";
  var canSave = dock.dataset.canSave === "1";
  var OPEN_KEY = "aos.chat.open", AGENT_KEY = "aos.chat.agent";
  var messages = [];
  var waiting = false;

  function store(key, value) {
    try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value); } catch (e) {}
  }
  function load(key) {
    try { return sessionStorage.getItem(key); } catch (e) { return null; }
  }
  function threadKey() { return "aos.chat.thread." + agentSelect.value + "." + (prospect || "none"); }
  function save() { store(threadKey(), messages.length ? JSON.stringify(messages) : null); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function actions(text) {
    var row = el("div", "chat-actions");
    var copy = el("button", "chat-action", "Copy");
    copy.type = "button";
    copy.addEventListener("click", function () {
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { copy.textContent = "Copied"; });
    });
    row.appendChild(copy);
    if (prospect && canSave) {
      var note = el("button", "chat-action", "Save as note");
      note.type = "button";
      note.addEventListener("click", function () {
        note.disabled = true;
        post("/prospects/" + prospect + "/agent/note", new URLSearchParams({ note: text })).then(function (reply) {
          note.textContent = reply.ok ? "Saved" : "Not saved";
        });
      });
      row.appendChild(note);
    }
    return row;
  }

  function bubble(message) {
    var node = el("div", "chat-msg chat-msg-" + message.role);
    node.appendChild(el("div", "chat-text", message.content));
    if (message.role === "assistant") node.appendChild(actions(message.content));
    log.appendChild(node);
    return node;
  }

  function render() {
    log.textContent = "";
    var option = agentSelect.selectedOptions[0];
    var intro = "Hi, I'm " + option.textContent.trim() + ". " +
      (prospect ? "I can see " + prospectName + "'s record. " : "") + "What are you working on?";
    log.appendChild(el("p", "chat-intro muted", intro));
    messages.forEach(bubble);
    log.scrollTop = log.scrollHeight;
  }

  function loadThread() {
    try { messages = JSON.parse(load(threadKey()) || "[]"); } catch (e) { messages = []; }
    if (!Array.isArray(messages)) messages = [];
    // A question left unanswered (the page changed mid-reply) can't be followed by another.
    if (messages.length && messages[messages.length - 1].role === "user") messages.pop();
    render();
  }

  function post(path, body, json) {
    var headers = { "X-AOS-Tool": "1" };
    if (json) headers["Content-Type"] = "application/json";
    return fetch(path, { method: "POST", headers: headers, body: body })
      .then(function (r) { return r.json(); })
      .catch(function () { return { ok: false, error: "Network error" }; });
  }

  function setOpen(open) {
    dock.hidden = !open;
    launcher.setAttribute("aria-expanded", String(open));
    launcher.classList.toggle("chat-launcher-open", open);
    store(OPEN_KEY, open ? "1" : null);
    if (open) {
      log.scrollTop = log.scrollHeight;
      if (input) input.focus();
    }
  }

  function send(text) {
    if (waiting || !text.trim()) return;
    waiting = true;
    messages.push({ role: "user", content: text.trim() });
    bubble(messages[messages.length - 1]);
    input.value = "";
    var typing = log.appendChild(el("div", "chat-msg chat-msg-assistant chat-typing", "…"));
    log.scrollTop = log.scrollHeight;
    var body = { agent: agentSelect.value, messages: messages };
    if (prospect) body.prospect_id = prospect;
    post("/agent/chat", JSON.stringify(body), true).then(function (reply) {
      typing.remove();
      waiting = false;
      if (reply.ok) {
        messages.push({ role: "assistant", content: reply.text });
        bubble(messages[messages.length - 1]);
        save();
      } else {
        // Take the message back so it can be sent again.
        var unsent = messages.pop();
        log.lastElementChild.remove();
        if (!input.value) input.value = unsent.content;
        log.appendChild(el("p", "chat-error", "Couldn't reply: " + (reply.error || "unknown error")));
      }
      log.scrollTop = log.scrollHeight;
      input.focus();
    });
  }

  var savedAgent = load(AGENT_KEY);
  if (savedAgent && agentSelect.querySelector('option[value="' + CSS.escape(savedAgent) + '"]')) agentSelect.value = savedAgent;
  loadThread();

  launcher.addEventListener("click", function () { setOpen(dock.hidden); });
  dock.querySelector("[data-chat-close]").addEventListener("click", function () { setOpen(false); launcher.focus(); });
  dock.querySelector("[data-chat-new]").addEventListener("click", function () {
    messages = [];
    save();
    render();
    if (input) input.focus();
  });
  agentSelect.addEventListener("change", function () {
    store(AGENT_KEY, agentSelect.value);
    loadThread();
  });
  dock.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { setOpen(false); launcher.focus(); }
  });
  if (form) {
    form.addEventListener("submit", function (event) { event.preventDefault(); send(input.value); });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(input.value); }
    });
  }
  if (load(OPEN_KEY) === "1") setOpen(true);
})();
