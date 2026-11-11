/* ScholarLift UI — profile form → live run board → tabbed results.
   Safety: every piece of untrusted text goes through el()/textContent (never innerHTML);
   links are http(s)-only with rel="noopener noreferrer". */
"use strict";

/* ── DOM helpers ──────────────────────────────────────────────────────── */
const SVGNS = "http://www.w3.org/2000/svg";
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}
function svgEl(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  return n;
}
function icon(name, cls) {
  const svg = svgEl("svg", { class: "ic" + (cls ? " " + cls : ""), "aria-hidden": "true" });
  svg.append(svgEl("use", { href: "#i-" + name }));
  return svg;
}
const $ = (id) => document.getElementById(id);
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function safeUrl(u) {
  try { const p = new URL(u); return (p.protocol === "http:" || p.protocol === "https:") ? u : null; }
  catch { return null; }
}
function link(u, text, cls, ic) {
  const s = safeUrl(u);
  if (!s) return el("span", { class: "muted", text: text || u || "—" });
  return el("a", { href: s, target: "_blank", rel: "noopener noreferrer", class: cls || "link" },
    ic ? icon(ic) : null, text || s);
}
const fmt = (n, d = 0) => (n == null || isNaN(n) ? "—" : Number(n).toFixed(d));
const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const truncate = (s, n) => (s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s);
const prettify = (s) => String(s || "").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const lines = (v) => (v || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
const csv = (v) => (v || "").split(",").map((s) => s.trim()).filter(Boolean);
const humanSize = (b) => (b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);
const show = (...ids) => ids.forEach((i) => $(i).classList.remove("hidden"));
const hide = (...ids) => ids.forEach((i) => $(i).classList.add("hidden"));

/* ── constants ────────────────────────────────────────────────────────── */
const AGENTS = [
  ["profile_enricher", "Profile", "user", "Reads your documents and form"],
  ["opportunity_scout", "Search", "search", "Official directories and the live web"],
  ["source_auditor", "Verify", "shield", "Fetches pages, checks evidence"],
  ["ranking_strategist", "Rank", "trend", "Scores eligibility and fit"],
  ["gap_advisor", "Improve", "bulb", "Finds gaps you can close"],
  ["application_strategist", "Write", "pen", "Drafts your statement"],
];
const STAGE_W = [8, 22, 34, 14, 10, 12];
const STATUS_LABEL = { eligible: "Eligible", likely_eligible: "Likely eligible", near_miss: "Near miss",
  ineligible: "Ineligible", requirements_unverified: "Requirements unclear" };
const STATUS_ICON = { eligible: "check", likely_eligible: "check", near_miss: "trend", ineligible: "x",
  requirements_unverified: "help" };
const TRUST_LABEL = { OFFICIAL_INSTITUTION: "Official source", RECOGNIZED_FUNDER: "Recognized funder",
  AGGREGATOR: "Aggregator", UNKNOWN: "Other website" };
const FUNDING_LABEL = { FULL_TUITION_STIPEND: "Full tuition + stipend", FULL_TUITION: "Full tuition",
  PARTIAL: "Partial funding", NONE: "No funding", UNKNOWN: "Funding not stated" };
const OUTCOME_LABEL = { COMPLETE: "Complete", DEGRADED: "Partial results", IN_PROGRESS: "In progress",
  FAILED_SAFE: "Stopped safely" };
const PRIORITY_LABEL = { 1: "High impact", 2: "Worth doing", 3: "Nice to have", 4: "Optional" };
const COUNTRIES = ["Australia", "Austria", "Belgium", "Canada", "China", "Denmark", "Finland", "France",
  "Germany", "Hong Kong", "Ireland", "Italy", "Japan", "Netherlands", "New Zealand", "Norway", "Singapore",
  "South Korea", "Spain", "Sweden", "Switzerland", "United Arab Emirates", "United Kingdom", "United States"];
const MAX_FILES = 5;
const LS_KEY = "scholarlift.form";
const REFRESH_KINDS = new Set(["node_completed", "phase_a_ready", "sop_ready", "run_completed",
  "run_degraded", "error"]);

/* ── state ────────────────────────────────────────────────────────────── */
let runId = null, es = null, tickTimer = null, pollTimer = null, startT = 0, lastSeq = 0;
let running = false, demoMode = false, lastVm = null, lastSig = "";
let activeTab = "top", statusFilter = "all", feedCount = 0, progMax = 0;
const agentState = {};
const openKeys = new Set();

/* ── theme & toasts ───────────────────────────────────────────────────── */
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  const b = $("btn-theme"); clear(b); b.append(icon(t === "dark" ? "sun" : "moon"));
}
function toggleTheme() {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  try { localStorage.setItem("scholarlift.theme", next); } catch {}
  applyTheme(next);
}
function toast(text, kind = "info", ms = 4200) {
  const ic = kind === "error" ? "alert" : kind === "success" ? "check" : "info";
  const t = el("div", { class: `toast ${kind}`, role: "status" }, icon(ic), el("span", { text }));
  $("toasts").append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 320); }, ms);
}
function setMsg(t) { $("form-msg").textContent = t || ""; }

/* ── chip inputs (comma-separated fields) ─────────────────────────────── */
function initChips(input) {
  const field = el("input", { class: "chips-input", type: "text", placeholder: input.placeholder,
    autocomplete: "off", "aria-label": input.name.replace(/_/g, " ") });
  if (input.dataset.suggest === "countries") field.setAttribute("list", "dl-countries");
  const wrap = el("div", { class: "chips" }, field);
  input.type = "hidden"; input.after(wrap); input._chips = wrap;
  const commit = () => { if (field.value.trim()) { addChips(input, field.value); field.value = ""; } };
  field.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(); }
    else if (e.key === "Backspace" && !field.value) { setChips(input, csv(input.value).slice(0, -1)); }
  });
  field.addEventListener("change", commit);
  field.addEventListener("blur", commit);
  wrap.addEventListener("click", () => field.focus());
  renderChips(input);
}
function setChips(input, values) {
  input.value = values.join(", "); renderChips(input);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}
function addChips(input, raw) {
  const cur = csv(input.value);
  for (const p of csv(raw)) if (!cur.some((c) => c.toLowerCase() === p.toLowerCase())) cur.push(p);
  setChips(input, cur);
}
function renderChips(input) {
  const wrap = input._chips; if (!wrap) return;
  const field = wrap.querySelector(".chips-input");
  wrap.querySelectorAll(".chip").forEach((c) => c.remove());
  for (const v of csv(input.value)) {
    field.before(el("span", { class: "chip" }, v,
      el("button", { type: "button", class: "chip-x", "aria-label": `Remove ${v}`,
        onclick: (e) => { e.stopPropagation(); setChips(input, csv(input.value).filter((x) => x !== v)); } },
        icon("x"))));
  }
}
function setField(name, v) {
  const n = document.querySelector(`[name="${CSS.escape(name)}"]`); if (!n) return;
  if (n.type === "checkbox") n.checked = !!v && v !== "off"; else n.value = v ?? "";
  if (n._chips) renderChips(n);
}

/* ── documents (drop zone + file rows) ────────────────────────────────── */
const fileRows = () => [...document.querySelectorAll("#file-rows .file-row")];
function labelFor(name) {
  const l = name.toLowerCase();
  return /resume|cv|curriculum/.test(l) ? "RESUME" : /transcript|marksheet|grade/.test(l) ? "TRANSCRIPT" : "OTHER";
}
function addFileRow(file, label) {
  const row = el("div", { class: "file-row" },
    icon("file", "file-ic"),
    el("div", { class: "file-meta" },
      el("div", { class: "file-name", text: file ? file.name : "" }),
      el("div", { class: "file-size muted small", text: file ? humanSize(file.size) : "" })),
    el("select", { name: "file_label", "aria-label": "Document type" },
      ...["RESUME", "TRANSCRIPT", "OTHER"].map((v) => el("option", { value: v, text: prettify(v.toLowerCase()) }))),
    el("button", { type: "button", class: "icon-btn", "aria-label": "Remove file",
      onclick: () => { row.remove(); updateMeter(); } }, icon("x")),
    el("input", { type: "file", name: "file", class: "file-input", tabindex: "-1" }));
  if (file) {
    const dt = new DataTransfer(); dt.items.add(file);
    row.querySelector("[name=file]").files = dt.files;
    row.querySelector("[name=file_label]").value = label || labelFor(file.name);
  }
  $("file-rows").append(row);
  return row;
}
function addFiles(list) {
  for (const f of list) {
    if (fileRows().length >= MAX_FILES) { toast(`You can attach up to ${MAX_FILES} documents.`, "error"); break; }
    addFileRow(f);
  }
  updateMeter();
}
function initDropzone() {
  const dz = $("dropzone"), input = $("dz-input");
  const open = () => input.click();
  dz.addEventListener("click", open);
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });
  ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
}

/* ── test-score rows ──────────────────────────────────────────────────── */
function addTestRow(test, score) {
  const row = el("div", { class: "test-row" },
    el("select", { name: "test_name", "aria-label": "Test" },
      ...["IELTS", "TOEFL", "PTE", "Duolingo", "GRE", "GMAT", "Other"].map((v) =>
        el("option", { value: v, text: v, ...(v === test ? { selected: "" } : {}) }))),
    el("input", { name: "test_score", placeholder: "Score", inputmode: "decimal", "aria-label": "Score",
      ...(score != null ? { value: score } : {}) }),
    el("button", { type: "button", class: "icon-btn", "aria-label": "Remove test",
      onclick: () => { row.remove(); updateMeter(); saveForm(); } }, icon("x")));
  $("test-rows").append(row);
}
const testPairs = () => [...document.querySelectorAll("#test-rows .test-row")].map((r) =>
  [r.querySelector("[name=test_name]").value, r.querySelector("[name=test_score]").value]);

/* ── form → profile ───────────────────────────────────────────────────── */
function formToProfile() {
  const f = new FormData($("profile-form"));
  const g = (n) => (f.get(n) || "").toString().trim();
  const profile = {
    nationality: g("nationality"), current_country: g("current_country"),
    languages: csv(g("languages")),
    education: [], skills: [], projects: [], research: [], experience: [],
    activities: [], awards: [], language_tests: [],
    financial_need: { level: g("need_level") || "unknown", annual_budget_usd: Number(g("budget")) || null,
      context: g("need_context") },
    preferences: { degree_level: g("pref_degree"), target_fields: csv(g("target_fields")),
      countries: csv(g("countries")), instruction_languages: csv(g("instr_langs")), intake: g("intake"),
      funding_required: g("need_level") === "full" || g("need_level") === "partial" },
    completeness_flags: {}, field_sources: {},
  };
  const degree = g("degree"), field = g("field"), inst = g("institution"), cgpa = g("cgpa");
  if (degree || field || inst || cgpa) {
    profile.education = [{ degree, field, institution: inst, level: g("level"),
      cgpa_value: cgpa ? Number(cgpa) : null, cgpa_scale: Number(g("cgpa_scale")) || null,
      grade_system: f.get("lower_is_better") ? "lower_is_better" : "standard" }];
  }
  for (const [name, score] of testPairs()) {
    if (name && Number(score)) profile.language_tests.push({ test_name: name, overall_score: Number(score) });
  }
  lines(g("research")).forEach((t) => profile.research.push({ title: t, kind: "publication" }));
  lines(g("experience")).forEach((t) => {
    const p = t.split("—").map((s) => s.trim());
    profile.experience.push({ role: p[0] || t, organization: p[1] || "",
      duration_months: Number(p[2]) || null, kind: "internship" });
  });
  lines(g("activities")).forEach((t) => {
    const p = t.split("—").map((s) => s.trim());
    profile.activities.push({ title: p[1] || t, role: p[0] || t });
  });
  lines(g("awards")).forEach((t) => profile.awards.push({ name: t }));
  return profile;
}

function fillFromProfile(p) {
  const edu = (p.education || [])[0] || {}, pr = p.preferences || {}, fn = p.financial_need || {};
  const set = setField;
  set("degree", edu.degree); set("level", edu.level); set("institution", edu.institution);
  set("field", edu.field); set("cgpa", edu.cgpa_value); set("cgpa_scale", edu.cgpa_scale);
  set("pref_degree", pr.degree_level); set("intake", pr.intake);
  set("target_fields", (pr.target_fields || []).join(", "));
  set("countries", (pr.countries || []).join(", "));
  set("instr_langs", (pr.instruction_languages || []).join(", "));
  set("nationality", p.nationality); set("current_country", p.current_country);
  set("languages", (p.languages || []).join(", "));
  clear($("test-rows"));
  (p.language_tests || []).forEach((t) => addTestRow(t.test_name, t.overall_score));
  if (!(p.language_tests || []).length) addTestRow();
  set("research", (p.research || []).map((r) => `${r.title}${r.venue ? " — " + r.venue : ""}`).join("\n"));
  set("experience", (p.experience || []).map((e) =>
    `${e.role} — ${e.organization}${e.duration_months ? " — " + e.duration_months : ""}`).join("\n"));
  set("activities", (p.activities || []).map((a) => `${a.role} — ${a.title}`).join("\n"));
  set("awards", (p.awards || []).map((a) => a.name).join("\n"));
  set("need_level", fn.level); set("budget", fn.annual_budget_usd); set("need_context", fn.context);
  updateMeter(); saveForm();
}

/* ── local persistence ────────────────────────────────────────────────── */
function saveForm() {
  const o = { fields: {}, tests: testPairs() };
  for (const [k, v] of new FormData($("profile-form")).entries()) {
    if (!["file", "file_label", "test_name", "test_score", "access_code"].includes(k)) o.fields[k] = v;
  }
  try { localStorage.setItem(LS_KEY, JSON.stringify(o)); } catch {}
}
function loadForm() {
  let o; try { o = JSON.parse(localStorage.getItem(LS_KEY) || "{}"); } catch { return; }
  for (const [k, v] of Object.entries(o.fields || {})) setField(k, v);
  if ((o.tests || []).length) { clear($("test-rows")); o.tests.forEach(([n, s]) => addTestRow(n, s)); }
}

/* ── profile strength meter ───────────────────────────────────────────── */
function strength() {
  const f = new FormData($("profile-form")); const g = (n) => (f.get(n) || "").toString().trim();
  const items = [
    [22, fileRows().length > 0, "Attach your résumé or transcript — it fills in details you may forget."],
    [20, g("pref_degree") && csv(g("target_fields")).length && csv(g("countries")).length,
      "Pick a target degree, fields and countries."],
    [14, g("degree") && g("field") && g("institution"), "Add your degree, field and institution."],
    [10, g("cgpa") && g("cgpa_scale"), "Add your GPA and its scale — many awards have cut-offs."],
    [10, lines(g("experience")).length || lines(g("research")).length || lines(g("activities")).length ||
      lines(g("awards")).length, "Add research, work or leadership experience."],
    [8, g("nationality"), "Add your nationality — it unlocks country-specific awards."],
    [8, testPairs().some(([, s]) => Number(s)), "Add an IELTS/TOEFL score — language rules are checked against it."],
    [8, g("need_level") && g("need_level") !== "unknown", "Tell us how much funding you need."],
  ];
  return { pct: items.reduce((s, [w, ok]) => s + (ok ? w : 0), 0),
    tips: items.filter((i) => !i[1]).slice(0, 3).map((i) => i[2]) };
}
function updateMeter() {
  const { pct, tips } = strength();
  $("meter-fill").style.width = pct + "%";
  $("meter-fill").className = `meter-fill ${pct >= 75 ? "high" : pct >= 45 ? "mid" : "low"}`;
  $("meter-pct").textContent = pct + "%";
  $("meter").setAttribute("aria-valuenow", String(pct));
  const ul = $("meter-tips"); clear(ul);
  tips.forEach((t) => ul.append(el("li", {}, icon("plus"), t)));
  if (!tips.length) ul.append(el("li", { class: "ok" }, icon("check"), "Great — this is a strong profile."));
}

/* ── samples & demo ───────────────────────────────────────────────────── */
async function fetchSample(name) {
  const r = await fetch(`/api/samples/${name}`);
  if (!r.ok) throw new Error(`sample ${name} not found`);
  return r.text();
}
async function loadSample(prefix) {
  try {
    if (document.body.dataset.view !== "form") backToForm(true);
    fillFromProfile(JSON.parse(await fetchSample(`${prefix}_profile.json`)));
    clear($("file-rows"));
    for (const [suffix, label] of [["resume", "RESUME"], ["transcript", "TRANSCRIPT"]]) {
      const text = await fetchSample(`${prefix}_${suffix}.md`).catch(() => null);
      if (text) addFileRow(new File([text], `${prefix}_${suffix}.md`, { type: "text/plain" }), label);
    }
    updateMeter();
    toast(`Sample "${prefix}" loaded — review it, then press “Find my scholarships”.`, "success");
    $("profile-form").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { toast("Could not load the sample: " + e.message, "error"); }
}
async function loadDemo(name) {
  try {
    const vm = JSON.parse(await fetchSample(`demo_result_${name}.json`));
    stopRun(); demoMode = true; runId = null; lastSig = "";
    showResults(false); show("demo-banner"); render(vm); setTab("top");
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (e) { toast("Could not load the example: " + e.message, "error"); }
}

/* ── views ────────────────────────────────────────────────────────────── */
function setView(v) { document.body.dataset.view = v; }
function showForm() { setView("form"); show("intro", "profile-form"); hide("board", "results", "demo-banner"); }
function showRun() { setView("running"); hide("intro", "profile-form", "demo-banner"); show("board"); $("board").classList.remove("done"); }
function showResults(withLog) {
  setView("results"); hide("intro", "profile-form", "board"); show("results");
  if (withLog) { show("board"); $("board").classList.add("done"); }
}
function backToForm(silent) {
  if (running && !silent && !confirm("Stop the current search and start a new one?")) return;
  if (running) cancelRun(true);
  stopRun(); demoMode = false; showForm();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ── run lifecycle ────────────────────────────────────────────────────── */
async function startRun(ev) {
  ev.preventDefault();
  const p = formToProfile();
  const hasForm = p.education.length && p.preferences.degree_level &&
    p.preferences.target_fields.length && p.preferences.countries.length;
  const picked = fileRows().filter((r) => r.querySelector("[name=file]").files.length);
  if (!picked.length && !hasForm) {
    const m = "Attach a document, or fill in your degree, target degree, fields and countries.";
    setMsg(m); toast(m, "error"); return;
  }
  const fd = new FormData();
  picked.forEach((r) => { const f = r.querySelector("[name=file]").files[0]; fd.append("files", f, f.name); });
  fd.append("labels", JSON.stringify(picked.map((r) => r.querySelector("[name=file_label]").value)));
  fd.append("profile", JSON.stringify(p));
  fd.append("display_name", document.querySelector("[name=display_name]").value || "");
  fd.append("github_username", document.querySelector("[name=github]").value || "");
  fd.append("access_code", document.querySelector("[name=access_code]")?.value || "");
  setMsg("Starting…"); $("btn-start").disabled = true;
  try {
    const r = await fetch("/api/runs", { method: "POST", body: fd });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { const m = data.detail || `Error ${r.status}`; setMsg(m); toast(String(m), "error"); return; }
    runId = data.run_id; saveForm(); setMsg(""); beginBoard(); connectEvents(0);
  } catch (e) { setMsg("Network error: " + e.message); toast("Network error: " + e.message, "error"); }
  finally { $("btn-start").disabled = false; }
}

function beginBoard() {
  demoMode = false; running = true; lastVm = null; lastSig = ""; lastSeq = 0; feedCount = 0;
  Object.keys(agentState).forEach((k) => delete agentState[k]);
  showRun(); hide("results");
  $("board-title").textContent = "Searching for your scholarships";
  $("board-sub").textContent = "Live web search · verification · ranking";
  $("btn-cancel").textContent = "Cancel";
  clear($("feed")); $("feed-count").textContent = "0";
  progMax = 0; buildAgents(); buildStats($("counters"), {}, "board"); setProgress(2);
  startT = Date.now(); clearInterval(tickTimer); clearInterval(pollTimer);
  tickTimer = setInterval(tick, 1000); pollTimer = setInterval(refreshView, 7000); tick();
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function tick() {
  const s = Math.floor((Date.now() - startT) / 1000);
  $("run-timer").textContent = fmtTime(s);
  const stage = AGENTS.reduce((a, [id], i) => a + (agentState[id]?.status === "done" ? STAGE_W[i]
    : agentState[id]?.status === "running" ? STAGE_W[i] * 0.5 : 0), 0);
  setProgress(clamp(Math.max(stage, Math.min(90, s / 240 * 90)), 2, 96));
}
function setProgress(pct) {
  progMax = Math.max(progMax, pct);
  $("progress-fill").style.width = progMax + "%";
  $("progress").setAttribute("aria-valuenow", String(Math.round(progMax)));
}
function stopRun() {
  running = false; clearInterval(tickTimer); clearInterval(pollTimer);
  if (es) { es.close(); es = null; }
}

function buildAgents() {
  const wrap = $("agent-cards"); clear(wrap);
  for (const [id, label, ic, desc] of AGENTS) {
    wrap.append(el("li", { class: "step idle", id: `agent-${id}` },
      el("span", { class: "step-ic" }, icon(ic)),
      el("div", { class: "step-body" },
        el("div", { class: "step-name", text: label }),
        el("div", { class: "step-desc", text: desc }),
        el("div", { class: "step-note", id: `note-${id}` }))));
  }
}
function agentSet(id, status, note) {
  const card = $(`agent-${id}`); if (!card) return;
  agentState[id] = { status };
  card.className = `step ${status}`;
  if (note != null && note !== "") $(`note-${id}`).textContent = note;
}
function updateNotes(c) {
  const notes = {
    opportunity_scout: c.candidates != null ? `${plural(c.candidates, "page")} found` : "",
    source_auditor: c.verified != null ? `${c.verified} verified` : "",
    ranking_strategist: c.assessed ? `${c.assessed} assessed` : "",
  };
  for (const [id, text] of Object.entries(notes)) {
    if (text && agentState[id]) $(`note-${id}`).textContent = text;
  }
}
function buildStats(wrap, c, mode) {
  clear(wrap);
  const items = mode === "board"
    ? [["Pages found", c.candidates, "search"], ["Verified", c.verified, "shield"], ["Leads", c.leads, "list"],
       ["Assessed", c.assessed, "target"], ["Top picks", c.top_5, "star"], ["Searches", c.search_rounds, "globe"]]
    : [["Verified", c.verified, "shield"], ["Top picks", c.top_5, "star"], ["Almost eligible", c.near, "trend"],
       ["Assessed", c.assessed, "target"], ["Pages scanned", c.candidates, "search"]];
  for (const [label, v, ic] of items) {
    wrap.append(el("div", { class: "stat" }, icon(ic),
      el("div", { class: "stat-v", text: v == null ? "·" : String(v) }),
      el("div", { class: "stat-l", text: label })));
  }
}

/* ── SSE ──────────────────────────────────────────────────────────────── */
function connectEvents(cursor) {
  if (es) es.close();
  es = new EventSource(`/api/runs/${runId}/events?cursor=${cursor}`);
  es.onmessage = (m) => {
    let ev; try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.type === "complete") { onDone(); return; }
    if (ev.type === "error") { feed("system", ev.message, "error"); return; }
    if (ev.seq != null) lastSeq = Math.max(lastSeq, ev.seq);
    onEvent(ev);
  };
  es.onerror = () => {
    if (es) es.close();
    if (!running) return;
    feed("system", "Connection lost — reconnecting…", "warn");
    setTimeout(() => { if (running) connectEvents(lastSeq); }, 1500);
  };
}
function mapNode(n) {
  if (n === "lead_expansion") return "opportunity_scout";
  if (n === "finalize" || n === "gateway") return "application_strategist";
  return n;
}
function onEvent(ev) {
  const msg = ev.payload?.message || ev.kind;
  const bad = ev.severity === "ERROR" || ev.kind === "error";
  const warn = ev.severity === "WARNING" || /failover|fallback|downshift|failed|degraded/i.test(msg);
  feed(ev.node, msg, bad ? "error" : warn ? "warn" : "");
  const id = mapNode(ev.node);
  if (ev.kind === "node_started") agentSet(id, "running");
  if (ev.kind === "node_completed" && ev.node !== "lead_expansion") agentSet(id, "done");
  if (ev.kind === "error" || ev.kind === "warning") { if ($(`agent-${id}`) && agentState[id]?.status !== "done") agentSet(id, "warn"); }
  if (REFRESH_KINDS.has(ev.kind)) refreshView();
  if (ev.kind === "run_completed" || ev.kind === "run_degraded") onDone();
}
function feed(node, text, cls) {
  const ul = $("feed"), s = Math.floor((Date.now() - startT) / 1000);
  ul.prepend(el("li", { class: `feed-item ${cls || ""}` },
    el("span", { class: "feed-t", text: fmtTime(Math.max(0, s)) }),
    el("span", { class: "feed-node", text: prettify(node) }),
    el("span", { class: "feed-msg", text })));
  while (ul.children.length > 150) ul.removeChild(ul.lastChild);
  $("feed-count").textContent = String(++feedCount);
}
async function onDone() {
  if (!running && !demoMode) return;
  stopRun(); setProgress(100); tick();
  for (const [id] of AGENTS) {
    const st = agentState[id]?.status;
    if (st === "running" || st === "warn") agentSet(id, "done");
    else if (!st) { const c = $(`agent-${id}`); if (c) c.className = "step skipped"; }
  }
  await refreshView(true);
  const hasContent = lastVm && ((lastVm.top_5 || []).length || (lastVm.other_opportunities || []).length);
  if (hasContent) {
    showResults(true); $("board-title").textContent = "Run log";
    $("board-sub").textContent = "Everything the agents did, step by step";
  } else {
    $("board-title").textContent = "The run finished without verified opportunities";
    $("board-sub").textContent = "Try broadening your countries or fields, or add more detail to your profile.";
    $("btn-cancel").textContent = "Back to form";
  }
}

/* ── progressive view ─────────────────────────────────────────────────── */
async function refreshView(force) {
  if (!runId || demoMode) return;
  try {
    const r = await fetch(`/api/runs/${runId}`);
    if (r.ok) render(await r.json(), force);
  } catch {}
}

function statusOf(item) { return item.assessment ? item.assessment.status : item.status; }
function allRows(vm) {
  const rows = (vm.top_5 || []).filter((c) => !c.missing).map((c) => ({
    id: c.id, name: c.name, provider: c.provider, country: c.country, verified: true,
    status: c.assessment?.status, score: c.assessment?.composite_score, url: c.official_url,
    trust_tier: c.trust_tier, deadline: c.deadline, reason: "", top: true }));
  const seen = new Set(rows.map((r) => r.id));
  for (const o of vm.other_opportunities || []) {
    if (!seen.has(o.id)) rows.push({ ...o, reason: o.not_ranked_reason || "" });
  }
  return rows;
}
function unmetText(a) {
  return (a.rules || []).filter((r) => String(r.result).toUpperCase() === "UNMET" && r.closable !== false)
    .map((r) => `${r.label}: ${r.explanation || r.required || ""}`);
}
function nearMisses(vm) {
  const out = [];
  for (const c of vm.top_5 || []) {
    if (!c.missing && c.assessment?.status === "near_miss") {
      out.push({ id: c.id, name: c.name, provider: c.provider, country: c.country, url: c.official_url,
        score: c.assessment.composite_score, deadline: c.deadline, gaps: c.assessment.gaps || [],
        gapText: unmetText(c.assessment), rank: c.rank });
    }
  }
  for (const o of vm.other_opportunities || []) {
    if (o.status === "near_miss") {
      out.push({ id: o.id, name: o.name, provider: o.provider, country: o.country, url: o.url,
        score: o.score, deadline: o.deadline, gaps: o.gaps || [], gapText: o.near_miss_gaps || [] });
    }
  }
  return out.sort((a, b) => (b.score || 0) - (a.score || 0));
}

function render(vm, force) {
  const sig = JSON.stringify({ ...vm, elapsed_s: 0 });
  buildStats($("counters"), vm.counters || {}, "board");
  updateNotes(vm.counters || {});
  if (sig === lastSig && !force) return;
  lastSig = sig; lastVm = vm;
  const near = nearMisses(vm), rows = allRows(vm), plan = vm.improvement_plan;
  const apps = vm.applications || {};
  const hasContent = rows.length || plan || Object.keys(apps).length;
  $("results").classList.toggle("hidden", !hasContent);
  if (vm.profile) { renderProfile(vm.profile, $("profile-summary")); }
  const badge = $("outcome-badge"), oc = vm.outcome || vm.status;
  badge.textContent = OUTCOME_LABEL[oc] || prettify(oc);
  badge.className = `badge oc-${String(oc).toLowerCase()}`;
  $("elapsed-chip").textContent = vm.elapsed_s ? `${fmtTime(vm.elapsed_s)} elapsed` : "";
  const flags = vm.degradation_flags || [];
  $("degraded-banner").classList.toggle("hidden", !flags.length);
  if (flags.length) {
    const b = $("degraded-banner"); clear(b);
    b.append(icon("alert"), el("span", { text: `Some stages ran with reduced capacity (${flags.map(prettify).join(", ")}). Results are real but may be thinner than usual.` }));
  }
  buildStats($("result-stats"), { ...(vm.counters || {}), near: near.length }, "results");
  renderTop5(vm.top_5 || [], vm);
  renderNear(near);
  renderAll(rows, vm.leads || []);
  renderPlan(plan);
  renderApps(apps, vm);
  const ready = Object.values(apps).filter((a) => a.status === "ready").length;
  const counts = { top: (vm.top_5 || []).filter((c) => !c.missing).length, near: near.length, all: rows.length,
    plan: plan ? (plan.suggestions || []).length : 0, sop: ready };
  for (const [k, v] of Object.entries(counts)) $(`tc-${k}`).textContent = String(v);
  const sopTab = document.querySelector('[data-tab="sop"]');
  if (ready && activeTab !== "sop") sopTab.classList.add("pulse");
}

function renderProfile(p, box) {
  clear(box);
  const edu = p.education || {}, pr = p.preferences || {};
  const chips = [];
  if (edu.degree || edu.field) chips.push(["cap", `${edu.degree || ""} ${edu.field || ""}`.trim()]);
  if (edu.institution) chips.push(["pin", edu.institution]);
  if (edu.cgpa) chips.push(["award", `GPA ${edu.cgpa}${edu.us_gpa ? ` (≈${fmt(edu.us_gpa, 2)}/4.0)` : ""}`]);
  if (p.nationality) chips.push(["user", p.nationality]);
  if ((pr.countries || []).length) chips.push(["globe", `→ ${pr.countries.join(", ")}`]);
  if (pr.degree_level) chips.push(["target", prettify(pr.degree_level)]);
  (p.language_tests || []).forEach((t) => chips.push(["pen", `${t.test} ${t.score}`]));
  chips.forEach(([ic, t]) => box.append(el("span", { class: "chip chip-static" }, icon(ic), t)));
}

/* ── tabs ─────────────────────────────────────────────────────────────── */
function setTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.setAttribute("aria-selected", on ? "true" : "false");
    t.tabIndex = on ? 0 : -1;
    if (on) t.classList.remove("pulse");
  });
  ["top", "near", "all", "plan", "sop"].forEach((k) => $(`tab-${k}`).classList.toggle("hidden", k !== name));
}
function initTabs() {
  const tabs = [...document.querySelectorAll(".tab")];
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => setTab(t.dataset.tab));
    t.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      const n = tabs[(i + d + tabs.length) % tabs.length]; n.focus(); setTab(n.dataset.tab);
    });
  });
}

/* ── result building blocks ───────────────────────────────────────────── */
function acc(key, title, body, count) {
  const d = el("details", { class: "acc" },
    el("summary", {}, el("span", { text: title }), count != null ? el("span", { class: "pill", text: String(count) }) : null,
      icon("chevron", "chev")), el("div", { class: "acc-body" }, body));
  if (openKeys.has(key)) d.open = true;
  d.addEventListener("toggle", () => { if (d.open) openKeys.add(key); else openKeys.delete(key); });
  return d;
}
function scoreRing(score) {
  const v = clamp(Number(score) || 0, 0, 100), r = 26, C = 2 * Math.PI * r;
  const fg = svgEl("circle", { class: "ring-fg", cx: 32, cy: 32, r, fill: "none", "stroke-width": 6,
    "stroke-linecap": "round", transform: "rotate(-90 32 32)", "stroke-dasharray": C });
  fg.style.strokeDashoffset = C;
  requestAnimationFrame(() => requestAnimationFrame(() => { fg.style.strokeDashoffset = C * (1 - v / 100); }));
  const svg = svgEl("svg", { viewBox: "0 0 64 64", class: `ring ${v >= 75 ? "s-high" : v >= 55 ? "s-mid" : "s-low"}` });
  svg.append(svgEl("circle", { class: "ring-bg", cx: 32, cy: 32, r, fill: "none", "stroke-width": 6 }), fg);
  return el("div", { class: "ring-wrap", title: `Composite match score: ${fmt(v, 1)} / 100` }, svg,
    el("div", { class: "ring-num" }, el("b", { text: fmt(v) }), el("small", { text: "match" })));
}
function statusBadge(status, unverified) {
  if (unverified) return el("span", { class: "badge st-unverified" }, icon("help"), "Not verified");
  return el("span", { class: `badge st-${status || "none"}` }, icon(STATUS_ICON[status] || "help"),
    STATUS_LABEL[status] || "Not assessed");
}
function tagText(cls, ic, text) {
  return el("span", { class: `tag ${cls}`, title: text }, icon(ic), el("span", { class: "tag-text", text: truncate(text, 60) }));
}
function deadlineTag(d) {
  if (!d) return tagText("", "calendar", "Deadline not stated");
  const left = d.days_left, when = d.iso || d.text || "";
  if (d.is_past || (left != null && left < 0)) return tagText("tag-bad", "clock", `Closed ${when} — check next cycle`);
  const cls = left != null && left <= 14 ? "tag-bad" : left != null && left <= 45 ? "tag-warn" : "tag-ok";
  return tagText(cls, "clock", `${when}${left != null ? ` · ${left}d left` : ""}`);
}
function fundingText(f) {
  const bits = [];
  if (f?.amount) bits.push(String(f.amount));
  const lvl = FUNDING_LABEL[f?.level];
  if (lvl && f.level !== "UNKNOWN") bits.push(lvl);
  else if (f?.details) bits.push(String(f.details).replace(/_/g, " "));
  if (!bits.length) bits.push("Funding not stated");
  return bits.join(" · ");
}
function trustTag(t) {
  const cls = t === "OFFICIAL_INSTITUTION" || t === "RECOGNIZED_FUNDER" ? "tag-ok" : "tag-warn";
  return el("span", { class: `tag ${cls}`, title: t === "UNKNOWN" || !t ? "Not a registered official domain — check the page yourself" : "" },
    icon("shield"), TRUST_LABEL[t] || "Other website");
}
function ruleRow(r) {
  const res = String(r.result).toLowerCase();
  return el("li", { class: `rule ${res}` },
    el("span", { class: "rule-ic" }, icon(res === "met" ? "check" : res === "unmet" ? "x" : "help")),
    el("div", { class: "rule-body" },
      el("div", { class: "rule-label", text: r.label }),
      el("div", { class: "rule-vals", text: `You: ${r.candidate || "—"}  ·  Needs: ${r.required || "—"}` }),
      r.explanation ? el("div", { class: "rule-note", text: r.explanation }) : null));
}
function compBars(comps) {
  return el("div", { class: "bars" }, ...comps.map((cp) => el("div", { class: "bar-row" },
    el("div", { class: "bar-head" },
      el("span", { text: prettify(cp.component) }),
      el("span", { class: "muted small", text: `weight ${Math.round(cp.weight * 100)}%` }),
      el("b", { text: fmt(cp.score) })),
    el("div", { class: "bar" }, el("div", { class: `bar-fill ${cp.score >= 75 ? "s-high" : cp.score >= 55 ? "s-mid" : "s-low"}`,
      style: `width:${clamp(cp.score, 0, 100)}%` })),
    cp.explanation ? el("div", { class: "muted small", text: cp.explanation }) : null)));
}

/* ── top picks ────────────────────────────────────────────────────────── */
function renderTop5(cards, vm) {
  const wrap = $("top5"); clear(wrap);
  const real = cards.filter((c) => !c.missing);
  if (!real.length) {
    wrap.append(emptyState("search", running ? "Looking for matches…" : "No ranked matches yet",
      running ? "Verified opportunities will appear here the moment they are ranked."
        : "Nothing verified could be ranked for this profile. Check the “All opportunities” tab for leads."));
    return;
  }
  real.forEach((c) => wrap.append(topCard(c, vm)));
}
function topCard(c, vm) {
  const a = c.assessment || {}, rules = a.rules || [], app = (vm.applications || {})[c.id];
  const offTarget = rules.some((r) => r.rule === "destination" && String(r.result).toUpperCase() === "UNMET");
  const met = rules.filter((r) => String(r.result).toUpperCase() === "MET").length;
  const unmet = rules.filter((r) => String(r.result).toUpperCase() === "UNMET").length;
  const card = el("article", { class: `card tpick ${c.rank === 1 ? "first" : ""}` },
    el("div", { class: "tpick-head" },
      el("div", { class: `medal m${c.rank <= 3 ? c.rank : "n"}`, text: String(c.rank) }),
      el("div", { class: "tpick-id" },
        el("h3", { class: "tpick-title", text: c.name }),
        el("div", { class: "tpick-sub" }, c.provider ? el("span", { text: c.provider }) : null,
          c.country ? el("span", { class: "tag tag-geo" }, icon("pin"), c.country) : null)),
      scoreRing(a.composite_score)),
    el("div", { class: "tags" }, statusBadge(a.status), trustTag(c.trust_tier),
      tagText("tag-money", "dollar", fundingText(c.funding)),
      deadlineTag(c.deadline),
      (c.degree_levels || []).length ? el("span", { class: "tag" }, icon("cap"), c.degree_levels.join(" / ")) : null,
      offTarget ? el("span", { class: "tag tag-warn" }, icon("alert"), "Outside your target countries") : null),
    (a.strengths || []).length || (a.concerns || []).length ? el("div", { class: "fit-grid" },
      (a.strengths || []).length ? el("div", { class: "fit-col good" }, el("h4", {}, icon("check"), "Why it fits"),
        el("ul", { class: "fit-list" }, ...a.strengths.map((s) => el("li", { text: s })))) : null,
      (a.concerns || []).length ? el("div", { class: "fit-col watch" }, el("h4", {}, icon("alert"), "Watch-outs"),
        el("ul", { class: "fit-list" }, ...a.concerns.map((s) => el("li", { text: s })))) : null) : null,
    a.rationale ? el("blockquote", { class: "rationale", text: a.rationale }) : null,
    c.how_to_apply ? el("div", { class: "howto" }, el("h4", {}, icon("list"), "How to apply"),
      el("p", { text: c.how_to_apply })) : null,
    acc(`${c.id}:rules`, "Eligibility checks", el("ul", { class: "rules" }, ...rules.map(ruleRow)), `${met} met · ${unmet} unmet`),
    (a.components || []).length ? acc(`${c.id}:score`, "How the score was built", compBars(a.components)) : null,
    (c.evidence || []).length ? acc(`${c.id}:ev`, "Evidence from the official page",
      el("div", { class: "quotes" }, ...c.evidence.map((e) => el("figure", { class: "quote" },
        el("blockquote", { text: e.quote }),
        el("figcaption", {}, el("span", { class: "tag", text: prettify(e.field) }), e.url ? link(e.url, "view page", "link", "external") : null)))),
      c.evidence.length) : null,
    el("div", { class: "tpick-foot" },
      link(c.official_url, "Open official page", "btn btn-primary btn-sm", "external"),
      c.application_url && c.application_url !== c.official_url ? link(c.application_url, "Apply", "btn btn-secondary btn-sm", "external") : null,
      el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => copyText(c.official_url, "Link copied") }, icon("copy"), "Copy link"),
      app ? el("button", { class: "btn btn-secondary btn-sm", type: "button",
        onclick: () => { setTab("sop"); $("sop-" + c.id)?.scrollIntoView({ behavior: "smooth" }); } }, icon("pen"), app.status === "ready" ? "View SOP" : `SOP ${app.status}`)
        : (demoMode ? null : el("button", { class: "btn btn-secondary btn-sm", type: "button", onclick: (e) => draftSop(c.id, c.name, e.currentTarget) }, icon("pen"), "Draft SOP"))));
  return card;
}

/* ── near misses ──────────────────────────────────────────────────────── */
function renderNear(list) {
  const wrap = $("near-list"); clear(wrap);
  wrap.append(el("p", { class: "tab-intro" }, "These opportunities nearly fit. Each has a gap you may be able to close — the improvement plan shows how."));
  if (!list.length) {
    wrap.append(emptyState("trend", running ? "Still checking eligibility…" : "No near misses in this run",
      "Everything verified is either a strong fit or blocked by something that cannot be changed."));
    return;
  }
  list.forEach((n) => {
    const gaps = n.gaps.length ? n.gaps.map((g) => el("li", { class: "gap" },
      icon("alert"), el("div", {}, el("strong", { text: g.description }),
        g.remedy ? el("div", { class: "gap-fix" }, icon("bulb"), g.remedy) : null)))
      : n.gapText.map((t) => el("li", { class: "gap" }, icon("alert"), el("div", {}, el("strong", { text: t }))));
    wrap.append(el("article", { class: "card near-card" },
      el("div", { class: "near-head" },
        el("div", {}, el("h3", { class: "tpick-title", text: n.name }),
          el("div", { class: "tpick-sub" }, n.provider ? el("span", { text: n.provider }) : null,
            n.country ? el("span", { class: "tag tag-geo" }, icon("pin"), n.country) : null)),
        n.score != null ? scoreRing(n.score) : null),
      el("div", { class: "tags" }, statusBadge("near_miss"), deadlineTag(n.deadline)),
      el("h4", { class: "near-sub" }, "What is holding you back"),
      gaps.length ? el("ul", { class: "gap-list" }, ...gaps)
        : el("p", { class: "muted small", text: "No specific gap was recorded — open the official page to confirm the requirements." }),
      el("div", { class: "tpick-foot" }, link(n.url, "Open official page", "btn btn-secondary btn-sm", "external"),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => setTab("plan") }, icon("bulb"), "See improvement plan"))));
  });
}

/* ── all opportunities ────────────────────────────────────────────────── */
const FILTERS = [["all", "All"], ["fit", "Eligible or likely"], ["near_miss", "Near miss"], ["ineligible", "Ineligible"], ["unverified", "Unverified"]];
function bucket(r) {
  if (!r.verified || !r.status || r.status === "requirements_unverified") return "unverified";
  return r.status === "eligible" || r.status === "likely_eligible" ? "fit" : r.status;
}
function renderAll(rows, leads) {
  const fbox = $("filters"), box = $("other-opps"); clear(fbox); clear(box);
  const counts = { all: rows.length };
  rows.forEach((r) => { counts[bucket(r)] = (counts[bucket(r)] || 0) + 1; });
  FILTERS.forEach(([k, label]) => {
    if (k !== "all" && !counts[k]) return;
    fbox.append(el("button", { type: "button", class: `fchip ${statusFilter === k ? "on" : ""}`,
      onclick: () => { statusFilter = k; renderAll(rows, leads); } }, label, el("span", { class: "tab-count", text: String(counts[k] || 0) })));
  });
  const shown = rows.filter((r) => statusFilter === "all" || bucket(r) === statusFilter)
    .sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  if (!shown.length) box.append(emptyState("list", "Nothing here yet", "Opportunities appear as soon as they are verified."));
  shown.forEach((r) => box.append(el("div", { class: "opp-row" },
    el("div", { class: "opp-main" },
      el("div", { class: "opp-name" }, r.top ? icon("star", "top-ic") : null, r.name),
      el("div", { class: "opp-meta", text: [r.provider, r.country].filter(Boolean).join(" · ") }),
      r.reason && !/^(ineligible|near miss)$/i.test(r.reason) ? el("div", { class: "opp-reason", text: r.reason }) : null),
    el("div", { class: "opp-status" }, statusBadge(r.status, !r.verified)),
    el("div", { class: "opp-score" }, r.score != null ? [el("b", { text: fmt(r.score) }),
      el("div", { class: "mini-bar" }, el("div", { class: "mini-fill", style: `width:${clamp(r.score, 0, 100)}%` }))] : el("span", { class: "muted", text: "—" })),
    el("div", { class: "opp-deadline" }, r.deadline ? deadlineTag(r.deadline) : el("span", { class: "muted", text: "—" })),
    el("div", { class: "opp-link" }, link(r.url, "Source", "link", "external")))));
  const sec = $("leads-sec"), list = $("leads-list"); clear(list);
  sec.classList.toggle("hidden", !leads.length);
  $("leads-count").textContent = String(leads.length);
  leads.forEach((l) => list.append(el("div", { class: "lead-row" }, el("strong", { text: l.name }),
    l.provider_hint ? el("span", { class: "muted", text: ` · ${l.provider_hint}` }) : null,
    l.source_url ? link(l.source_url, "mentioned here", "link", "external") : null)));
}

/* ── improvement plan ─────────────────────────────────────────────────── */
function renderPlan(plan) {
  const box = $("improvement"); clear(box);
  if (!plan) {
    box.append(emptyState("bulb", running ? "Working out how to improve your odds…" : "No improvement plan yet",
      "Once opportunities are assessed, this tab lists concrete steps that unlock near-misses."));
    return;
  }
  if (plan.summary) box.append(el("div", { class: "plan-summary" }, icon("bulb"), el("p", { text: plan.summary })));
  const list = el("div", { class: "plan-list" }); box.append(list);
  [...(plan.suggestions || [])].sort((a, b) => a.priority - b.priority).forEach((s) => {
    const names = s.unlock_names || [];
    list.append(el("article", { class: `card plan-item p${clamp(s.priority, 1, 4)}` },
      el("div", { class: "plan-head" },
        el("span", { class: `pri pri-${s.priority}`, text: PRIORITY_LABEL[s.priority] || `P${s.priority}` }),
        s.category ? el("span", { class: "tag", text: prettify(s.category) }) : null,
        s.deadline_hint ? tagText("tag-warn", "clock", /^\d+ days?$/.test(s.deadline_hint) ? `Nearest deadline in ${s.deadline_hint}` : s.deadline_hint) : null),
      el("h3", { class: "plan-title", text: s.title }),
      el("p", { class: "plan-action", text: s.action }),
      s.rationale ? el("p", { class: "plan-why muted", text: s.rationale }) : null,
      names.length ? el("div", { class: "unlocks" }, el("span", { class: "muted small", text: `Unlocks ${plural(names.length, "opportunity", "opportunities")}:` }),
        ...names.slice(0, 4).map((n) => el("span", { class: "chip chip-static", text: n })),
        names.length > 4 ? el("span", { class: "muted small", text: `+${names.length - 4} more` }) : null) : null));
  });
}

/* ── statements of purpose ────────────────────────────────────────────── */
function renderApps(apps, vm) {
  const box = $("sop-panel"); clear(box);
  const entries = Object.entries(apps);
  if (!entries.length) {
    const top = (vm.top_5 || []).find((c) => !c.missing);
    box.append(emptyState("pen", running ? "Your statement is being drafted…" : "No statement of purpose yet",
      "A grounded SOP is written for your best match. Every claim is traced to your profile or the opportunity page.",
      !running && !demoMode && top ? el("button", { class: "btn btn-primary", type: "button",
        onclick: (e) => draftSop(top.id, top.name, e.currentTarget) }, icon("pen"), `Draft SOP for ${top.name}`) : null));
    return;
  }
  for (const [oid, app] of entries) {
    const opp = (vm.top_5 || []).find((c) => c.id === oid), name = opp ? opp.name : "Selected opportunity";
    if (app.status !== "ready") {
      box.append(el("section", { class: "card sop-wrap", id: `sop-${oid}` }, el("h3", { text: name }),
        el("p", { class: "muted", text: `SOP ${app.status}${app.error ? ": " + app.error : ""}` })));
      continue;
    }
    const paras = (app.sop_text || "").split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
    const body = paras.length > 1 ? paras : (app.sop_text || "").split(/\n/).filter(Boolean);
    box.append(el("section", { class: "card sop-wrap", id: `sop-${oid}` },
      el("div", { class: "sop-head" }, el("div", {}, el("span", { class: "eyebrow-sm", text: "Statement of purpose" }), el("h3", { text: name })),
        el("div", { class: "sop-tools" },
          el("button", { class: "btn btn-secondary btn-sm", type: "button", onclick: () => copyText(app.sop_text, "SOP copied") }, icon("copy"), "Copy"),
          demoMode ? el("button", { class: "btn btn-secondary btn-sm", type: "button", onclick: () => downloadText(`${name}.md`, `# ${name}\n\n${app.sop_text}\n`) }, icon("download"), "Download .md")
            : el("a", { class: "btn btn-secondary btn-sm", href: `/api/runs/${runId}/sop/${oid}/download` }, icon("download"), "Download .md"),
          el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => printSop(oid) }, icon("printer"), "Print"))),
      el("div", { class: "sop-meta" }, el("span", { class: "tag", text: `${app.word_count} words` }),
        el("span", { class: "tag tag-ok" }, icon("shield"), `${plural((app.claim_ledger || []).length, "claim")} traced to a source`),
        (app.removed_claims || []).length ? el("span", { class: "tag tag-warn" }, icon("alert"), `${plural(app.removed_claims.length, "unverified claim")} removed`) : null),
      /\[[A-Z_]{3,}\]/.test(app.sop_text || "") ? el("p", { class: "tab-intro small" }, "Highlighted placeholders are filled automatically when you enter your display name in the Identity section.") : null,
      el("div", { class: "sop-doc" }, ...body.map(sopPara)),
      (app.execution_checklist || []).length ? acc(`${oid}:check`, "Application checklist",
        el("ul", { class: "checklist" }, ...app.execution_checklist.map((t) => el("li", {}, icon("check"), t)))) : null,
      (app.claim_ledger || []).length ? acc(`${oid}:ledger`, "Claims and where they come from",
        el("ul", { class: "ledger" }, ...app.claim_ledger.map((e) => el("li", {},
          el("code", { text: e.fact_id }), el("span", { text: ` ${prettify(e.field)}: ${e.value}` }),
          el("span", { class: `badge src-${e.source}`, text: e.source })))), app.claim_ledger.length) : null,
      (app.removed_claims || []).length ? acc(`${oid}:removed`, "Claims removed because they could not be verified",
        el("ul", { class: "removed" }, ...app.removed_claims.map((c) => el("li", { text: c }))), app.removed_claims.length) : null));
  }
}
function sopPara(text) {
  const p = el("p");
  text.split(/(\[[A-Z_]{3,}\])/).forEach((part) => {
    if (/^\[[A-Z_]{3,}\]$/.test(part)) p.append(el("mark", { class: "ph", text: part }));
    else if (part) p.append(document.createTextNode(part));
  });
  return p;
}
function printSop(oid) {
  const sec = $("sop-" + oid); if (!sec) return;
  sec.classList.add("print-target"); document.body.classList.add("printing-sop");
  window.addEventListener("afterprint", () => {
    sec.classList.remove("print-target"); document.body.classList.remove("printing-sop");
  }, { once: true });
  window.print();
}
function emptyState(ic, title, sub, action) {
  return el("div", { class: "empty" }, el("span", { class: "empty-ic" }, icon(ic)),
    el("div", { class: "empty-title", text: title }), el("div", { class: "empty-sub", text: sub }), action);
}
async function copyText(text, msg) {
  try { await navigator.clipboard.writeText(text); toast(msg, "success", 2200); }
  catch { toast("Copy failed — select the text manually.", "error"); }
}
function downloadText(name, text) {
  const a = el("a", { href: URL.createObjectURL(new Blob([text], { type: "text/markdown" })),
    download: name.replace(/[^\w.-]+/g, "_") });
  document.body.append(a); a.click(); a.remove();
}

/* ── on-demand SOP ────────────────────────────────────────────────────── */
async function draftSop(oppId, name, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Drafting…"; }
  feed("application_strategist", `Drafting an SOP for ${name}…`, "");
  try {
    const r = await fetch(`/api/runs/${runId}/sop`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ opportunity_id: oppId }) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    toast(`Drafting your statement for ${name}…`);
    let tries = 0;
    const poll = setInterval(async () => {
      tries++;
      const s = await fetch(`/api/runs/${runId}/sop/${oppId}`).then((x) => x.json()).catch(() => null);
      if (s && s.status !== "generating") {
        clearInterval(poll);
        toast(s.status === "ready" ? "Your statement is ready." : `SOP ${s.status}`, s.status === "ready" ? "success" : "error");
        await refreshView(true); setTab("sop");
      } else if (tries > 80) { clearInterval(poll); toast("The SOP is taking long — refresh in a moment.", "error"); if (btn) btn.disabled = false; }
    }, 1500);
  } catch (e) { toast("SOP request failed: " + e.message, "error"); if (btn) { btn.disabled = false; btn.textContent = "Draft SOP"; } }
}

/* ── cancel & clear ───────────────────────────────────────────────────── */
async function cancelRun(silent) {
  if (!runId) return;
  await fetch(`/api/runs/${runId}/cancel`, { method: "POST" }).catch(() => {});
  if (!silent) { feed("system", "Cancelled — partial results are kept", "warn"); onDone(); }
}
async function clearData() {
  if (!confirm("Delete your saved form and this run's data from the server?")) return;
  if (runId) await fetch(`/api/runs/${runId}`, { method: "DELETE" }).catch(() => {});
  try { localStorage.removeItem(LS_KEY); } catch {}
  location.reload();
}

/* ── boot ─────────────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", async () => {
  applyTheme(document.documentElement.getAttribute("data-theme") || "dark");
  const dl = el("datalist", { id: "dl-countries" }, ...COUNTRIES.map((c) => el("option", { value: c })));
  document.body.append(dl);
  document.querySelectorAll("input[data-chips]").forEach(initChips);
  initDropzone(); initTabs(); addTestRow(); loadForm(); updateMeter(); setTab("top");
  $("profile-form").addEventListener("input", () => { saveForm(); updateMeter(); });
  $("profile-form").addEventListener("submit", startRun);
  $("btn-add-test").addEventListener("click", () => addTestRow());
  $("btn-sample-rahul").addEventListener("click", () => loadSample("rahul"));
  $("btn-sample-amara").addEventListener("click", () => loadSample("amara"));
  $("btn-demo").addEventListener("click", () => loadDemo("rahul"));
  $("btn-demo-exit").addEventListener("click", () => backToForm(true));
  $("btn-cancel").addEventListener("click", () => (running ? cancelRun() : backToForm(true)));
  $("btn-new").addEventListener("click", () => backToForm());
  $("btn-clear").addEventListener("click", clearData);
  $("btn-theme").addEventListener("click", toggleTheme);
  const pill = $("status-pill");
  try {
    const h = await fetch("/api/health").then((r) => r.json());
    if (h.access_code_required) show("access-row");
    const bad = h.llm_available === false, noSearch = h.search === false;
    pill.className = `status-pill ${bad ? "bad" : noSearch ? "warn" : "ok"}`;
    $("status-text").textContent = bad ? "No AI model configured" : noSearch ? "Search not configured" : "Ready";
    pill.title = `Models: ${(h.providers || []).join(", ") || "none"} · Search: ${(h.search_providers || []).join(", ") || "none"}`;
    if (bad) setMsg("Warning: no LLM provider is configured on the server.");
  } catch { pill.className = "status-pill bad"; $("status-text").textContent = "Server unreachable"; }
  const q = new URLSearchParams(location.search);
  if (q.get("demo")) await loadDemo(q.get("demo") === "1" ? "rahul" : q.get("demo"));
  else if (q.get("sample")) await loadSample(q.get("sample"));
  if (q.get("tab")) setTab(q.get("tab"));
});
