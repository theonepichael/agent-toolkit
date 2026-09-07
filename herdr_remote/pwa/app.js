// herdr remote — vanilla JS client. No build step, relative paths only (the
// app is served under a path prefix via tailscale serve on the Fedora box).
//
// Token handling (grill decision `pwa-token-handling`): pasted once, stored
// in localStorage, sent as an Authorization bearer header on every request —
// never in a URL. A 401 clears the stored token and re-shows the entry
// screen. SSE is consumed via the fetch API's ReadableStream (the browser's
// built-in event-source helper cannot set headers), and the token is never
// put in the stream URL where it would leak into logs.

const TOKEN_KEY = "herdr-remote-token";
const $ = (id) => document.getElementById(id);

let token = localStorage.getItem(TOKEN_KEY) || "";
let agents = [];

function headers() {
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

function showGate(message) {
  $("topbar").classList.add("hidden");
  $("agents").classList.add("hidden");
  $("detail").classList.add("hidden");
  $("token-gate").classList.remove("hidden");
  if (message) {
    $("token-error").textContent = message;
    $("token-error").classList.remove("hidden");
  }
}

function showList() {
  $("token-gate").classList.add("hidden");
  $("detail").classList.add("hidden");
  $("topbar").classList.remove("hidden");
  $("agents").classList.remove("hidden");
}

async function api(path, options = {}) {
  const resp = await fetch(path, { ...options, headers: headers() });
  if (resp.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    token = "";
    showGate("Token rejected — paste a fresh one.");
    throw new Error("unauthorized");
  }
  return resp;
}

function renderAgents() {
  const root = $("agents");
  root.innerHTML = "";
  for (const agent of agents) {
    const card = document.createElement("button");
    card.className = `card status-${agent.agent_status}`;
    card.innerHTML = `
      <span class="dot"></span>
      <span class="name">${escapeHtml(agent.name)}</span>
      <span class="status">${escapeHtml(agent.agent_status)}</span>
      <span class="meta">${escapeHtml(agent.title || agent.agent)}</span>`;
    card.onclick = () => openDetail(agent.name);
    root.appendChild(card);
  }
  if (!agents.length) root.innerHTML = "<p class='meta'>No agents right now.</p>";
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

async function openDetail(name) {
  showDetail(name);
  const resp = await api(`/api/agents/${encodeURIComponent(name)}/read`);
  if (!resp.ok) {
    $("detail-output").textContent = `read failed: ${resp.status}`;
    return;
  }
  const body = await resp.json();
  $("detail-output").textContent = body.output || "(no recent output)";
}

function showDetail(name) {
  $("agents").classList.add("hidden");
  $("detail-name").textContent = name;
  $("prompt-error").classList.add("hidden");
  $("detail").classList.remove("hidden");
}

async function sendPrompt(event) {
  event.preventDefault();
  const name = $("detail-name").textContent;
  const text = $("prompt-text").value.trim();
  if (!text) return;
  const resp = await api(`/api/agents/${encodeURIComponent(name)}/prompt`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
  if (resp.ok) {
    $("prompt-text").value = "";
    $("prompt-error").textContent = "Prompt sent — watch the status.";
    $("prompt-error").classList.remove("hidden");
  } else {
    const body = await resp.json().catch(() => ({}));
    $("prompt-error").textContent = body.error || `send failed: ${resp.status}`;
    $("prompt-error").classList.remove("hidden");
  }
}

function setConn(state) {
  $("conn-state").textContent = state;
  $("conn-state").className = `conn conn-${state === "live" ? "on" : "off"}`;
}

// Fetch-based SSE with auto-reconnect: on any stream end the client
// reconnects and re-fetches a fresh snapshot (the bridge re-sends initial
// state on every connect, so no offset tracking is needed).
async function listenEvents() {
  while (token) {
    try {
      const resp = await fetch("/api/events", { headers: headers() });
      if (resp.status === 401) {
        localStorage.removeItem(TOKEN_KEY);
        token = "";
        showGate("Token rejected — paste a fresh one.");
        return;
      }
      if (!resp.ok || !resp.body) throw new Error(`stream ${resp.status}`);
      setConn("live");
      await readStream(resp.body);
      setConn("reconnecting");
    } catch {
      setConn("reconnecting");
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }
}

async function readStream(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      handleEvent(block);
    }
  }
}

function handleEvent(block) {
  let name = "message";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (name !== "state" || !data) return;
  try {
    agents = JSON.parse(data).agents || [];
    renderAgents();
  } catch {
    /* malformed frame — next event resyncs us */
  }
}

async function init() {
  $("token-save").onclick = async () => {
    const candidate = $("token-input").value.trim();
    const resp = await fetch("/api/health", { headers: { Authorization: `Bearer ${candidate}` } });
    if (resp.status === 401) {
      showGate("Token rejected — check it and try again.");
      return;
    }
    token = candidate;
    localStorage.setItem(TOKEN_KEY, token);
    start();
  };
  $("back").onclick = showList;
  $("prompt-form").onsubmit = sendPrompt;
  if (!token) {
    showGate();
    return;
  }
  start();
}

async function start() {
  showList();
  // Snapshot first so the UI is useful even if the stream is momentarily down.
  const resp = await api("/api/agents");
  if (resp.ok) {
    agents = (await resp.json()).agents || [];
    renderAgents();
  }
  listenEvents();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
}

init();
