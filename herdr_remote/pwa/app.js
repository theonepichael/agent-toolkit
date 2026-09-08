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
let currentDetailId = null;
let prevStatus = new Map(); // agent id -> last rendered status (flash-on-change)
const STATUSES = ["working", "idle", "blocked", "done"];

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
  if (!agents.length) {
    // Empty state replaces the whole list; never mixed into the keyed diff.
    root.replaceChildren();
    const empty = document.createElement("p");
    empty.classList.add("empty");
    empty.textContent = "No agents right now.";
    root.appendChild(empty);
    prevStatus.clear();
    return;
  }
  const existing = new Map();
  for (const node of root.children) {
    if (node.dataset && node.dataset.id) existing.set(node.dataset.id, node);
  }
  const seen = new Set();
  for (const agent of agents) {
    seen.add(agent.id);
    let card = existing.get(agent.id);
    if (!card) {
      card = document.createElement("button");
      card.classList.add("card");
      card.dataset.id = agent.id;
      card.innerHTML = `
        <span class="dot" aria-hidden="true"></span>
        <span class="name"></span>
        <span class="pill"></span>
        <span class="meta"></span>`;
      card.querySelector(".name").textContent = agent.name;
      card.querySelector(".meta").textContent = agent.title || agent.agent;
      card.onclick = () => openDetail(agent.id);
    }
    for (const s of STATUSES) {
      card.classList.toggle(`status-${s}`, s === agent.agent_status);
    }
    card.querySelector(".pill").textContent = agent.agent_status;
    // Flash only on an actual status change (never on unchanged snapshot
    // refetches); restart the animation deterministically via reflow.
    if (prevStatus.has(agent.id) && prevStatus.get(agent.id) !== agent.agent_status) {
      card.classList.remove("flash");
      void card.offsetWidth;
      card.classList.add("flash");
    }
    prevStatus.set(agent.id, agent.agent_status);
    root.appendChild(card); // also keeps DOM order following payload order
  }
  for (const [id, node] of existing) {
    if (!seen.has(id)) {
      node.remove();
      prevStatus.delete(id);
    }
  }
}

async function openDetail(id) {
  const agent = agents.find((a) => a.id === id);
  showDetail(id, agent);
  const resp = await api(`/api/agents/${encodeURIComponent(id)}/read`);
  if (!resp.ok) {
    $("detail-output").textContent = `read failed: ${resp.status}`;
    return;
  }
  const body = await resp.json();
  $("detail-output").textContent = body.output || "(no recent output)";
}

function showDetail(id, agent) {
  currentDetailId = id;
  $("agents").classList.add("hidden");
  $("detail-name").textContent = agent ? agent.title || agent.name : id;
  $("prompt-error").classList.add("hidden");
  $("detail").classList.remove("hidden");
}

async function sendPrompt(event) {
  event.preventDefault();
  const id = currentDetailId;
  const text = $("prompt-text").value.trim();
  if (!text) return;
  const button = $("prompt-send");
  const error = $("prompt-error");
  button.disabled = true;
  button.classList.add("pending");
  try {
    const resp = await api(`/api/agents/${encodeURIComponent(id)}/prompt`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    if (resp.ok) {
      $("prompt-text").value = "";
      error.textContent = "Prompt sent — watch the status.";
      error.classList.add("ok");
    } else {
      const body = await resp.json().catch(() => ({}));
      error.textContent = body.error || `send failed: ${resp.status}`;
      error.classList.remove("ok");
    }
    error.classList.remove("hidden");
  } finally {
    // try…finally: a thrown network error must not leave the button stuck
    // pending; disabled during flight also blocks double-tap duplicates.
    button.disabled = false;
    button.classList.remove("pending");
  }
}

function setConn(state) {
  const pill = $("conn-state");
  pill.textContent = state;
  pill.classList.toggle("conn-on", state === "live");
  pill.classList.toggle("conn-wait", state === "connecting" || state === "reconnecting");
  pill.classList.toggle("conn-off", state === "off");
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
