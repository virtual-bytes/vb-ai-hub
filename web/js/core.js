const COLS = { ideas: ["Ideas", "Backlog and plans"], active: ["Active", "In progress"], needs_me: ["Needs you", "Waiting on your reply"], done: ["Done", ""] };
const SRC = { code: "Claude Code", chat: "claude.ai chat", project: "claude.ai project", owui: "Open WebUI", manual: "Manual", "inference-console": "Inference Console", task: "Task", "project-doc": "Project", chatgpt: "ChatGPT", grok: "Grok", cursor: "Cursor", codex: "Codex" };
const srcClass = s => ({ code: "code", chat: "chat", project: "chat", owui: "owui" }[s] || "other");
const srcLabel = s => SRC[s] || s;
const KIND = { handoff: "Handoff", doc: "Doc", upload: "Upload", transcript: "Transcript", digest: "Digest" };
const PSTATUS = { active: "Active", paused: "Paused", done: "Done", archived: "Archived" };
const PRIO = { urgent: "Urgent", high: "High", normal: "Normal", low: "Low" };
const PRIO_ORDER = { urgent: 0, high: 1, normal: 2, low: 3 };
const todayStr = () => new Date().toISOString().slice(0, 10);
function dueClass(d, done) { if (!d || done) return ""; const t = todayStr(); if (d < t) return "over"; const soon = new Date(Date.now() + 3 * 864e5).toISOString().slice(0, 10); return d <= soon ? "soon" : ""; }
function dueLabel(d) { if (!d) return ""; const t = todayStr(); if (d === t) return "Due today"; if (d < t) return `Overdue · ${d}`; return `Due ${d}`; }
const DONE_LIMIT = 40;
const state = { cards: new Map(), columns: Object.keys(COLS), settings: {}, projects: [], tasks: [], libProjects: [],
  src: "all", project: "", q: "", view: "board", openId: null, openKind: "card", tab: "details", showAllDone: false,
  boardMode: "columns", boardCol: "needs_me", closedGroups: new Set(["done"]), libProjectId: null, libData: null, libMode: "project", selecting: false, selected: new Set(), jobs: {}, taskFilter: { project: "", done: false } };
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function store(k, v) { try { v === undefined ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} }
function load(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } }
function applyTheme(t) { t ? document.documentElement.dataset.theme = t : delete document.documentElement.dataset.theme; }
applyTheme(load("kb.theme"));

// ---------- api
function authHeaders() { const t = load("kb.token"); return t ? { Authorization: "Bearer " + t } : {}; }
async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...authHeaders(), ...(opts.headers || {}) } });
  if (res.status === 401) { signIn(); throw new Error("unauthorized"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
  return res.json();
}
const post = (p, b, m = "POST") => api(p, { method: m, body: JSON.stringify(b) });
async function fetchBlob(path) { const res = await fetch(path, { headers: authHeaders() }); if (!res.ok) throw new Error(res.statusText); return res.blob(); }
async function download(path, name) {
  try {
    const url = URL.createObjectURL(await fetchBlob(path));
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) { toast("Download failed: " + e.message); }
}
const enc = id => encodeURIComponent(id);
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 2800); }

const fullDate = ts => ts ? new Date(ts * 1000).toLocaleString() : "";
const size = n => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";
const tok = n => !n ? "0" : n < 1000 ? String(n) : n < 1e6 ? (n / 1000).toFixed(n < 1e4 ? 1 : 0) + "k" : n < 1e9 ? (n / 1e6).toFixed(n < 1e7 ? 2 : 1) + "M" : (n / 1e9).toFixed(2) + "B";
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {  // the clipboard API only exists on https and localhost, and a default install is plain http
    const box = Object.assign(document.createElement("textarea"), { value: text });
    box.style.cssText = "position:fixed;opacity:0";
    ($("dialog[open]") || document.body).append(box); box.select();
    const done = document.execCommand("copy"); box.remove();
    if (!done) return toast("Couldn't copy. Select the text and copy it yourself");
  }
  toast("Copied");
}
const shq = s => /^[\w@%+=:,./~-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`;
const projById = id => state.projects.find(p => p.id === id);
const projByName = n => state.projects.find(p => p.name === n);
const tasksOf = name => { const p = projByName(name); return p ? state.tasks.filter(t => t.project_id === p.id) : []; };
const taskById = id => state.tasks.find(t => t.id === id);
