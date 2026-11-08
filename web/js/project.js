// ---------- project workspace
const RULE_LABEL = { title: "Title contains", path: "Folder starts with", source_project: "Source project is" };
const PROJECT_TABS = { overview: "Overview", status: "Status", docs: "Documents", work: "Work", activity: "Activity", settings: "Settings" };
const DOC_FILTERS = { all: "All", handoff: "Handoffs", digest: "Digests", doc: "Docs", upload: "Uploads" };
const HANDOFF_PREVIEW = 1400;
Object.assign(state, { libTab: PROJECT_TABS[load("kb.libTab")] ? load("kb.libTab") : "overview", libDocs: "all", statusMetric: "total" });
const handoffText = new Map();
let statusCache = null;

const editingProject = () => $("#pBody")?.contains(document.activeElement) && document.activeElement.matches("input, textarea, select");
function setProjectTab(tab) { state.libTab = tab; store("kb.libTab", tab); $("#libMain").scrollTop = 0; renderLibraryMain(); }
const firstLine = s => String(s || "").trim().split("\n")[0].slice(0, 160);
const openTasks = d => d.tasks.filter(t => t.status !== "done");
const dueOver = t => t.due && dueClass(t.due, t.status === "done") === "over";
const latestHandoff = d => d.docs.find(x => x.kind === "handoff" && (x.path.startsWith("generated:") || /handoff/i.test(x.name))) || d.docs.find(x => x.kind === "handoff");
function handoffPreview(t) {
  if (t.length <= HANDOFF_PREVIEW) return t;
  const lines = t.slice(0, HANDOFF_PREVIEW).split("\n").slice(0, -1);
  while (lines.length && /^\s*(#|$)/.test(lines.at(-1))) lines.pop();
  return lines.join("\n");
}
function lastActivity(d) { return Math.max(d.project.updated_at || 0, ...d.cards.map(c => c.last_activity || 0)); }

async function renderLibraryMain() {
  const main = $("#libMain"), pid = state.libProjectId;
  if (!pid) return renderLibraryHome();
  let d; try { d = await api(`/api/projects/${enc(pid)}`); } catch (e) { main.innerHTML = `<div class="notice">${esc(e.message)}</div>`; return; }
  if (state.libProjectId !== pid || state.q || state.libMode !== "project") return;
  state.libData = d;
  for (const c of d.cards) state.cards.set(c.id, c);
  if (state.reviewPid === pid) return renderReview(d);
  state.reviewPid = null;
  const p = d.project, tab = state.libTab, keep = main.scrollTop;
  const counts = { docs: d.docs.length, work: d.cards.length };
  main.innerHTML = `<div class="pw">
    <div class="ph">
      <div class="ttl"><h2>${esc(p.name)}</h2><div class="sub">${p.description ? esc(p.description) + " " : ""}${PSTATUS[p.status]}, last activity ${ago(lastActivity(d))}.</div></div>
      ${d.draft ? `<button class="btn small primary" id="pReview">Review draft</button>` : genBtn("Generate handoff", "handoff", "project", p.id, "primary")}
      ${genBtn("Weekly digest", "digest", "project", p.id)}
    </div>
    <div class="tabs" role="tablist" aria-label="Project sections">${Object.entries(PROJECT_TABS).map(([id, label]) =>
      `<button role="tab" data-ptab="${id}" aria-selected="${id === tab}">${label}${counts[id] ? ` <span class="arch">${counts[id]}</span>` : ""}</button>`).join("")}</div>
    <div id="pBody">${PROJECT_VIEWS[tab](d)}</div></div>`;
  main.scrollTop = keep;
  $$("[data-ptab]", main).forEach(b => b.onclick = () => setProjectTab(b.dataset.ptab));
  $$("[data-go]", main).forEach(b => b.onclick = () => setProjectTab(b.dataset.go));
  if (d.draft) $("#pReview").onclick = () => { state.reviewPid = pid; renderLibraryMain(); };
  PROJECT_BINDS[tab]?.(d);
  for (const id in state.jobs) { const j = state.jobs[id]; const b = $(`[data-gen="${j.kind}|${j.scope}|${CSS.escape(j.target)}"]`); if (b) { b.dataset.busy = "1"; b.textContent = "Generating…"; state.jobs[id].btn = b; } }
}

// ---------- library home: every project, grouped by what it needs from you
const WALL_GROUPS = [["needs", "Needs you"], ["moving", "Moving"], ["quiet", "Quiet"]];
function wallFlag(p) {
  const days = p.last_handoff ? Math.floor((Date.now() / 1000 - p.last_handoff) / 86400) : 0;
  return p.overdue ? `<span class="flag"><span class="dot bad"></span>${p.overdue} overdue task${p.overdue === 1 ? "" : "s"}</span>`
    : p.unrecorded ? `<span class="flag warn"><span class="dot waiting"></span>${p.last_handoff ? `No handoff in ${days} days` : "No handoff yet"}</span>` : "";
}
async function loadAttention() {
  try { state.overview = (await api(`/api/library/overview?today=${localDay()}`)).projects; } catch { return; }
  renderPicker();
}
async function renderLibraryHome() {
  await loadAttention();
  const main = $("#libMain"), list = state.overview || [];
  if (state.libProjectId || state.libMode !== "project" || state.q) return;
  main.innerHTML = !list.length ? `<div class="empty">The library fills up as sessions run, chats sync and you archive cards.</div>` : `<div class="pw">${WALL_GROUPS.map(([id, name]) => {
    const rows = list.filter(p => p.group === id); if (!rows.length) return "";
    return `<h3 class="wall-h">${name} <span class="count">${rows.length}</span></h3><div class="wall">${rows.map(p => `<button class="wall-card" data-wall="${esc(p.id)}">
      <span class="top"><span class="n">${esc(p.name)}</span><span class="arch">${ago(p.last_activity)}</span></span>
      <span class="step${p.next_step ? "" : " none"}">${esc(p.next_step || (p.last_handoff ? "No next step listed" : "No handoff yet"))}</span>
      <span class="facts"><span>${p.open_tasks ? `${p.open_tasks} open task${p.open_tasks === 1 ? "" : "s"}` : "No open tasks"}</span><span>${p.docs} document${p.docs === 1 ? "" : "s"}</span>${wallFlag(p)}</span></button>`).join("")}</div>`;
  }).join("")}</div>`;
  $$("[data-wall]", main).forEach(b => b.onclick = () => pickProject(b.dataset.wall));
}

const PROJECT_VIEWS = {
  overview(d) {
    const p = d.project, u = d.usage, handoff = latestHandoff(d), tasks = openTasks(d);
    return `<div class="pw-cols">
      <div class="wide">
        ${handoff ? `<section class="pcard">
          <div class="pcard-h"><b>Latest handoff</b><span class="sub">${ago(handoff.created_at)}${handoff.versions > 1 ? `, ${handoff.versions} versions` : ""}</span></div>
          <div class="pcard-b"><div class="md" id="pHandoff"><div class="sub">Loading…</div></div>
            <div class="row" style="margin-top:12px"><button class="btn small" data-act="doc" data-owner="${esc(handoff.card_id)}" data-path="${esc(handoff.path)}">Read in full</button><button class="btn small" id="pSteps">Add next steps as tasks</button><button class="btn link" data-go="docs">All documents</button></div></div>
        </section>` : `<div class="empty">No handoff yet. Generate one to sum up where this project stands.</div>`}
        <h3>Recent work</h3>
        ${d.worklog.slice(0, 5).map(w => `<a class="prow" href="#" data-card="${esc(w.card_id)}"><span class="t"><span class="n">${esc(w.card_title)}</span><span class="sub">${esc(firstLine(w.text))}</span></span><span class="arch">${ago(w.at)}</span></a>`).join("") || `<div class="empty">No work log entries yet.</div>`}
      </div>
      <div class="side">
        <h3>Open tasks</h3>
        ${tasks.slice(0, 4).map(taskRow).join("") || `<div class="sub">No open tasks.</div>`}
        ${tasks.length > 4 ? `<button class="btn link" data-go="work">All ${tasks.length} tasks</button>` : ""}
        ${p.goals ? `<h3>Goals</h3><p class="sub pre">${esc(p.goals)}</p>` : ""}
        ${p.links.length ? `<h3>Links</h3>${p.links.filter(l => safeUrl(l.url)).map(l => `<a class="plink" href="${safeHref(l.url)}" target="_blank" rel="noopener">${esc(l.label || l.url)}</a>`).join("")}` : ""}
        <h3>This project so far</h3>
        <dl class="pfacts"><dt>Items</dt><dd>${d.cards.length}</dd><dt>Documents</dt><dd>${d.docs.length}</dd><dt>Commits and MRs</dt><dd>${d.links.length}</dd>
          ${u.output ? `<dt>Tokens in</dt><dd>${tok(u.input + u.cache_write + u.cache_read)}</dd><dt>Tokens out</dt><dd>${tok(u.output)}</dd>` : ""}</dl>
      </div></div>`;
  },
  status() { return `<div id="pStat">${statusCache?.pid === state.libProjectId ? "" : `<div class="sub" style="margin-top:18px">Loading…</div>`}</div>`; },
  docs(d) {
    const shown = state.libDocs === "all" ? d.docs : d.docs.filter(x => x.kind === state.libDocs);
    return `<div class="row pw-bar"><span class="seg" role="group" aria-label="Kind">${Object.entries(DOC_FILTERS).map(([id, label]) => `<button data-docs="${id}" aria-pressed="${id === state.libDocs}">${label}</button>`).join("")}</span>
        <span class="spacer"></span><button class="btn small" id="pUpload">Upload</button></div>
      ${shown.length ? `<div class="t-wrap"><table class="list"><thead><tr><th>Name</th><th>From</th><th>Kind</th><th>Updated</th></tr></thead><tbody>
        ${shown.map(x => `<tr class="click" data-act="doc" data-owner="${esc(x.card_id)}" data-path="${esc(x.path)}"><td><div>${esc(x.name)}</div><div class="arch mono">${esc(x.path.startsWith("generated:") ? "" : x.path !== x.name ? x.path : "")}</div></td>
          <td>${esc(x.card_id.startsWith("project:") ? "Project" : x.card_id.startsWith("task:") ? "Task: " + x.owner_title : x.owner_title)}</td><td><span class="tag plain">${KIND[x.kind] || esc(x.kind)}</span>${x.versions > 1 ? ` <span class="arch">${x.versions} versions</span>` : ""}</td><td>${ago(x.created_at)}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty">${d.docs.length ? "Nothing of this kind yet." : "No documents in this project yet."}</div>`}`;
  },
  work(d) {
    const byTask = id => d.cards.filter(c => c.task_id === id), unassigned = d.cards.filter(c => !c.task_id);
    return `<h3>Tasks <button class="btn small" id="tNew">New task</button></h3>
    ${d.tasks.length ? d.tasks.map(t => { const cs = byTask(t.id); return `<div class="task${t.status === "done" ? " done" : ""}"><div class="task-h" data-act="task" data-task="${esc(t.id)}">
        <span class="t">${esc(t.title)}</span>${t.priority !== "normal" ? `<span class="prio ${t.priority}">${PRIO[t.priority]}</span>` : ""}${t.due ? `<span class="due ${dueClass(t.due, t.status === "done")}">${dueLabel(t.due)}</span>` : ""}<span class="arch">${cs.length} item${cs.length === 1 ? "" : "s"} · ${ago(t.updated_at)}</span>${genBtn("Handoff", "handoff", "task", t.id)}</div></div>`; }).join("")
      : `<div class="empty">Tasks group the sessions and chats that belong to one piece of work.</div>`}
    <h3>Items${unassigned.length && d.tasks.length ? ` <span class="arch">${unassigned.length} without a task</span>` : ""}</h3>
    <div class="t-wrap"><table class="list"><thead><tr><th>Title</th><th>Source</th><th>Task</th><th>State</th><th>Library</th><th class="num">Tokens out</th><th>Last activity</th></tr></thead><tbody>
    ${d.cards.map(c => `<tr class="click" data-card="${esc(c.id)}"><td>${esc(c.title)}</td><td><span class="tag ${srcClass(c.source)}">${esc(srcLabel(c.source))}</span></td>
      <td>${c.task_id && taskById(c.task_id) ? esc(taskById(c.task_id).title) : `<span class="arch">-</span>`}</td>
      <td>${c.archived_at ? `<span class="tag plain">Archived</span>` : esc(COLS[c.column]?.[0] || c.column)}</td><td>${archBits(c) || `<span class="arch">-</span>`}</td>
      <td class="num">${c.usage?.output ? tok(c.usage.output) : `<span class="arch">-</span>`}</td><td title="${esc(fullDate(c.last_activity))}">${ago(c.last_activity)}</td></tr>`).join("") || `<tr><td colspan="7" class="sub">No items. Add rules in Settings or set a card's project.</td></tr>`}
    </tbody></table></div>
    <h3>Commits and merge requests</h3>
    ${linksHTML(d.links.slice(0, 30))}`;
  },
  activity(d) {
    const rows = [
      ...d.worklog.map(w => ({ at: w.at, kind: "Work log", html: `<a href="#" data-card="${esc(w.card_id)}">${esc(w.card_title)}</a><span class="sub">${esc(firstLine(w.text))}</span>` })),
      ...d.links.map(l => ({ at: l.at, kind: l.kind === "commit" ? "Commit" : l.kind === "mr" ? "MR" : "PR", html: safeUrl(l.url) ? `<a href="${safeHref(l.url)}" target="_blank" rel="noopener">${esc(l.title || l.key)}</a>` : esc(l.title || l.key) })),
      ...d.docs.map(x => ({ at: x.created_at, kind: KIND[x.kind] || x.kind, html: `<button class="btn link" data-act="doc" data-owner="${esc(x.card_id)}" data-path="${esc(x.path)}">${esc(x.name)}</button>` })),
    ].sort((a, b) => b.at - a.at).slice(0, 60);
    return rows.length ? `<div class="pw-list">${rows.map(r => `<div class="arow"><span class="arch" title="${esc(fullDate(r.at))}">${ago(r.at)}</span><span class="t">${r.html}</span><span class="arch">${esc(r.kind)}</span></div>`).join("")}</div>`
      : `<div class="empty" style="margin-top:18px">Nothing has happened in this project yet.</div>`;
  },
  settings(d) {
    const p = d.project;
    return `<div class="pw-form">
      <div class="grid2">
        <label class="field">Name<input class="input" id="pName" value="${esc(p.name)}"></label>
        <label class="field">Status<select class="input" id="pStatus">${Object.entries(PSTATUS).map(([k, v]) => `<option value="${k}" ${k === p.status ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      </div>
      <label class="field" style="margin-top:14px">Description<textarea class="short" id="pDesc" placeholder="What this project is">${esc(p.description)}</textarea></label>
      <label class="field" style="margin-top:14px">Goals<textarea class="short" id="pGoals" placeholder="Outcomes, milestones, definition of done">${esc(p.goals)}</textarea></label>
      <label class="switch" style="margin-top:12px"><input type="checkbox" id="pContext" ${p.context ? "checked" : ""}> Give new Claude Code sessions in this project its handoff, open tasks and recent work</label>
      <h3>Links <button class="btn small" id="pAddLink">Add</button></h3>
      <div class="links-list" id="pLinks">${p.links.map((l, i) => `<div class="lk"><input class="input" data-li="${i}" data-k="label" value="${esc(l.label)}" placeholder="Label"><input class="input" data-li="${i}" data-k="url" value="${esc(l.url)}" placeholder="https://"><a class="btn small" href="${safeHref(l.url)}" target="_blank" rel="noopener">Open</a><button class="btn small" data-rmlink="${i}">Remove</button></div>`).join("") || `<div class="sub">Repo, staging URL, docs, tickets…</div>`}</div>
      <h3>Matching rules</h3>
      <div class="sub" style="margin-bottom:8px">New and existing items that match land in this project automatically, unless you set an item's project by hand.</div>
      <div class="chips">${p.rules.map(r => `<span class="rule"><span class="f">${RULE_LABEL[r.field]}</span> <b>${esc(r.pattern)}</b><button title="Remove rule" data-rmrule="${r.id}">×</button></span>`).join("") || `<span class="sub">No rules yet.</span>`}</div>
      <div class="row" style="margin-top:8px;align-items:flex-end">
        <label class="field" style="flex:0 0 190px">Match<select class="input" id="rField"><option value="title">Title contains</option><option value="path">Folder starts with</option><option value="source_project">Source project is</option></select></label>
        <label class="field">Pattern<input class="input" id="rPattern" placeholder="e.g. my-app, /home/me/projects/my-app"></label>
        <button class="btn" id="rAdd">Add rule</button>
      </div>
      <h3>Move or export</h3>
      <div class="row"><button class="btn small" id="pExport">Export (.zip)</button><button class="btn small" id="pMerge">Merge into…</button></div>
    </div>`;
  },
};
function taskRow(t) {
  const meta = [t.priority !== "normal" ? PRIO[t.priority] : "", t.due ? dueLabel(t.due) : ""].filter(Boolean).join(", ") || "No due date";
  return `<button class="prow stack" data-act="task" data-task="${esc(t.id)}"><span class="n">${esc(t.title)}</span><span class="sub ${dueClass(t.due, false)}">${esc(meta)}</span></button>`;
}

const PROJECT_BINDS = {
  overview: d => { const h = latestHandoff(d); if (h) { fillHandoff(h); $("#pSteps").onclick = () => addNextSteps(d.project); } },
  status: () => { paintStatus(); loadStatus(); },
  docs(d) {
    $$("[data-docs]").forEach(b => b.onclick = () => { state.libDocs = b.dataset.docs; renderLibraryMain(); });
    $("#pUpload").onclick = () => { $("#uploadInput").dataset.owner = `project:${d.project.id}`; $("#uploadInput").click(); };
  },
  work(d) { $("#tNew").onclick = async () => { try { if (await newTaskDialog(d.project.id)) { await refresh(); renderLibraryMain(); } } catch (e) { toast(e.message); } }; },
  settings(d) {
    const p = d.project;
    const patch = async b => { try { await post(`/api/projects/${enc(p.id)}`, b, "PATCH"); toast("Saved"); await refresh(); await refreshLibrary(); renderLibraryMain(); } catch (e) { toast(e.message); } };
    $("#pName").onchange = async e => {
      const n = e.target.value.trim(); if (!n) { e.target.value = p.name; return; }
      const other = projByName(n);
      if (other && other.id !== p.id && !(await confirmUI("Merge projects?", `A project named <b>${esc(n)}</b> already exists. Merge <b>${esc(p.name)}</b> into it? Its items, tasks, documents and rules move there.`, "Merge", true))) { e.target.value = p.name; return; }
      patch({ name: n });
    };
    $("#pStatus").onchange = e => patch({ status: e.target.value });
    $("#pContext").onchange = e => patch({ context: e.target.checked });
    $("#pDesc").onchange = e => patch({ description: e.target.value });
    $("#pGoals").onchange = e => patch({ goals: e.target.value });
    const links = () => p.links.map((l, i) => ({ label: $(`[data-li="${i}"][data-k="label"]`)?.value ?? l.label, url: $(`[data-li="${i}"][data-k="url"]`)?.value ?? l.url }));
    $$("[data-li]").forEach(inp => inp.onchange = () => patch({ links: links() }));
    $$("[data-rmlink]").forEach(b => b.onclick = () => patch({ links: links().filter((_, i) => i !== +b.dataset.rmlink) }));
    $("#pAddLink").onclick = async () => {
      const r = await dialog({ title: "Add link", fields: [{ name: "url", label: "URL", type: "url", required: true, placeholder: "https://" }, { name: "label", label: "Label (optional)", placeholder: "Repo, staging, docs…" }], ok: "Add link" });
      if (!r || !r.url) return;
      patch({ links: [...links(), { label: r.label || r.url.replace(/^https?:\/\//, "").split("/")[0], url: r.url }] });
    };
    $$("[data-rmrule]").forEach(b => b.onclick = async () => { try { const r = await api(`/api/rules/${b.dataset.rmrule}`, { method: "DELETE" }); toast(`Rule removed${r.moved ? `; ${r.moved} items moved` : ""}`); await refresh(); refreshLibrary(); } catch (e) { toast(e.message); } });
    $("#rAdd").onclick = async () => {
      const pattern = $("#rPattern").value.trim(); if (!pattern) return toast("Enter a pattern");
      try { const r = await post("/api/rules", { project_id: p.id, field: $("#rField").value, pattern }); toast(`Rule added; ${r.moved} item${r.moved === 1 ? "" : "s"} moved here`); await refresh(); refreshLibrary(); } catch (e) { toast(e.message); }
    };
    $("#pExport").onclick = async () => {
      const r = await dialog({ title: `Export ${p.name}`, message: "Downloads a zip with the project overview, and each item's documents (with versions), work log, file list and commits.",
        fields: [{ name: "transcripts", label: "Include full transcripts (much larger)", type: "checkbox" }], ok: "Download" });
      if (!r) return;
      toast("Preparing the export…"); download(`/api/projects/${enc(p.id)}/bundle${r.transcripts ? "?transcripts=1" : ""}`, p.name.replace(/[^\w.-]+/g, "_") + ".zip");
    };
    $("#pMerge").onclick = async () => {
      const opts = projectOptions(p.id); if (!opts.length) return toast("There's no other project to merge into");
      const r = await dialog({ title: `Merge ${p.name}`, message: `Everything in <b>${esc(p.name)}</b> (items, tasks, documents, rules) moves into the project you pick, and <b>${esc(p.name)}</b> is removed. Future items from the same source follow automatically.`,
        fields: [{ name: "into", label: "Merge into", type: "select", options: opts }], ok: "Merge", danger: true });
      if (!r) return;
      const target = projById(r.into); if (!target) return;
      post(`/api/projects/${enc(p.id)}/merge`, { into: target.id }).then(async () => { toast(`Merged into ${target.name}`); state.libProjectId = target.id; await refresh(); refreshLibrary(); }).catch(e => toast(e.message));
    };
  },
};

async function addNextSteps(p) {
  let r; try { r = await api(`/api/projects/${enc(p.id)}/next-steps`); } catch (e) { return toast(e.message); }
  const fresh = r.steps.filter(s => !s.exists);
  if (!r.steps.length) return toast("This handoff doesn't list any next steps");
  if (!fresh.length) return toast("Every next step is already a task");
  const picked = await dialog({ title: "Add next steps as tasks", ok: "Add tasks", message: `From <b>${esc(r.handoff.name)}</b>.${r.steps.length > fresh.length ? ` ${r.steps.length - fresh.length} already ${r.steps.length - fresh.length === 1 ? "is a task" : "are tasks"}.` : ""}`,
    fields: fresh.map((s, i) => ({ name: `s${i}`, label: s.title, type: "checkbox", value: true })) });
  if (!picked) return;
  const titles = fresh.filter((_, i) => picked[`s${i}`]).map(s => s.title);
  try { for (const title of titles) await post("/api/tasks", { project_id: p.id, title, priority: "normal", due: "" }); } catch (e) { return toast(e.message); }
  if (titles.length) toast(`Added ${titles.length} task${titles.length === 1 ? "" : "s"}`);
  await refresh(); renderLibraryMain();
}
async function fillHandoff(h) {
  const show = t => { const el = $("#pHandoff"); if (el) el.innerHTML = md(handoffPreview(t)); };
  if (handoffText.has(h.sha)) return show(handoffText.get(h.sha));
  try { const t = await (await fetchBlob(`/api/blobs/${h.sha}`)).text(); handoffText.set(h.sha, t); show(t); }
  catch (e) { const el = $("#pHandoff"); if (el) el.innerHTML = `<div class="notice">${esc(e.message)}</div>`; }
}

// ---------- status tab: token use for this project
async function loadStatus() {
  const pid = state.libProjectId;
  if (!$("#pStat")) return;
  let u; try { u = await api(`/api/projects/${enc(pid)}/usage?today=${localDay()}`); } catch (e) { if ($("#pStat")) $("#pStat").innerHTML = `<div class="notice">${esc(e.message)}</div>`; return; }
  if (state.libProjectId !== pid) return;
  statusCache = { pid, u }; paintStatus();
}
function burnRate(series) {
  const sum = rows => rows.reduce((a, r) => a + r.total, 0), last = sum(series.slice(-7)), prev = sum(series.slice(-14, -7));
  const change = prev ? Math.round((last - prev) / prev * 100) : null;
  const note = change === null ? "Nothing in the 7 days before" : change === 0 ? "About the same as the 7 days before" : `${change > 0 ? "Up" : "Down"} ${Math.abs(change)}% from the 7 days before`;
  return { perDay: last / 7, note, rising: change > 0 };
}
function statusHTML(d, u) {
  const burn = burnRate(u.series), a = u.all, tasks = openTasks(d), overdue = tasks.filter(dueOver).length, handoff = latestHandoff(d);
  const cell = (label, n) => `<div><dt>${label}</dt><dd title="${num(n)}">${tok(n)}</dd></div>`;
  const local = usage?.gpu.all.total;
  return `<section class="burn" aria-label="Token use">
      <div class="big"><span class="sub">Tokens used, all time</span><b title="${num(a.total)}">${tok(a.total)}</b><span class="sub">${tok(a.total - a.output)} in, ${tok(a.output)} out</span></div>
      <div class="big"><span class="sub">Burn rate, last 7 days</span><b title="${num(Math.round(burn.perDay))}">${tok(Math.round(burn.perDay))}<small> per day</small></b><span class="sub${burn.rising ? " soon" : ""}">${burn.note}</span></div>
      <dl>${cell("Today", u.today.total)}${cell("7 days", u.week.total)}${cell("30 days", u.month.total)}</dl>
    </section>
    ${local ? `<div class="sub pw-note">Claude tokens in this project. Work on ${esc(localLabel())} isn't recorded per project, so it only appears on the Usage page.</div>` : ""}
    <div class="pw-cols">
      <div class="wide">
        <h3>Last 30 days<span class="seg" role="group" aria-label="Metric"><button data-smetric="total" aria-pressed="${state.statusMetric === "total"}">Total</button><button data-smetric="output" aria-pressed="${state.statusMetric === "output"}">Output</button></span></h3>
        ${usageChart(u.series, state.statusMetric)}
        ${u.by_model.length ? `<h3>By model</h3>${usageRows(u.by_model.slice(0, 8).map(m => [m.name, m]), "Model")}` : ""}
        ${u.by_source.length > 1 ? `<h3>By source</h3>${usageRows(u.by_source.map(x => [srcLabel(x.name), x]), "Source")}` : ""}
      </div>
      <div class="side">
        <h3>At a glance</h3>
        <dl class="pfacts"><dt>Last activity</dt><dd>${ago(lastActivity(d))}</dd><dt>Latest handoff</dt><dd>${handoff ? ago(handoff.created_at) : "None yet"}</dd>
          <dt>Open tasks</dt><dd>${tasks.length}</dd><dt>Overdue</dt><dd class="${overdue ? "over" : ""}">${overdue}</dd><dt>On the board</dt><dd>${d.cards.filter(c => !c.archived_at).length}</dd></dl>
        ${u.by_card.length ? `<h3>Heaviest work</h3>${u.by_card.slice(0, 5).map(c => `<a class="prow" href="#" data-card="${esc(c.name)}"><span class="t"><span class="n">${esc(c.title)}</span></span><span class="arch" title="${num(c.total)}">${tok(c.total)}</span></a>`).join("")}` : ""}
      </div>
    </div>`;
}
function paintStatus() {
  const el = $("#pStat");
  if (!el || statusCache?.pid !== state.libProjectId || !state.libData) return;
  const u = statusCache.u;
  el.innerHTML = statusHTML(state.libData, u);
  bindChart($(".chart", el), u.series, state.statusMetric);
  $$("[data-smetric]", el).forEach(b => b.onclick = () => { state.statusMetric = b.dataset.smetric; paintStatus(); });
}
