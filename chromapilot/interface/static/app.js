/* ChromaPilot interface — front-end logic.
 *
 * Responsibilities:
 *   1. Boot: poll /api/health until the agent has imported, then /api/bootstrap
 *      to render the static panels (agent graph, tool catalog, pipeline DAG).
 *   2. Run: POST /api/run, then consume the per-run Server-Sent-Events stream
 *      and translate each event into a DOM update.
 *   3. Interrupts: when the graph pauses for human input, show the matching
 *      inline form and POST the answer to /api/resume (or /api/stop).
 *
 * The event protocol is a flat {type, ...payload} envelope produced by
 * run_session.py; dispatch() is the single switch over event types.
 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
const esc = (s) => (s == null ? "" : String(s));

/* Small inline icon set (stroke icons, currentColor). */
const ICONS = {
  map: '<svg viewBox="0 0 16 16"><circle cx="3" cy="8" r="2"/><circle cx="13" cy="4" r="2"/><circle cx="13" cy="12" r="2"/><path d="M5 7.3l6-2.6M5 8.7l6 2.6"/></svg>',
  agent: '<svg viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" rx="2.2"/><path d="M6 7h4M6 10h2.5"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2"/></svg>',
  book: '<svg viewBox="0 0 16 16"><path d="M3 3.5A1.5 1.5 0 0 1 4.5 2H13v11H4.5A1.5 1.5 0 0 0 3 14.5z"/><path d="M3 12.5A1.5 1.5 0 0 1 4.5 11H13"/></svg>',
  activity: '<svg viewBox="0 0 16 16"><path d="M1.5 8h3l2-5 3 10 2-5h3"/></svg>',
  plan: '<svg viewBox="0 0 16 16"><path d="M2.5 4.5l1.5 1.5 2.5-3M2.5 10.5l1.5 1.5 2.5-3"/><path d="M9 4.5h4.5M9 10.5h4.5"/></svg>',
  image: '<svg viewBox="0 0 16 16"><rect x="2" y="3" width="12" height="10" rx="1.6"/><circle cx="5.7" cy="6.5" r="1.2"/><path d="M14 11l-3.5-3.5L5 13"/></svg>',
  check: '<svg viewBox="0 0 16 16"><path d="M3 8.5l3 3 7-7"/></svg>',
  copy: '<svg viewBox="0 0 16 16"><rect x="6" y="6" width="7.5" height="7.5" rx="1.4"/><path d="M4 10.5V4a1.5 1.5 0 0 1 1.5-1.5H11"/></svg>',
  play: '<svg viewBox="0 0 16 16"><path d="M5 3.2v9.6L12.5 8z"/></svg>',
  stop: '<svg viewBox="0 0 16 16"><rect x="4" y="4" width="8" height="8" rx="1.5"/></svg>',
  edit: '<svg viewBox="0 0 16 16"><path d="M11.5 2.5l2 2L6 12H4v-2z"/><path d="M3 14h10"/></svg>',
  warn: '<svg viewBox="0 0 16 16"><path d="M8 2l6.5 11.5h-13z"/><path d="M8 6.5v3.5M8 12.2v.3"/></svg>',
  report: '<svg viewBox="0 0 16 16"><path d="M4 2h5.5L13 5.5V14H4z"/><path d="M9.5 2v3.5H13M6.5 8.5h3.5M6.5 11h3.5"/></svg>',
};
const icon = (name, cls) => {
  const i = el("i", "ic" + (cls ? " " + cls : ""));
  i.innerHTML = ICONS[name] || "";
  return i;
};

// Human-readable labels for the chat roles streamed in `message` events.
const ROLE_LABELS = { ai: "agent", human: "you", tool: "tool", system: "system" };

// Friendly phase text per agent-graph node, shown in the live status bar.
const PHASE_LABELS = {
  planner: "Planning the analysis…",
  extract_input_state: "Reading your inputs…",
  user_feedback_node: "Waiting for your review",
  validator: "Validating the plan…",
  executor: "Preparing the next step…",
  tools: "Running a pipeline tool…",
  tools_checker: "Checking the tool result…",
  auto_code_generator: "Writing custom analysis code…",
  replayer: "Assembling the results…",
  report_generator: "Writing the report…",
};

// Example requests for the landing screen (paths are placeholders).
const EXAMPLES = [
  { label: "FASTQ → aligned BAM (Hiplex)",
    text: "We have raw paired-end FASTQ files generated from Hiplex Cut&Tag experiments, located at:\n\n/path/to/fastq\n\nThe barcode configuration file for the experiment is located at:\n\n/path/to/barcode/barcode.csv\n\nThe goal is to process these data and obtain aligned BAM files using hg38 as the reference genome. The dataset contains four samples: C1, C2, T1, and T2.\n\nAll intermediate files and final results should be written to the following output directory:\n\n/path/to/output" },
  { label: "Coverage tracks (bigWig)",
    text: "We have BAM files from a Hiplex Cut&Tag experiment located at:\n\n/path/to/bam\n\nGenerate RPM-normalised genome coverage tracks in bigWig format using hg38 for samples C1 and C2.\n\nAll intermediate files and final results should be written to the following output directory:\n\n/path/to/output" },
  { label: "Peaks + differential (C vs T)",
    text: "We have BAM files generated from Hiplex Cut&Tag experiments, located at:\n\n/path/to/bam\n\nThe goal is to identify peak regions for each CRF pair using hg38 as the reference genome, perform pathway enrichment on the peaks, and conduct differential peak analysis between the C group (C1, C2) and the T group (T1, T2).\n\nThe dataset contains four samples: C1, C2, T1, and T2.\n\nAll intermediate files and final results should be written to the following output directory:\n\n/path/to/output" },
  { label: "Biclustering C1/C2 → differential",
    text: "We have raw paired-end FASTQ files from Hiplex Cut&Tag experiments at:\n\n/path/to/fastq\n\nwith the barcode file at:\n\n/path/to/barcode/barcode.csv\n\nUsing hg38, perform biclustering jointly on C1 and C2 to identify CRF-pair clusters and localisation patterns, then use those patterns to compare the C group (C1, C2) with the T group (T1, T2) and identify differential regions.\n\nAll intermediate files and final results should be written to the following output directory:\n\n/path/to/output" },
];

/* The run in progress is remembered per browser so that reloading the page —
   or reopening it after the SSH tunnel dropped — re-attaches to it: the server
   keeps every event and replays them (see /api/stream). */
function saveRun(obj) { try { obj ? localStorage.setItem("mea.run", JSON.stringify(obj)) : localStorage.removeItem("mea.run"); } catch (_) {} }
function loadRun() { try { return JSON.parse(localStorage.getItem("mea.run") || "null"); } catch (_) { return null; } }

const STATE = {
  bootstrap: null,
  runId: null,
  es: null,
  steps: [],
  dagNodes: {},   // tool name -> <g> element
  running: false,
  t0: 0,
  timer: null,
  entries: 0,
  activeSub: {},  // parent node -> active sub-step name
};

/* ------------------------------------------------------------- status bar */
function rb(state, phase, sub) {
  const bar = $("#runbar");
  bar.classList.remove("hidden");
  if (state) bar.className = "runbar " + state;
  if (phase != null) {
    const p = $("#rb-phase");
    p.textContent = phase;
    if (sub) p.appendChild(el("span", "sub", sub));
  }
}
function rbTicker(text) { $("#rb-ticker").textContent = text || ""; }
function rbStartClock(t0) {
  STATE.t0 = t0 || Date.now();      // t0: resume an existing run's clock
  rbTick();
  clearInterval(STATE.timer);
  STATE.timer = setInterval(rbTick, 1000);
}
function rbStopClock() { clearInterval(STATE.timer); STATE.timer = null; }
function fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
}
function rbTick() { $("#rb-elapsed").textContent = fmtElapsed(Date.now() - STATE.t0); }
let CURRENT_TS = null;   // server timestamp of the event being dispatched
const fmtTime = () => (CURRENT_TS ? new Date(CURRENT_TS * 1000) : new Date()).toLocaleTimeString([], { hour12: false });
function fmtSize(b) {
  if (b == null) return "";
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
  if (b < 1073741824) return (b / 1048576).toFixed(1) + " MB";
  return (b / 1073741824).toFixed(2) + " GB";
}

/* ====================================================================== boot */
async function boot() {
  try {
    const h = await fetch("/api/health").then((r) => r.json());
    setBootPill(h.status, h.error);
    if (h.status === "ready") return loadBootstrap();
    if (h.status === "error") return showBootError(h.error);
  } catch (_) {
    setBootPill("booting");
  }
  setTimeout(boot, 1500);
}

function showBootError(msg) {
  $("#boot-error-msg").textContent = msg || "(see the server terminal for details)";
  $("#boot-error").hidden = false;
  $("#run-btn").disabled = true;
}

function setBootPill(status, error) {
  const pill = $("#boot-pill"), dot = $("#boot-dot"), txt = $("#boot-text");
  const cls = status === "ready" ? "ready" : status === "error" ? "error" : "booting";
  dot.className = "dot " + cls;
  pill.className = "pill " + (cls === "booting" ? "" : cls);
  txt.textContent = status === "ready" ? "agent ready"
    : status === "error" ? "boot failed" : "importing agent…";
  if (error) txt.title = error;
}

async function loadBootstrap() {
  const b = await fetch("/api/bootstrap").then((r) => r.json());
  STATE.bootstrap = b;
  STATE.toolsByName = {};
  (b.tools || []).forEach((t) => { STATE.toolsByName[t.name] = t; });
  $("#run-btn").disabled = false;       // agent is ready — allow runs
  renderModels(b.providers, b.active, b.model);
  renderAgentGraph(b.control_nodes, b.sub_nodes || {});
  renderToolCatalog(b.tools);
  renderDAG(b.dag);
  renderProtoLegend(b.dag.protocols || []);
  STATE.reportEnabled = !!b.report_enabled;
  if (!b.report_enabled) {
    // report stage is disabled in this config — note it on the reporter node.
    const g = $('.gnode[data-name="report_generator"]');
    if (g) { g.classList.add("off"); g.querySelector(".gstate").textContent = "off in config"; g.title = "report_enabled is false in config.yaml"; }
  }
  restoreRun();
}

async function restoreRun() {
  const saved = loadRun();
  if (!saved || !saved.id) return;
  let st;
  try { st = await fetch("/api/run_status?run_id=" + saved.id).then((r) => r.json()); } catch (_) { return; }
  if (!st || !st.exists) { saveRun(null); return; }      // server restarted — nothing to resume
  resetRunUI();
  STATE.runId = saved.id;
  STATE.lastRequest = saved.request || st.request || "";
  STATE.startedAt = saved.t0 ? new Date(saved.t0) : new Date(st.created * 1000);
  STATE.t0 = saved.t0 || Date.now();
  STATE.running = !st.finished;
  $("#pb-text").textContent = STATE.lastRequest; $("#pb-text").title = STATE.lastRequest;
  setOutputDir(st.output_dir || saved.outdir || null);
  showWorkspace();
  $("#run-btn").disabled = STATE.running;
  $("#stop-btn").disabled = !STATE.running;
  rb("working", st.finished ? "Restoring the finished run…" : "Re-attaching to the run…");
  rbTicker(`replaying ${st.n_events} event(s)`);
  if (STATE.running) rbStartClock(STATE.t0); else $("#rb-elapsed").textContent = "";
  openStream(saved.id);
}

/* ------------------------------------------------- model / provider picker */
function renderModels(providers, active, model) {
  providers = providers || [];
  const sel = $("#model-select");
  sel.innerHTML = "";
  providers.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.id; o.textContent = p.label;
    if (p.id === active) o.selected = true;
    sel.appendChild(o);
  });
  const cur = providers.find((p) => p.id === active);
  setModelPill(cur ? cur.label : model);
  // Only show the picker when there's an actual choice (≥2 providers configured).
  $("#modelsel-wrap").hidden = providers.length < 2;
  STATE.providers = providers;
  sel.onchange = () => {
    const p = providers.find((x) => x.id === sel.value);
    if (p) setModelPill(p.label);
  };
}
function setModelPill(label) {
  const m = $("#model-name");
  m.innerHTML = "";
  const parts = String(label || "model").split(" — ");
  if (parts.length === 2) {
    m.appendChild(document.createTextNode(parts[0] + " "));
    m.appendChild(el("code", null, parts[1]));
  } else {
    m.textContent = label || "model";
  }
}

/* ------------------------------------------------------ static: agent graph */
function renderAgentGraph(nodes, subNodes) {
  const host = $("#agent-graph");
  host.innerHTML = "";
  nodes.forEach((n) => {
    const g = el("div", "gnode idle");
    g.dataset.name = n.name;
    g.dataset.role = n.role;
    g.appendChild(el("span", "gdot"));
    g.appendChild(el("span", "glabel", n.label));
    g.appendChild(el("span", "gstate", n.role));
    const subs = subNodes[n.name];
    if (subs && subs.length) {
      const wrap = el("div", "gsubs");
      subs.forEach((s) => {
        const c = el("span", "gsub", s.label);
        c.dataset.name = s.name;
        wrap.appendChild(c);
      });
      g.appendChild(wrap);
      g.title = "sub-graph: " + subs.map((s) => s.label).join(" → ");
    }
    host.appendChild(g);
  });
}

function markNode(name, state) {
  const g = $(`.gnode[data-name="${name}"]`);
  if (!g) return;
  g.classList.remove("idle", "active", "done");
  g.classList.add(state);
  const st = g.querySelector(".gstate");
  if (st && !g.classList.contains("off")) {
    st.textContent = state === "active" ? "running" : state === "done" ? "done" : g.dataset.role;
  }
  if (state === "done") {
    g.querySelectorAll(".gsub.active").forEach((c) => { c.classList.remove("active"); c.classList.add("done"); });
  }
}

function markSubnode(parent, name) {
  const g = $(`.gnode[data-name="${parent}"]`);
  if (!g) return;
  if (!g.classList.contains("active")) markNode(parent, "active");   // a sub-step implies its parent is running
  const subs = g.querySelector(".gsubs");
  if (subs) subs.classList.add("touched");
  g.querySelectorAll(".gsub.active").forEach((c) => { c.classList.remove("active"); c.classList.add("done"); });
  const c = g.querySelector(`.gsub[data-name="${name}"]`);
  if (c) { c.classList.remove("done"); c.classList.add("active"); }
  STATE.activeSub[parent] = name;
}

/* ------------------------------------------------------ static: tool catalog */
function renderToolCatalog(tools) {
  const host = $("#tool-catalog");
  host.innerHTML = "";
  $("#tool-count").textContent = (tools || []).length + " tools";
  (tools || []).forEach((t) => {
    const w = el("div", "tool");
    w.dataset.name = t.name;
    const head = el("div", "thead");
    head.appendChild(el("span", "tnum", t.index + "."));
    head.appendChild(el("span", "tname", t.name));
    if (t.per_sample) { const tag = el("span", "tag", "per sample"); tag.title = "run once per sample"; head.appendChild(tag); }
    head.appendChild(el("span", "chev", "▸"));
    w.appendChild(head);
    w.appendChild(el("div", "tfn", t.summary || t.function || ""));

    const body = el("div", "tbody");
    if (t.function) body.appendChild(el("p", null, t.function));
    if (t.note) body.appendChild(el("p", null, t.note));
    if (t.params && t.params.length) {
      body.appendChild(el("h4", null, "Inputs"));
      const ul = el("ul", "params");
      t.params.forEach((p) => {
        const li = el("li");
        li.appendChild(el("code", null, p.name));
        li.appendChild(el("span", null, p.desc || ""));
        ul.appendChild(li);
      });
      body.appendChild(ul);
    }
    if (t.outputs) { body.appendChild(el("h4", null, "Outputs")); body.appendChild(el("pre", null, t.outputs)); }
    if (t.path_convention) { body.appendChild(el("h4", null, "Writes to (relative to output dir)")); body.appendChild(el("pre", null, t.path_convention)); }
    w.appendChild(body);
    head.onclick = () => w.classList.toggle("open");
    host.appendChild(w);
  });
}

function openTool(name) {
  const head = $('.panel-head[data-target="tool-catalog"]');
  const cat = $("#tool-catalog");
  if (cat.classList.contains("collapsed")) { cat.classList.remove("collapsed"); head.classList.add("open"); }
  const w = $(`.tool[data-name="${name}"]`);
  if (!w) return;
  w.classList.add("open", "flash");
  setTimeout(() => w.classList.remove("flash"), 1200);
  w.scrollIntoView({ behavior: "smooth", block: "center" });
}

/* ------------------------------------------------------ static: pipeline DAG */
const NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const e = document.createElementNS(NS, tag);
  for (const k in (attrs || {})) e.setAttribute(k, attrs[k]);
  return e;
}

function renderDAG(dag) {
  const svg = $("#dag");
  svg.innerHTML = "";
  STATE.dagNodes = {};
  const layout = dag.layout || {};
  const protoIndex = {};
  (dag.protocols || []).forEach((p, i) => { protoIndex[p] = i; });
  const W = 150, H = 42, GX = 46, GY = 14, PAD = 10;
  let maxLayer = 0, maxRow = 0;
  for (const k in layout) {
    maxLayer = Math.max(maxLayer, layout[k].layer);
    maxRow = Math.max(maxRow, layout[k].row);
  }
  const xOf = (l) => PAD + l * (W + GX);
  const yOf = (r) => PAD + r * (H + GY);
  const width = xOf(maxLayer) + W + PAD;
  const height = yOf(maxRow) + H + PAD;
  // viewBox only (no fixed width/height attrs) so CSS scales it to fit the
  // panel width — the whole pipeline is always visible, no horizontal scroll.
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  // arrowheads
  const defs = svgEl("defs");
  [["arr", "#cbd3df"], ["arr-lit", "#4f46e5"]].forEach(([id, fill]) => {
    const m = svgEl("marker", { id, markerWidth: 8, markerHeight: 8, refX: 7, refY: 4, orient: "auto", markerUnits: "userSpaceOnUse" });
    m.appendChild(svgEl("path", { d: "M0 0.5 L7.5 4 L0 7.5 z", fill }));
    defs.appendChild(m);
  });
  svg.appendChild(defs);

  // edges first (under nodes)
  (dag.edges || []).forEach((e) => {
    const a = layout[e.source], b = layout[e.target];
    if (!a || !b) return;
    const x1 = xOf(a.layer) + W, y1 = yOf(a.row) + H / 2;
    const x2 = xOf(b.layer) - 1, y2 = yOf(b.row) + H / 2;
    const dx = Math.max(18, (x2 - x1) * 0.5);
    const path = svgEl("path", {
      d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`,
      class: "dagedge p" + (protoIndex[e.protocol] || 0),
      "marker-end": "url(#arr)",
    });
    path.dataset.source = e.source;
    path.dataset.target = e.target;
    const t = svgEl("title"); t.textContent = `${e.source} → ${e.target} (${e.protocol})`;
    path.appendChild(t);
    svg.appendChild(path);
  });
  // nodes
  for (const name in layout) {
    const p = layout[name];
    const g = svgEl("g", { class: "dagnode" });
    g.dataset.name = name;
    const x = xOf(p.layer), y = yOf(p.row);
    g.appendChild(svgEl("rect", { x, y, width: W, height: H, rx: 10 }));
    g.appendChild(svgEl("circle", { class: "sdot", cx: x + 12, cy: y + H / 2, r: 3.2 }));
    const cx = x + W / 2 + 6, cy = y + H / 2;
    const text = svgEl("text", { x: cx, "text-anchor": "middle" });
    const lines = wrapLabel(name);   // 1 or 2 lines so the text fits the box
    if (lines.length === 1) {
      text.setAttribute("y", cy + 3.4);
      text.textContent = lines[0];
    } else {
      text.setAttribute("y", cy);
      [[-2.6, lines[0]], [10.5, lines[1]]].forEach(([dy, s]) => {
        const ts = svgEl("tspan", { x: cx, dy });
        ts.textContent = s;
        text.appendChild(ts);
      });
    }
    const title = svgEl("title");
    const tool = STATE.toolsByName && STATE.toolsByName[name];
    title.textContent = name + (tool && tool.summary ? " — " + tool.summary : "") + "\n(click for details)";
    g.appendChild(text); g.appendChild(title);
    g.onclick = () => openTool(name);
    svg.appendChild(g);
    STATE.dagNodes[name] = g;
  }
}

function renderProtoLegend(protocols) {
  const host = $("#proto-legend");
  host.innerHTML = "";
  protocols.forEach((p, i) => {
    const s = el("span");
    s.appendChild(el("i", "edge p" + i));
    s.appendChild(document.createTextNode(p));
    host.appendChild(s);
  });
}

// Split a long tool name onto two lines at the underscore nearest the middle,
// so the label never overflows its box.
function wrapLabel(name) {
  if (name.length <= 14) return [name];
  const idxs = [];
  for (let i = 1; i < name.length - 1; i++) if (name[i] === "_") idxs.push(i);
  if (!idxs.length) return [name];
  const mid = name.length / 2;
  let best = idxs[0];
  for (const i of idxs) if (Math.abs(i - mid) <= Math.abs(best - mid)) best = i;
  return [name.slice(0, best + 1), name.slice(best + 1)];   // keep the underscore on line 1
}

function highlightDAG() {
  for (const name in STATE.dagNodes) STATE.dagNodes[name].setAttribute("class", "dagnode");
  $$(".dagedge").forEach((p) => { p.classList.remove("lit"); p.setAttribute("marker-end", "url(#arr)"); });
  const byFn = {};
  // A tool that is done in one step and pending in a later one shows as
  // "running"/"planned" for the later step — the most informative state wins.
  const rank = { done: 1, planned: 2, active: 3 };
  STATE.steps.forEach((s) => {
    if (!s.function) return;
    const st = s.status === "done" ? "done" : s.status === "active" ? "active" : "planned";
    if (!byFn[s.function] || rank[st] > rank[byFn[s.function]]) byFn[s.function] = st;
  });
  for (const fn in byFn) {
    const g = STATE.dagNodes[fn];
    if (!g) continue;
    g.setAttribute("class", "dagnode " + byFn[fn]);
  }
  $$(".dagedge").forEach((p) => {
    if (byFn[p.dataset.source] && byFn[p.dataset.target]) {
      p.classList.add("lit");
      p.setAttribute("marker-end", "url(#arr-lit)");
      p.parentNode.appendChild(p.parentNode.removeChild(p)); // on top of other edges
    }
  });
  // keep nodes above edges
  for (const name in STATE.dagNodes) { const g = STATE.dagNodes[name]; g.parentNode.appendChild(g); }
}

/* ====================================================================== run */
async function startRun() {
  const request = $("#request").value.trim();
  if (!request) { toast("Type a request first."); $("#request").focus(); return; }
  if (STATE.running) { toast("A run is already in progress."); return; }
  resetRunUI();
  STATE.lastRequest = request;
  STATE.summary = "";
  const body = { request };
  const outdir = $("#outdir").value.trim();
  if (outdir) body.output_dir = outdir;
  const sel = $("#model-select");
  if (sel && sel.value) body.provider = sel.value;   // switch LLM if chosen

  $("#run-btn").disabled = true;
  let res;
  try {
    res = await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json());
  } catch (e) { toast("Could not reach server."); $("#run-btn").disabled = false; return; }
  if (res.error) { toast(res.error); $("#run-btn").disabled = false; return; }

  STATE.runId = res.run_id;
  STATE.running = true;
  STATE.startedAt = new Date();
  $("#stop-btn").disabled = false;
  // Move from the landing prompt to the agent-interaction workspace.
  $("#pb-text").textContent = request;
  $("#pb-text").title = request;
  setOutputDir(outdir || null);
  showWorkspace();
  logEntry("event", "run", "Run started · output " + res.output_dir);

  // Show motion immediately — the planner LLM call can take ~90s, during which
  // the only signal is a hidden console line, so without this the UI looks dead.
  rb("working", PHASE_LABELS.planner);
  rbTicker("the planner can take up to ~90 s for complex requests…");
  rbStartClock();

  saveRun({ id: res.run_id, request, outdir: outdir || null, t0: STATE.t0 });
  openStream(res.run_id);
}

function openStream(runId) {
  if (STATE.es) { STATE.es.close(); STATE.es = null; }
  // No ?after= here on purpose: on a reconnect the browser sends Last-Event-ID
  // itself, so the server resumes exactly after the last event we received.
  const es = new EventSource("/api/stream?run_id=" + runId);
  STATE.es = es;
  es.onopen = () => { if (STATE.reconnecting) { STATE.reconnecting = false; rbTicker("reconnected"); } };
  es.onmessage = (m) => { try { dispatch(JSON.parse(m.data)); } catch (err) { console.error(err); } };
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) {
      // The server refused the stream (run unknown after a server restart).
      if (STATE.running) { logEntry("warn", "connection", "Lost the run: the server no longer knows it (was it restarted?)."); rb("fail", "Connection lost"); endRun(); }
    } else if (STATE.running) {
      STATE.reconnecting = true;
      rbTicker("connection dropped — reconnecting…");
    }
  };
}

function setOutputDir(dir) {
  STATE.outputDir = dir || null;
  const row = $("#pb-out");
  if (dir) { $("#pb-outdir").textContent = dir; $("#pb-outdir").title = "click to copy"; row.hidden = false; }
  else row.hidden = true;
}

function resetRunUI() {
  $("#log").innerHTML = "";
  STATE.entries = 0;
  $("#activity-count").textContent = "";
  $("#plan").innerHTML = '<div class="empty">No plan yet.</div>';
  $("#plan-meta").textContent = "";
  $("#progress").hidden = true;
  $("#figures").innerHTML = "";
  $("#files").innerHTML = '<div class="empty">Files written by the run appear here with their server paths.</div>';
  $("#artifact-count").textContent = "";
  $("#interrupt-zone").innerHTML = "";
  $$(".gnode").forEach((g) => { g.classList.remove("active", "done"); g.classList.add("idle");
    const st = g.querySelector(".gstate"); if (st && !g.classList.contains("off")) st.textContent = g.dataset.role;
    g.querySelectorAll(".gsub").forEach((c) => c.classList.remove("active", "done"));
    const subs = g.querySelector(".gsubs"); if (subs) subs.classList.remove("touched"); });
  STATE.steps = [];
  STATE.meta = null;
  STATE.executedPlan = null;
  STATE.filesByKind = {};
  STATE.awaiting = false;
  STATE.activeSub = {};
  STATE.openGroups = { figure: true, report: true, table: true };
  $("#open-report").disabled = true;
  highlightDAG();
}

function endRun() {
  STATE.running = false;
  $("#run-btn").disabled = false;
  $("#stop-btn").disabled = true;
  rbStopClock();
  if (STATE.es) { STATE.es.close(); STATE.es = null; }
}

/* ------------------------------------------------------ landing/workspace */
function showWorkspace() {
  $("#landing").classList.add("hidden");
  $("#workspace").classList.remove("hidden");
  window.scrollTo({ top: 0 });
}
function newRun() {
  if (STATE.running && !confirm("A run is in progress. Stop it and start a new analysis?")) return;
  if (STATE.running) stopRun();
  endRun();
  saveRun(null);
  resetRunUI();
  $("#runbar").classList.add("hidden");
  $("#workspace").classList.add("hidden");
  $("#landing").classList.remove("hidden");
  $("#request").focus();
}

/* ----------------------------------------------------------- event router */
function dispatch(ev) {
  CURRENT_TS = ev.ts || null;
  switch (ev.type) {
    case "run_started":
      STATE.model = ev.model; STATE.provider = ev.provider;
      if (ev.report_enabled !== undefined) STATE.reportEnabled = ev.report_enabled;
      break;
    case "node":
      markNode(ev.name, ev.state);
      // Reflect the live phase in the status bar (skip the waiting node — the
      // interrupt handler gives it a clearer message).
      if (ev.state === "active" && ev.name !== "user_feedback_node" && !STATE.awaiting)
        rb("working", PHASE_LABELS[ev.name] || ev.label || ev.name);
      break;
    case "subnode":
      markSubnode(ev.parent, ev.name);
      if (!STATE.awaiting) rb("working", PHASE_LABELS[ev.parent] || ev.parent, ev.label);
      break;
    case "plan": onPlan(ev); break;
    case "plan_meta": onPlanMeta(ev); break;
    case "executed_plan": STATE.executedPlan = ev.text; break;
    case "steps": STATE.steps = ev.steps; renderPlan(); highlightDAG(); break;
    case "validation": onValidation(ev); break;
    case "message": {
      // The planner composes an internal "human" message (prompt + feedback);
      // label it as such so it is not mistaken for something the user typed.
      let who = ROLE_LABELS[ev.role] || ev.role || "agent";
      if (ev.role === "human" && /^Previous plan \(if any\)/.test(ev.text || "")) who = "planner input";
      else if (ev.role === "human" && /^User feedback:/.test(ev.text || "")) {
        // The agent echoes the review answer; an empty one is already logged as "Plan approved".
        if (!(ev.text || "").replace(/^User feedback:/, "").trim()) break;
        who = "your feedback";
      }
      logEntry(ev.role || "ai", who, ev.text);
      break;
    }
    case "tool_call": logToolCall(ev); break;
    case "tool_result": logToolResult(ev); break;
    case "console": rbTicker(ev.text); logConsole(ev.text); break;
    case "artifact": onArtifact(ev); break;
    case "output_dir": setOutputDir(ev.output_dir); break;
    case "interrupt": onInterrupt(ev); break;
    case "resumed":
      STATE.awaiting = false;
      $("#interrupt-zone").innerHTML = "";
      logEntry("human", "you", ev.text || (ev.kind === "plan" ? "Plan approved" : "Approved"));
      rb("working", "Working…"); rbTicker("");
      break;
    case "done": onDone(ev); break;
    case "error":
      logEntry("err", "error", ev.message + (ev.traceback ? "\n\n" + ev.traceback : ""));
      rb("fail", "Run ended with an error"); rbStopClock();
      break;
  }
}

function onPlan(ev) {
  STATE.steps = ev.steps || [];
  STATE.planText = ev.text || "";
  renderPlan();
  highlightDAG();
  if (STATE.steps.length) logEntry("event", "planner", `Plan drafted — ${STATE.steps.length} step(s). Review it in the panel on the right.`);
}

function onPlanMeta(ev) {
  STATE.meta = ev;
  const meta = [];
  if (ev.num_of_sample) meta.push(ev.num_of_sample + " sample" + (ev.num_of_sample === 1 ? "" : "s"));
  if (ev.input_file_path && ev.input_file_path.length) meta.push(ev.input_file_path.length + " input file" + (ev.input_file_path.length === 1 ? "" : "s"));
  $("#plan-meta").textContent = meta.join(" · ");
  if (ev.overall_plan) $("#plan-meta").title = ev.overall_plan;
}

function renderPlan() {
  const host = $("#plan");
  if (!STATE.steps.length) { host.innerHTML = '<div class="empty">No plan yet.</div>'; $("#progress").hidden = true; return; }
  host.innerHTML = "";
  STATE.steps.forEach((s) => {
    const row = el("div", "step " + (s.status || "pending"));
    const num = el("span", "snum");
    if (s.status === "done") num.innerHTML = ICONS.check; else num.textContent = String(s.index);
    row.appendChild(num);
    const body = el("div", "sbody");
    const fn = el("div", "sfn");
    if (s.auto_code) { fn.appendChild(document.createTextNode("custom code")); fn.appendChild(el("span", "auto", "auto-generated")); }
    else fn.textContent = s.function || s.title;
    body.appendChild(fn);
    if (s.title && s.title !== s.function) body.appendChild(el("div", "stitle", s.title));
    if (s.detail) body.appendChild(el("div", "sdetail", s.detail));
    row.appendChild(body);
    row.title = s.detail ? "click for the step's inputs / outputs" : "";
    row.onclick = () => { if (s.detail) row.classList.toggle("open"); };
    host.appendChild(row);
  });
  renderProgress();
}

function renderProgress() {
  const n = STATE.steps.length;
  const done = STATE.steps.filter((s) => s.status === "done").length;
  const p = $("#progress");
  p.hidden = !n;
  $("#progress-fill").style.width = (n ? Math.round(100 * done / n) : 0) + "%";
  $("#progress-text").textContent = `${done} / ${n} done`;
}

function onValidation(ev) {
  if (ev.valid) logEntry("event", "validator", "Plan validated — nothing else needed. Executing…");
  else logEntry("warn", "validator",
    "Plan needs more information: " + (ev.missing && ev.missing.length ? ev.missing.join("; ") : (ev.thinking || "—")));
}

/* --------------------------------------------------------------- artifacts */
function onArtifact(ev) {
  STATE.filesByKind = STATE.filesByKind || {};
  (STATE.filesByKind[ev.kind] = STATE.filesByKind[ev.kind] || []).push(ev);
  // Any figure with a URL gets a thumbnail — the server renders PDF→PNG and
  // serves PNG/JPG/SVG as-is, so both formats display.
  if (ev.kind === "figure" && ev.url) { addFigure(ev); $("#open-report").disabled = false; }
  if (ev.kind === "report" && ev.url) {
    $("#open-report").disabled = false;
    $("#open-report").title = "the agent's report.pdf";
  }
  renderFiles();
  const n = Object.values(STATE.filesByKind).reduce((a, b) => a + b.length, 0);
  $("#artifact-count").textContent = n + " file" + (n === 1 ? "" : "s");
}

function addFigure(ev) {
  const host = $("#figures");
  const card = el("div", "fig");
  const img = el("img");
  img.src = ev.url;
  img.alt = ev.name;
  img.loading = "lazy";
  img.onerror = () => { card.remove(); };
  card.appendChild(img);
  card.appendChild(el("div", "cap", ev.rel || ev.name));
  card.title = ev.path;
  card.onclick = () => openLightbox(ev.url, ev.path || ev.rel || ev.name);
  host.appendChild(card);
}

/* ----------------------------------------------------------- lightbox */
function openLightbox(url, caption) {
  $("#lb-img").src = url;
  $("#lb-cap").textContent = caption || "";
  $("#lb-copy").onclick = () => copyText(caption);
  $("#lightbox").classList.remove("hidden");
}
function closeLightbox() {
  $("#lightbox").classList.add("hidden");
  $("#lb-img").src = "";
}

const KIND_ORDER = ["figure", "report", "table", "track", "sequence", "code", "log", "other"];
const KIND_LABEL = { figure: "figures", report: "report", table: "tables", track: "tracks & alignments",
                     sequence: "sequences", code: "code", log: "logs", other: "other files" };
const ROW_CAP = 40;
function renderFiles() {
  const host = $("#files");
  host.innerHTML = "";
  let any = false;
  KIND_ORDER.forEach((kind) => {
    const items = (STATE.filesByKind || {})[kind];
    if (!items || !items.length) return;
    any = true;
    const grp = el("div", "fgroup" + (STATE.openGroups[kind] ? " open" : ""));
    const label = el("div", "fglabel");
    label.appendChild(el("span", "chev", "▸"));
    label.appendChild(el("span", "fsw " + kind));
    label.appendChild(el("span", null, KIND_LABEL[kind] || kind));
    label.appendChild(el("span", "n", String(items.length)));
    label.onclick = () => { STATE.openGroups[kind] = !STATE.openGroups[kind]; grp.classList.toggle("open"); };
    grp.appendChild(label);
    const rows = el("div", "rows");
    const showAll = STATE.openGroups[kind + ":all"];
    items.slice(0, showAll ? items.length : ROW_CAP).forEach((it) => {
      const viewable = (kind === "figure" || kind === "report") && it.url;
      const row = el("div", "frow" + (viewable ? " clickable" : ""));
      const path = el("span", "fpath", it.rel || it.path);
      path.title = viewable ? "click to view · " + it.path : "click to copy · " + it.path;
      path.onclick = () => (viewable ? openLightbox(it.url, it.path) : copyText(it.path));
      row.appendChild(path);
      if (it.size != null) row.appendChild(el("span", "fsize", fmtSize(it.size)));
      const cp = el("button", "btn ghost icon small"); cp.title = "copy full path"; cp.appendChild(icon("copy"));
      cp.onclick = (e) => { e.stopPropagation(); copyText(it.path); };
      row.appendChild(cp);
      rows.appendChild(row);
    });
    if (!showAll && items.length > ROW_CAP) {
      const more = el("div", "more", `show all ${items.length} files`);
      more.onclick = () => { STATE.openGroups[kind + ":all"] = true; renderFiles(); };
      rows.appendChild(more);
    }
    grp.appendChild(rows);
    host.appendChild(grp);
  });
  if (!any) host.innerHTML = '<div class="empty">Files written by the run appear here with their server paths.</div>';
}

/* --------------------------------------------------------------- interrupts */
const INTERRUPT_UI = {
  plan: { badge: "Plan review", title: "Review the plan before anything runs",
          accept: "Approve & execute", feedback: "Request changes",
          help: "Check the steps on the right. Approve to run the pipeline on this node, or describe what to change and the planner will revise the plan." },
  figures: { badge: "Figure review", title: "Review the figures",
             accept: "Looks good", feedback: "Request changes",
             help: "Approve the figures, or describe what to adjust." },
  code: { badge: "Code check", title: "Generated code keeps failing",
          accept: "Try once more", feedback: "Send a note",
          help: "The auto-generated code failed repeatedly. Continue to let the agent retry once more, or add a note about what went wrong." },
};

function onInterrupt(ev) {
  STATE.awaiting = true;
  const cfg = INTERRUPT_UI[ev.kind] || INTERRUPT_UI.plan;
  rb("waiting", "Needs your input", cfg.title);
  rbTicker("Respond in the form in the middle column to continue.");
  showInterrupt(ev);
}

function showInterrupt(ev) {
  const cfg = INTERRUPT_UI[ev.kind] || INTERRUPT_UI.plan;
  const zone = $("#interrupt-zone");
  zone.innerHTML = "";
  const box = el("div", "interrupt kind-" + ev.kind);
  const head = el("div", "ihead");
  head.appendChild(el("span", "ibadge", cfg.badge));
  head.appendChild(el("h3", null, cfg.title));
  if (ev.kind === "plan" && STATE.steps.length) head.appendChild(el("span", "istep", STATE.steps.length + " planned step" + (STATE.steps.length === 1 ? "" : "s")));
  box.appendChild(head);

  const bodyEl = el("div", "ibody");
  const help = el("div", "iprompt", cfg.help);
  help.title = ev.prompt || "";
  bodyEl.appendChild(help);
  const ta = el("textarea");
  ta.placeholder = ev.kind === "plan" ? "Optional — e.g. “only process sample C1”, “skip annotation”, “use mm10”…"
                                      : "Optional feedback — leave empty to accept as-is";
  bodyEl.appendChild(ta);

  const actions = el("div", "iactions");
  const accept = el("button", "btn primary"); accept.appendChild(icon("play", "fill")); accept.appendChild(document.createTextNode(cfg.accept));
  accept.onclick = () => respond("");
  const send = el("button", "btn"); send.appendChild(icon("edit")); send.appendChild(document.createTextNode(cfg.feedback));
  send.onclick = () => { if (!ta.value.trim()) { toast("Type your feedback first, or press " + cfg.accept); ta.focus(); return; } respond(ta.value.trim()); };
  const stop = el("button", "btn ghost danger", "Stop here");
  stop.title = "End the run now — nothing will be executed";
  stop.onclick = stopRun;
  const hint = el("span", "kbd-hint");
  hint.innerHTML = "<kbd>Ctrl</kbd>+<kbd>Enter</kbd> sends";
  actions.appendChild(accept); actions.appendChild(send); actions.appendChild(stop); actions.appendChild(hint);
  ta.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); ta.value.trim() ? respond(ta.value.trim()) : respond(""); }
  });
  bodyEl.appendChild(actions);
  box.appendChild(bodyEl);
  zone.appendChild(box);
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  ta.focus({ preventScroll: true });
}

async function respond(text) {
  $("#interrupt-zone").innerHTML = "";
  rb("working", "Sending your answer…");
  await fetch("/api/resume", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: STATE.runId, text }),
  }).catch(() => toast("resume failed"));
}

async function stopRun() {
  if (!STATE.runId) return;
  await fetch("/api/stop", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: STATE.runId }),
  }).catch(() => {});
  $("#interrupt-zone").innerHTML = "";
  rb("working", "Stopping…"); rbTicker("finishing the current step");
}

/* ------------------------------------------------------------------- done */
function onDone(ev) {
  const elapsed = fmtElapsed(Date.now() - STATE.t0);
  endRun();
  STATE.awaiting = false;
  STATE.done = ev;
  if (ev.stopped) rb("done", "Run stopped");
  else if (ev.success) rb("done", "Run complete");
  else rb("fail", "Run ended with an error");
  rbTicker(`${ev.n_steps || 0} planned step(s) · ${ev.n_artifacts || 0} file(s) written · ${elapsed}`);
  $$(".gnode.active").forEach((g) => { g.classList.remove("active"); g.classList.add("done"); });
  if (ev.executed_plan) STATE.executedPlan = ev.executed_plan;
  if (ev.output_dir) setOutputDir(ev.output_dir);

  const box = el("div", "summary" + (ev.stopped ? " stopped" : ev.success ? "" : " fail"));
  const h = el("h3");
  h.appendChild(icon(ev.stopped ? "stop" : ev.success ? "check" : "warn"));
  h.appendChild(document.createTextNode(ev.stopped ? (ev.executing ? "Run stopped before all steps finished" : "Run stopped at plan review — nothing was executed")
    : ev.success ? "Run complete" : "Run ended with an error"));
  box.appendChild(h);
  const stats = el("div", "stats");
  stats.innerHTML = `<span><b>${ev.n_steps || 0}</b> planned step(s)</span><span><b>${ev.n_artifacts || 0}</b> file(s) written</span><span><b>${elapsed}</b> elapsed</span>`;
  box.appendChild(stats);
  if (ev.output_dir || STATE.outputDir) {
    const code = el("div", "scode", "output: " + (ev.output_dir || STATE.outputDir));
    code.title = "click to copy"; code.onclick = () => copyText(ev.output_dir || STATE.outputDir);
    box.appendChild(code);
  }
  const actions = el("div", "sactions");
  const figs = (STATE.filesByKind && STATE.filesByKind.figure) || [];
  if (figs.length || (STATE.steps && STATE.steps.length)) {
    $("#open-report").disabled = false;
    const view = el("button", "btn primary"); view.appendChild(icon("report")); view.appendChild(document.createTextNode("View report"));
    view.onclick = openReport;
    actions.appendChild(view);
  }
  const again = el("button", "btn", "New analysis");
  again.onclick = newRun;
  actions.appendChild(again);
  box.appendChild(actions);
  $("#interrupt-zone").innerHTML = "";
  $("#interrupt-zone").appendChild(box);
  if (figs.length) setTimeout(() => $("#results-panel").scrollIntoView({ behavior: "smooth", block: "center" }), 100);
}

/* --------------------------------------------------------------- report */
/* The agent's own report: report.pdf, written by the report_generator sub-graph.
   It carries the captioned figures, the written analysis and the references, so
   when it exists it IS the report and the interface only embeds it. The summary
   below is the fallback for runs where that stage produced nothing. */
function agentReport() {
  const reps = (STATE.filesByKind && STATE.filesByKind.report) || [];
  return reps.find((r) => /report\.pdf$/i.test(r.name)) || reps[0] || null;
}
const rawUrl = (a) => a.url + "&raw=1";

function openReport() {
  const rep = agentReport();
  $("#report").classList.remove("hidden");
  const body = $("#rp-body");
  body.innerHTML = "";
  if (rep) return renderAgentReport(rep, body);
  renderSummaryReport(body);
}

function renderAgentReport(rep, body) {
  $("#rp-source").textContent = "written by the agent's report generator";
  $("#rp-print").hidden = true;          // the PDF viewer brings its own print
  $("#rp-open").hidden = false;
  $("#rp-open").onclick = () => window.open(rawUrl(rep), "_blank", "noopener");
  body.classList.add("pdf");
  const frame = el("iframe", "rp-frame");
  frame.src = rawUrl(rep);
  frame.title = "agent report";
  body.appendChild(frame);
  const path = el("div", "rp-path");
  path.appendChild(el("code", null, rep.path));
  const cp = el("button", "btn small", "Copy path");
  cp.onclick = () => copyText(rep.path);
  path.appendChild(cp);
  body.appendChild(path);
}

function renderSummaryReport(body) {
  $("#rp-source").textContent = "interface summary";
  $("#rp-print").hidden = false;
  $("#rp-open").hidden = true;
  body.classList.remove("pdf");
  const note = el("div", "rp-note");
  note.appendChild(el("strong", null, "This is the interface's own summary, not the agent's report. "));
  note.appendChild(document.createTextNode(
    STATE.reportEnabled === false
      ? "The report stage is switched off for this server (report_enabled: 'false' in config.yaml). "
        + "Turn it on and re-run to get the agent's report.pdf with captioned figures and written analysis."
      : "This run did not produce a report.pdf - the report stage did not run or did not finish."));
  body.appendChild(note);
  body.appendChild(el("h1", null, "ChromaPilot — analysis report"));
  const meta = el("div", "rp-meta");
  const bits = [];
  if (STATE.startedAt) bits.push(STATE.startedAt.toLocaleString());
  if (STATE.model) bits.push((STATE.provider ? STATE.provider + " · " : "") + STATE.model);
  if (STATE.outputDir) bits.push("output directory: " + STATE.outputDir);
  meta.textContent = bits.join("  ·  ");
  body.appendChild(meta);

  if (STATE.lastRequest) {
    body.appendChild(el("h2", null, "Request"));
    body.appendChild(el("p", null, STATE.lastRequest));
  }
  if (STATE.meta && (STATE.meta.overall_plan || STATE.meta.num_of_sample || (STATE.meta.input_file_path || []).length)) {
    body.appendChild(el("h2", null, "What the agent understood"));
    const dl = el("dl", "rp-grid");
    const add = (k, v) => { if (v == null || v === "" || v === 0) return; dl.appendChild(el("dt", null, k)); dl.appendChild(el("dd", null, String(v))); };
    add("Goal", STATE.meta.overall_plan);
    add("Samples", STATE.meta.num_of_sample);
    add("Input files", (STATE.meta.input_file_path || []).length ? STATE.meta.input_file_path.join("\n") : null);
    add("Required parameters", STATE.meta.required_parameters);
    body.appendChild(dl);
  }
  if (STATE.steps && STATE.steps.length) {
    body.appendChild(el("h2", null, "Pipeline steps"));
    const ul = el("ul", "rp-steps");
    STATE.steps.forEach((s) => {
      const li = el("li");
      li.appendChild(el("span", "n" + (s.status === "done" ? "" : " pending"), s.status === "done" ? "✓" : String(s.index)));
      const c = el("code", null, s.auto_code ? "custom code (auto-generated)" : (s.function || s.title));
      li.appendChild(c);
      if (s.title && s.title !== s.function) li.appendChild(el("span", "t", "— " + s.title));
      ul.appendChild(li);
    });
    body.appendChild(ul);
  }
  if (STATE.executedPlan) {
    body.appendChild(el("h2", null, "Executed steps with actual outputs"));
    body.appendChild(el("pre", null, STATE.executedPlan));
  } else if (STATE.planText) {
    body.appendChild(el("h2", null, "Plan as drafted"));
    body.appendChild(el("pre", null, STATE.planText));
  }
  const figs = (STATE.filesByKind && STATE.filesByKind.figure) || [];
  body.appendChild(el("h2", null, `Figures (${figs.length})`));
  if (!figs.length) {
    body.appendChild(el("div", "rp-empty", "No figures were produced for this run."));
  }
  figs.forEach((f) => {
    const wrap = el("div", "rp-fig");
    const img = el("img");
    img.src = f.url; img.alt = f.name; img.loading = "lazy";
    img.onclick = () => openLightbox(f.url, f.path);
    wrap.appendChild(img);
    wrap.appendChild(el("div", "cap", f.path || f.rel || f.name));
    body.appendChild(wrap);
  });
}
function closeReport() { $("#report").classList.add("hidden"); }

/* ------------------------------------------------------------------- log */
function nearBottom(log) { return log.scrollHeight - log.scrollTop - log.clientHeight < 90; }
function appendLog(e) {
  const log = $("#log");
  const stick = nearBottom(log);
  const empty = log.querySelector(".empty"); if (empty) empty.remove();
  log.appendChild(e);
  if (stick) log.scrollTop = log.scrollHeight;
}
function whoLine(who, extra) {
  const w = el("div", "who");
  w.appendChild(document.createTextNode(who));
  if (extra) w.appendChild(extra);
  w.appendChild(el("span", "t", fmtTime()));
  return w;
}
function countEntry() { STATE.entries += 1; $("#activity-count").textContent = STATE.entries + " entries"; }

function logEntry(cls, who, text) {
  if (!text) return;
  const e = el("div", "entry " + cls);
  e.appendChild(whoLine(who));
  const txt = el("div", "txt", text);
  e.appendChild(txt);
  if (text.length > 700) {
    txt.classList.add("clamp");
    const more = el("button", "more", "Show more");
    more.onclick = () => { txt.classList.toggle("clamp"); more.textContent = txt.classList.contains("clamp") ? "Show more" : "Show less"; };
    e.appendChild(more);
  }
  appendLog(e); countEntry();
}

function fmtVal(v) {
  if (v == null) return "None";
  if (Array.isArray(v)) return v.map(fmtVal).join("\n");
  if (typeof v === "object") return JSON.stringify(v, null, 1);
  return String(v);
}
function kvGrid(obj) {
  const g = el("div", "kv");
  Object.keys(obj).forEach((k) => {
    const v = obj[k];
    g.appendChild(el("span", "k", k));
    const val = el("span", "v", fmtVal(v));
    if (v == null) val.classList.add("nil");
    const s = typeof v === "string" ? v : "";
    if (s.startsWith("/")) { val.classList.add("path"); val.title = "click to copy"; val.onclick = () => copyText(s); }
    g.appendChild(val);
  });
  return g;
}
function logToolCall(ev) {
  const e = el("div", "entry toolcall");
  e.appendChild(whoLine("tool call", el("span", "fn", ev.name || "")));
  if (ev.args && typeof ev.args === "object") e.appendChild(kvGrid(ev.args));
  else e.appendChild(el("div", "txt", esc(ev.args)));
  appendLog(e); countEntry();
}
function logToolResult(ev) {
  const e = el("div", "entry tool");
  e.appendChild(whoLine("tool result", el("span", "fn", ev.name || "")));
  if (ev.result && typeof ev.result === "object" && !Array.isArray(ev.result)) e.appendChild(kvGrid(ev.result));
  else e.appendChild(el("div", "txt", ev.text || ""));
  appendLog(e); countEntry();
}
function logConsole(text) {
  const e = el("div", "entry console");
  e.appendChild(el("div", "txt", text));
  appendLog(e);
}

/* ----------------------------------------------------------------- toast */
let toastTimer = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2200);
}
function copyText(text) {
  if (!text) return;
  const done = () => toast("Path copied");
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  else fallbackCopy(text, done);
}
function fallbackCopy(text, done) {
  const ta = el("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch (_) { toast("Copy not available — select the path manually"); }
  ta.remove();
}

/* ------------------------------------------------------------------- wire */
function wire() {
  $$(".ic[data-icon]").forEach((i) => { i.innerHTML = ICONS[i.dataset.icon] || ""; });
  $("#run-btn").onclick = startRun;
  $("#stop-btn").onclick = stopRun;
  $("#new-run").onclick = newRun;
  $("#request").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") startRun();
  });
  // Remember the draft request across reloads (per browser).
  try {
    const saved = localStorage.getItem("mea.request");
    if (saved && !$("#request").value) $("#request").value = saved;
    $("#request").addEventListener("input", () => { try { localStorage.setItem("mea.request", $("#request").value); } catch (_) {} });
  } catch (_) {}
  const ex = $("#examples");
  EXAMPLES.forEach((x) => {
    const c = el("button", "chip", x.label);
    c.type = "button"; c.title = "fill the box with this example";
    c.onclick = () => { $("#request").value = x.text; $("#request").focus(); $("#request").dispatchEvent(new Event("input")); };
    ex.appendChild(c);
  });
  $("#pb-outdir").onclick = () => copyText($("#pb-outdir").textContent);
  $("#show-console").addEventListener("change", (e) => {
    $("#log").classList.toggle("hide-console", !e.target.checked);
  });
  $("#log").classList.add("hide-console");
  $$(".collapsible").forEach((h) => {
    h.addEventListener("click", () => {
      h.classList.toggle("open");
      const t = document.getElementById(h.dataset.target);
      if (t) t.classList.toggle("collapsed");
    });
  });
  // Lightbox: close on ✕, click-outside, or Esc.
  $("#lb-close").onclick = closeLightbox;
  $("#lightbox").addEventListener("click", (e) => { if (e.target.id === "lightbox") closeLightbox(); });
  // Report modal.
  $("#open-report").onclick = openReport;
  $("#rp-close").onclick = closeReport;
  $("#rp-print").onclick = () => window.print();
  $("#report").addEventListener("click", (e) => { if (e.target.id === "report") closeReport(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeLightbox(); closeReport(); } });
  window.addEventListener("beforeunload", (e) => { if (STATE.running) { e.preventDefault(); e.returnValue = ""; } });
}

wire();
boot();
