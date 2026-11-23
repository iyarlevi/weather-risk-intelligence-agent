// Weather Risk Intelligence: chat UI.
// A plain client of the HTTP API: POST /chat for the agent, GET /rankings for the side panel.
// No framework or build step, so the server can serve this folder as-is.

const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");
const inputEl = $("input");
const sendBtn = $("send");

const EXAMPLES = [
  "Which hubs in the Midwest are most exposed to winter disruption?",
  "Compare Miami and Houston in terms of hurricane and flood exposure.",
  "What percentage of days in Denver last year had snowfall?",
  "Why is the Dallas hub's weather disruption risk high?",
  "How is the risk score calculated?",
];

// ------------------------------------------------------------------ session id
// Kept in sessionStorage so a page refresh continues the same conversation.
// Wrapped in try/catch: storage can be blocked (private mode), and the app must still work.
let sessionId = null;
try { sessionId = sessionStorage.getItem("wria_session"); } catch (_) {}
function setSession(id) {
  sessionId = id;
  try { id ? sessionStorage.setItem("wria_session", id) : sessionStorage.removeItem("wria_session"); } catch (_) {}
}

// ------------------------------------------------------------------ helpers
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Minimal, safe markdown: escape everything first, then allow **bold**, *italic* and line breaks.
function renderMarkdown(text) {
  const html = escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g, "$1<em>$2</em>");
  const div = el("div", { class: "answer-text" });
  div.innerHTML = html.split(/\n{2,}/).map((p) => `<p>${p.replace(/\n/g, "<br>")}</p>`).join("");
  return div;
}

function scrollToBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }
function hideWelcome() { const w = $("welcome"); if (w) w.remove(); }

// ------------------------------------------------------------------ rendering
function renderUser(text) {
  hideWelcome();
  messagesEl.append(el("div", { class: "msg user" }, text));
  scrollToBottom();
}

function renderAgent(resp, { restored = false } = {}) {
  const a = resp.answer;
  const card = el("div", { class: "msg agent" });
  card.append(renderMarkdown(a.answer));

  if (a.ranking.length) {
    card.append(
      el("table", { class: "result" },
        el("thead", {}, el("tr", {}, el("th", {}, "#"), el("th", {}, "Hub"), el("th", {}, "Score"), el("th", {}, "Main driver"))),
        el("tbody", {}, a.ranking.map((r) =>
          el("tr", {}, el("td", {}, r.rank), el("td", {}, r.name), el("td", { class: "num" }, r.score.toFixed(1)), el("td", {}, r.note))
        ))
      )
    );
  }

  // Key figures only when there's no ranking table (a ranking already shows its numbers).
  if (a.key_figures.length && !a.ranking.length) {
    card.append(el("div", { class: "figures" }, a.key_figures.slice(0, 8).map((f) =>
      el("div", { class: "figure", title: `from ${f.source_tool}` },
        el("span", { class: "v" }, `${f.value}${f.unit === "%" ? "%" : ""}`),
        f.unit !== "%" ? ` ${f.unit}` : "",
        el("span", { class: "l" }, f.label))
    )));
  }

  const list = (title, items) => items.length
    ? el("details", {}, el("summary", {}, `${title} (${items.length})`), el("ul", {}, items.map((x) => el("li", {}, x))))
    : null;
  card.append(
    list("Reasoning", a.reasoning),
    list("Assumptions", a.assumptions),
    list("Data sources", a.data_sources),
  );

  const tools = resp.tool_calls.map((t) => t.name).join(", ") || "none";
  card.append(el("div", { class: "answer-meta" },
    el("span", { class: `badge conf-${a.confidence}`, title: a.confidence_reason }, `confidence: ${a.confidence}`),
    el("span", { class: "badge", title: "Deterministic tools the agent called" }, `tools: ${tools}`),
    restored ? null : el("span", { class: "badge" }, `${(resp.latency_ms / 1000).toFixed(1)}s`),
  ));
  card.append(el("div", { class: "hint", style: "margin-top:4px" }, a.confidence_reason));

  // The server's grounding check: numbers the AI quoted that no tool returned.
  for (const w of resp.grounding_warnings) {
    card.append(el("div", { class: "warning" }, "⚠ Unverified: ", w));
  }

  if (a.follow_up_suggestions.length) {
    card.append(el("div", { class: "followups" }, a.follow_up_suggestions.map((q) =>
      el("button", { class: "chip", type: "button", onclick: () => ask(q) }, q))));
  }

  messagesEl.append(card);
  scrollToBottom();
  if (!restored) speak(a.answer);
}

function renderError(text) {
  messagesEl.append(el("div", { class: "msg agent error" }, "⚠ ", text));
  scrollToBottom();
}

// ------------------------------------------------------------------ chat
let busy = false;

async function ask(question) {
  question = question.trim();
  if (!question || busy) return;
  busy = true;
  sendBtn.disabled = true;
  inputEl.value = "";
  autoGrow();
  renderUser(question);

  // Show elapsed time: answers take 10-20 s because the agent calls tools first.
  const thinking = el("div", { class: "msg agent thinking" }, "Checking the data");
  messagesEl.append(thinking);
  scrollToBottom();
  const t0 = Date.now();
  const timer = setInterval(() => { thinking.textContent = `Checking the data (${Math.round((Date.now() - t0) / 1000)}s)`; }, 1000);

  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question, session_id: sessionId }),
    });
    const body = await res.json().catch(() => ({}));
    thinking.remove();
    if (!res.ok) {
      const detail = typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`;
      renderError(`The agent couldn't answer: ${detail}`);
    } else {
      setSession(body.session_id);
      renderAgent(body);
    }
  } catch (err) {
    thinking.remove();
    renderError(`Can't reach the server (${err.message}). Is it running?`);
  } finally {
    clearInterval(timer);
    busy = false;
    sendBtn.disabled = false;
    inputEl.focus();
  }
}

$("chat-form").addEventListener("submit", (e) => { e.preventDefault(); ask(inputEl.value); });
inputEl.addEventListener("keydown", (e) => {
  // Enter sends; Shift+Enter adds a new line.
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(inputEl.value); }
});
function autoGrow() { inputEl.style.height = "auto"; inputEl.style.height = `${inputEl.scrollHeight}px`; }
inputEl.addEventListener("input", autoGrow);

$("new-chat").addEventListener("click", async () => {
  if (sessionId) fetch(`/chat/${encodeURIComponent(sessionId)}`, { method: "DELETE" }).catch(() => {});
  setSession(null);
  messagesEl.replaceChildren(welcomeNode);
  inputEl.focus();
});

// Example questions on the welcome screen.
const welcomeNode = $("welcome");
$("examples").append(...EXAMPLES.map((q) => el("button", { class: "chip", type: "button", onclick: () => ask(q) }, q)));

// ------------------------------------------------------------------ ranking panel
const HAZARD_LABELS = {
  composite: "overall", winter: "winter", hurricane_wind: "hurricane & wind",
  flood: "flood", severe_storm: "tornado & hail", heat: "heat",
};

async function loadRanking() {
  const hazard = $("rank-hazard").value;
  const region = $("rank-region").value;
  const params = new URLSearchParams({ hazard });
  if (region) params.set("region", region);
  const list = $("ranking-list");
  try {
    const res = await fetch(`/rankings?${params}`);
    const rows = await res.json();
    list.replaceChildren(...rows.map((r) => el("li", {
      title: `${r.tier} · main driver: ${r.top_driver}`,
      // Clicking a hub asks the agent to explain it (follows the selected hazard).
      onclick: () => ask(hazard === "composite"
        ? `Why does the ${r.name} hub have this overall weather risk score?`
        : `Explain the ${r.name} hub's ${HAZARD_LABELS[hazard]} risk score.`),
    },
      el("span", { class: "rank-no" }, r.rank),
      el("div", {},
        el("div", { class: "rank-name" }, r.name, " ", el("small", {}, r.region)),
        el("div", { class: "bar" }, el("span", { class: `bg-${r.tier}`, style: `width:${r.score}%` }))),
      el("span", { class: `score-pill tier-${r.tier}` }, r.score.toFixed(1)),
    )));
  } catch (err) {
    list.replaceChildren(el("li", {}, `Couldn't load ranking: ${err.message}`));
  }
}
$("rank-hazard").addEventListener("change", loadRanking);
$("rank-region").addEventListener("change", loadRanking);

async function loadStatus() {
  try {
    const h = await (await fetch("/health")).json();
    $("data-status").textContent =
      `Data: ${h.history_window.replace("..", " to ")} · ${h.hubs} hubs` +
      (h.llm_configured ? "" : " · ⚠ no API key configured");
  } catch (_) {}
}

// ------------------------------------------------------------------ voice (bonus)
// Speech-to-text uses the browser's Web Speech API (Chrome, Edge, Safari). No server changes.
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (Recognition) {
  const mic = $("mic");
  mic.hidden = false;
  const rec = new Recognition();
  rec.lang = "en-US";
  rec.interimResults = true;
  let listening = false;
  rec.onresult = (e) => {
    inputEl.value = Array.from(e.results).map((r) => r[0].transcript).join("");
    autoGrow();
    // Send automatically when the browser marks the phrase as final.
    if (e.results[e.results.length - 1].isFinal) ask(inputEl.value);
  };
  rec.onend = () => { listening = false; mic.classList.remove("listening"); };
  rec.onerror = () => { listening = false; mic.classList.remove("listening"); };
  mic.addEventListener("click", () => {
    if (listening) { rec.stop(); return; }
    listening = true;
    mic.classList.add("listening");
    rec.start();
  });
}

// Text-to-speech for answers, when "Read aloud" is ticked.
function speak(text) {
  if (!$("speak-toggle").checked || !window.speechSynthesis) return;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(new SpeechSynthesisUtterance(text.replace(/\*+/g, "")));
}

// After a refresh, redraw the stored conversation so what's on screen matches what the
// agent remembers. Unknown or expired sessions simply start fresh.
async function restoreHistory() {
  if (!sessionId) return;
  try {
    const res = await fetch(`/chat/${encodeURIComponent(sessionId)}`);
    const turns = res.ok ? await res.json() : [];
    if (!turns.length) { setSession(null); return; }
    for (const t of turns) {
      renderUser(t.question);
      renderAgent({
        answer: t.answer,
        tool_calls: t.tools.map((name) => ({ name })),
        grounding_warnings: [],
        latency_ms: 0,
      }, { restored: true });
    }
  } catch (_) {}
}

// ------------------------------------------------------------------ start
loadRanking();
loadStatus();
restoreHistory();
inputEl.focus();
