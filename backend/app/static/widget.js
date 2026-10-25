/*
 * Embeddable chat widget. No dependencies, one <script> tag:
 *
 *   <script src="https://YOUR-SERVER/widget.js" data-key="pk_..." async></script>
 *
 * Options (all optional):
 *   data-agent="hr"             informational; the key decides which assistant answers
 *   data-container="#chat"      render inline inside that element instead of a floating bubble
 *   data-position="left"        floating bubble on the left (default: right)
 *   data-open="true"            start opened
 *
 * data-key is a *publishable* embed key from Admin > Website embed. It can only chat, and only from the websites
 * listed on the key. Never put a user API key or session token in a web page.
 * The widget lives in a Shadow DOM, so your site's CSS cannot break it and it cannot break your site.
 * All text is inserted with textContent, never as HTML.
 */
(function () {
  "use strict";
  var script = document.currentScript;
  if (!script) return;
  var key = script.getAttribute("data-key");
  if (!key) { console.error("[chat-widget] missing data-key attribute"); return; }
  var base = new URL(script.src, location.href).origin;
  var containerSel = script.getAttribute("data-container");
  var inline = !!containerSel;
  var left = script.getAttribute("data-position") === "left";

  // ---- identity: an anonymous, stable visitor id kept in the visitor's own browser
  function randomId() {
    var a = new Uint8Array(12);
    (window.crypto || window.msCrypto).getRandomValues(a);
    return Array.prototype.map.call(a, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
  }
  var visitor = null;
  try { visitor = localStorage.getItem("rag-visitor"); } catch (e) { /* storage blocked */ }
  if (!visitor || !/^[A-Za-z0-9_-]{8,64}$/.test(visitor)) {
    visitor = randomId();
    try { localStorage.setItem("rag-visitor", visitor); } catch (e) { /* stays for this page view */ }
  }
  var storeKey = "rag-widget-session:" + key.slice(0, 12);
  var sessionId = null;
  try { sessionStorage.getItem(storeKey) && (sessionId = sessionStorage.getItem(storeKey)); } catch (e) { /* ignore */ }

  function headers() {
    return { "Content-Type": "application/json", "X-Embed-Key": key, "X-Visitor-Id": visitor };
  }
  function errText(res) {
    return res.json().then(function (e) { return (e.error && e.error.message) || ("Request failed (" + res.status + ")"); },
      function () { return "Request failed (" + res.status + ")"; });
  }

  // ---- DOM (inside a shadow root)
  var host = document.createElement("div");
  host.setAttribute("data-rag-widget", "");
  var root = host.attachShadow({ mode: "open" });
  var color = "#4f46e5";
  var css = document.createElement("style");
  function applyStyle() {
    css.textContent = [
      ":host{all:initial}",
      "*{box-sizing:border-box;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}",
      ".wrap{" + (inline ? "width:100%;height:100%;min-height:380px;" :
        "position:fixed;bottom:20px;" + (left ? "left" : "right") + ":20px;z-index:2147483000;") + "font-size:14px;line-height:1.45;color:#0f172a}",
      ".panel{display:" + (inline ? "flex" : "none") + ";flex-direction:column;" +
        (inline ? "height:100%;" : "width:360px;max-width:calc(100vw - 24px);height:520px;max-height:calc(100vh - 100px);margin-bottom:12px;") +
        "background:#fff;border:1px solid #e2e8f0;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.2);overflow:hidden}",
      ".panel.open{display:flex}",
      ".head{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;background:" + color + ";color:#fff;font-weight:600}",
      ".x{background:none;border:0;color:#fff;font-size:20px;line-height:1;cursor:pointer;padding:0 4px}",
      ".log{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px;background:#f8fafc}",
      ".b{max-width:86%;padding:8px 11px;border-radius:12px;white-space:pre-wrap;word-break:break-word;background:#fff;border:1px solid #e2e8f0}",
      ".b.user{align-self:flex-end;background:" + color + ";color:#fff;border-color:transparent}",
      ".b.bot,.b.human{align-self:flex-start}.b.human{background:#dcfce7;border-color:#bbf7d0;color:#14532d}",
      ".src{align-self:flex-start;font-size:11px;color:#64748b}",
      ".banner{padding:6px 12px;background:#fef3c7;color:#92400e;font-size:12px}.banner[hidden]{display:none}",
      ".card{align-self:flex-start;max-width:86%;padding:8px 11px;border-radius:12px;background:#fffbeb;border:1px solid #fcd34d;color:#78350f;font-size:13px}",
      ".card .t{font-weight:600;margin-bottom:4px}.card .row{display:flex;gap:6px;margin-top:6px}",
      "form{display:flex;gap:6px;padding:10px;border-top:1px solid #e2e8f0;background:#fff}",
      "input{flex:1;min-width:0;padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;font:inherit;color:inherit;background:#fff}",
      "input:focus-visible,button:focus-visible{outline:2px solid " + color + ";outline-offset:1px}",
      "button.p{padding:8px 12px;border:0;border-radius:8px;background:" + color + ";color:#fff;cursor:pointer;font:inherit}",
      "button.p:disabled{opacity:.5;cursor:not-allowed}",
      "button.s{padding:5px 10px;border:1px solid #d6d3d1;border-radius:6px;background:#fff;cursor:pointer;font:inherit;color:#0f172a}",
      ".human-btn{background:#fff;border:0;color:" + color + ";cursor:pointer;font-size:12px;padding:0 12px 8px;text-align:left}",
      ".bubble{width:56px;height:56px;border-radius:50%;border:0;background:" + color + ";color:#fff;font-size:24px;cursor:pointer;" +
        "box-shadow:0 6px 20px rgba(0,0,0,.25);display:block;margin-" + (left ? "right" : "left") + ":auto}",
    ].join("\n");
  }
  applyStyle();

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  var wrap = el("div", "wrap");
  var panel = el("div", "panel");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Chat");
  var head = el("div", "head");
  var titleEl = el("span", "", "Ask our assistant");
  var closeBtn = el("button", "x", "×");
  closeBtn.setAttribute("aria-label", "Close chat");
  head.appendChild(titleEl);
  if (!inline) head.appendChild(closeBtn);
  var log = el("div", "log");
  log.setAttribute("aria-live", "polite");
  var banner = el("div", "banner");
  banner.hidden = true;
  var form = el("form");
  var input = el("input");
  input.placeholder = "Type your question…";
  input.setAttribute("aria-label", "Message");
  input.maxLength = 2000;
  var send = el("button", "p", "Send");
  send.type = "submit";
  var human = el("button", "human-btn", "Talk to a human");
  human.type = "button";
  form.appendChild(input);
  form.appendChild(send);
  [head, log, banner, form, human].forEach(function (n) { panel.appendChild(n); });
  wrap.appendChild(panel);
  var bubbleBtn = null;
  if (!inline) {
    bubbleBtn = el("button", "bubble", "💬");
    bubbleBtn.setAttribute("aria-label", "Open chat");
    wrap.appendChild(bubbleBtn);
  }
  root.appendChild(css);
  root.appendChild(wrap);

  function mount() {
    var target = inline ? document.querySelector(containerSel) : document.body;
    if (!target) { console.error("[chat-widget] container not found: " + containerSel); return; }
    target.appendChild(host);
    if (inline) host.style.cssText = "display:block;width:100%;height:100%";
  }
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);

  // ---- behaviour
  var busy = false, pollTimer = null;

  function bubble(role, text) {
    var b = el("div", "b " + (role === "user" ? "user" : role === "human" ? "human" : "bot"), text);
    log.appendChild(b);
    log.scrollTop = log.scrollHeight;
    return b;
  }
  function note(text) { var s = el("div", "src", text); log.appendChild(s); log.scrollTop = log.scrollHeight; }

  function setBanner(status) {
    banner.hidden = !status;
    banner.textContent = status === "waiting" ? "Waiting for a human agent…"
      : status === "active" ? "You're chatting with a human agent." : "";
    human.hidden = !!status;
    if (status && !pollTimer) pollTimer = setInterval(refresh, 2500);
    if (!status && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function render(items) {
    log.textContent = "";
    bubble("bot", welcome);
    items.forEach(function (m) { bubble(m.role, m.content); });
  }
  function refresh() {
    if (!sessionId || busy) return;
    fetch(base + "/api/sessions/" + sessionId + "/messages", { headers: headers() })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) { render(d.items); setBanner(d.handoff_status); } })
      .catch(function () { /* transient */ });
  }

  // An action the assistant prepared (leave request, ticket, ...) only runs after the visitor presses Confirm.
  function actionCard(result) {
    var card = el("div", "card");
    card.appendChild(el("div", "t", "Please confirm this action"));
    card.appendChild(el("div", "", result.summary || "Action"));
    var row = el("div", "row");
    var yes = el("button", "p", "Confirm");
    var no = el("button", "s", "Cancel");
    function decide(kind) {
      yes.disabled = no.disabled = true;
      fetch(base + "/api/actions/" + result.action_id + "/" + kind, { method: "POST", headers: headers(), body: "{}" })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (r) {
          card.textContent = r.ok ? ((r.d.message && r.d.message.content) || "Done.") :
            ((r.d.error && r.d.error.message) || "Could not complete that.");
        })
        .catch(function () { card.textContent = "Could not reach the server."; });
    }
    yes.addEventListener("click", function () { decide("confirm"); });
    no.addEventListener("click", function () { decide("reject"); });
    row.appendChild(yes);
    row.appendChild(no);
    card.appendChild(row);
    log.appendChild(card);
    log.scrollTop = log.scrollHeight;
  }

  function ask(question) {
    busy = true;
    send.disabled = true;
    bubble("user", question);
    var out = bubble("bot", "…");
    var citations = [], text = "";
    fetch(base + "/api/chat/stream", {
      method: "POST", headers: headers(), body: JSON.stringify({ question: question, session_id: sessionId })
    }).then(function (res) {
      if (!res.ok || !res.body) return errText(res).then(function (m) { throw new Error(m); });
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = "";
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) return;
          buf += dec.decode(r.value, { stream: true });
          var i;
          while ((i = buf.indexOf("\n\n")) !== -1) {
            var block = buf.slice(0, i); buf = buf.slice(i + 2);
            var ev = /^event: (.+)$/m.exec(block), data = /^data: (.+)$/m.exec(block);
            if (!ev || !data) continue;
            var d = JSON.parse(data[1]);
            if (ev[1] === "session") {
              sessionId = d.session_id;
              try { sessionStorage.setItem(storeKey, sessionId); } catch (e) { /* ignore */ }
            } else if (ev[1] === "sources") citations = d.citations || [];
            else if (ev[1] === "token") { text += d.text; out.textContent = text; log.scrollTop = log.scrollHeight; }
            else if (ev[1] === "tool") { if (d.result && d.result.status === "awaiting_user_confirmation") actionCard(d.result); }
            else if (ev[1] === "handoff") setBanner(d.status);
            else if (ev[1] === "error") out.textContent = d.message;
            else if (ev[1] === "done") {
              if (d.message.role === "user") out.remove();
              var names = {};
              citations.filter(function (c) { return c.cited; }).forEach(function (c) { names[c.filename + (c.page ? " p." + c.page : "")] = 1; });
              if (Object.keys(names).length) note("Sources: " + Object.keys(names).join(", "));
            }
          }
          return pump();
        });
      }
      return pump();
    }).catch(function (e) { out.textContent = "Sorry, something went wrong: " + e.message; })
      .then(function () { busy = false; send.disabled = false; });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var q = input.value.trim();
    if (!q || busy) return;
    input.value = "";
    ask(q);
  });
  human.addEventListener("click", function () {
    function go() {
      fetch(base + "/api/sessions/" + sessionId + "/handoff", { method: "POST", headers: headers(), body: "{}" })
        .then(function (r) { if (r.ok) { setBanner("waiting"); refresh(); } else return errText(r).then(note); });
    }
    if (sessionId) return go();
    fetch(base + "/api/sessions", { method: "POST", headers: headers(), body: "{}" })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        sessionId = s.id;
        try { sessionStorage.setItem(storeKey, sessionId); } catch (e) { /* ignore */ }
        go();
      });
  });

  function open(show) {
    panel.classList.toggle("open", show);
    if (bubbleBtn) bubbleBtn.setAttribute("aria-label", show ? "Close chat" : "Open chat");
    if (show) { input.focus(); refresh(); }
  }
  if (bubbleBtn) bubbleBtn.addEventListener("click", function () { open(!panel.classList.contains("open")); });
  closeBtn.addEventListener("click", function () { open(false); });

  // ---- look & wording come from the server (so admins can change them without touching the website)
  var welcome = "Hi! How can I help?";
  fetch(base + "/api/embed/config", { headers: headers() })
    .then(function (r) { return r.ok ? r.json() : errText(r).then(function (m) { throw new Error(m); }); })
    .then(function (c) {
      titleEl.textContent = c.title;
      welcome = c.welcome;
      if (/^#[0-9a-fA-F]{6}$/.test(c.color)) { color = c.color; applyStyle(); }
      bubble("bot", welcome);
    })
    .catch(function (e) {
      bubble("bot", "The chat is unavailable right now: " + e.message);
      send.disabled = true;
      input.disabled = true;
      human.hidden = true;
    });

  if (inline || script.getAttribute("data-open") === "true") open(true);
})();
