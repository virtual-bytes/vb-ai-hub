// ---------- drawer (card or task)
async function openDrawer(id, tab) {
  if (!state.cards.has(id)) { try { state.cards.set(id, await api(`/api/cards/${enc(id)}`)); } catch { return toast("Card not found"); } }
  state.openKind = "card"; state.openId = id;
  $("#dTabs").innerHTML = ["details", "library", "activity"].map(t => `<button data-tab="${t}" aria-selected="false">${t[0].toUpperCase() + t.slice(1)}</button>`).join("");
  $$("#dTabs button").forEach(b => b.onclick = () => selectTab(b.dataset.tab));
  $("#dArchive").hidden = $("#dDelete").hidden = false;
  const h = $("#dHandoff"); h.hidden = false; h.dataset.label = "Generate handoff"; h.textContent = "Generate handoff"; h.dataset.busy = "";
  h.onclick = () => generate("handoff", "card", id, h);
  selectTab(tab || "details"); showDrawer();
}
function showDrawer() { $("#drawer").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false"); $("#dMore").open = false; if (state.view === "board") renderBoard(); else if (state.view === "tasks") renderTasksView(); }
function closeDrawer() { state.openId = null; $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); if (location.hash.startsWith("#card=")) history.replaceState(null, "", location.pathname); if (state.view === "board") renderBoard(); else if (state.view === "tasks") renderTasksView(); }
$("#dClose").onclick = closeDrawer;
$("#dMore").addEventListener("click", e => { if (e.target.closest("button")) $("#dMore").open = false; });
document.addEventListener("click", e => { if (!e.target.closest?.("#dMore")) $("#dMore").open = false; });
function drawerHead(c) {
  $("#dTitle").textContent = c.title || "Untitled";
  $("#dTag").textContent = [srcLabel(c.source), c.status && c.column !== "done" ? STATUS[c.status] : "", c.last_activity ? `last activity ${ago(c.last_activity)}` : ""].filter(Boolean).join(", ") + ".";
}
function selectTab(tab) {
  state.tab = tab; $$("#dTabs button").forEach(b => b.setAttribute("aria-selected", b.dataset.tab === tab));
  const c = state.cards.get(state.openId); if (!c) return;
  drawerHead(c);
  $("#dArchived").hidden = !c.archived_at; $("#dArchive").textContent = c.archived_at ? "Restore to board" : "Archive";
  ({ details: () => renderDetails(true), library: renderCardLibrary, activity: renderActivity })[tab]();
}
function taskOptions(c) {
  const ts = tasksOf(c.project);
  return `<option value="">No task</option>` + ts.map(t => `<option value="${esc(t.id)}" ${t.id === c.task_id ? "selected" : ""}>${esc(t.title)}${t.status === "done" ? " (done)" : ""}</option>`).join("") + `<option value="__new">New task…</option>`;
}
function renderDetails(full) {
  const c = state.cards.get(state.openId); if (!c || state.openKind !== "card") return;
  const body = $("#dBody");
  $("#dArchived").hidden = !c.archived_at; $("#dArchive").textContent = c.archived_at ? "Restore to board" : "Archive"; drawerHead(c);
  if (!full && body.contains(document.activeElement) && document.activeElement.matches("input,textarea,select")) { const i = $("#dInfo"); if (i) i.innerHTML = infoHTML(c); return; }
  const resume = c.source === "code" && c.session_id ? `cd ${shq(c.cwd || "~")} && claude --resume ${c.session_id}` : "";
  const openLabel = c.source === "owui" ? "Open in Open WebUI" : c.source === "chat" || c.source === "project" ? "Open in claude.ai" : "Open link";
  const locked = (c.user_fields || []).includes("project");
  body.innerHTML = `
    <label class="field">Title<input class="input" data-f="title" value="${esc(c.title)}"></label>
    <div class="row">
      <label class="field">Project ${locked ? `<span class="arch">set by hand · <button class="btn link" id="unlockProj" type="button">use rules</button></span>` : `<span class="arch">from ${c.source_project && c.source_project !== c.project ? "a rule" : "the source"}</span>`}<input class="input" data-f="project" list="projNames" value="${esc(c.project)}"></label>
      <label class="field">Task<select class="input" id="taskSel">${taskOptions(c)}</select></label>
      <label class="field">Column<select class="input" data-f="column">${state.columns.map(k => `<option value="${k}" ${k === c.column ? "selected" : ""}>${COLS[k][0]}</option>`).join("")}</select></label>
    </div>
    ${c.source !== "code" ? `<label class="field">Link<input class="input" data-f="url" value="${esc(c.url)}"></label>` : ""}
    ${safeUrl(c.url) ? `<a class="btn" style="align-self:flex-start" href="${safeHref(c.url)}" target="_blank" rel="noopener">${openLabel}</a>` : ""}
    ${resume ? `<div><div class="sec-h"><h3>Resume in a terminal</h3></div><div class="cmd" style="margin-top:10px"><code>${esc(resume)}</code><button class="btn small" id="copyResume">Copy</button></div></div>` : ""}
    ${c.summary ? `<div><div class="sec-h"><h3>Latest summary</h3><button class="btn link" data-goto="library">Full work log</button></div><div class="log" style="margin-top:10px"><div class="b">${esc(c.summary)}</div></div></div>` : ""}
    <label class="field">Notes<textarea data-f="notes" placeholder="Goals, decisions, next steps, links…">${esc(c.notes)}</textarea></label>
    <dl class="kv" id="dInfo">${infoHTML(c)}</dl>`;
  $$("[data-f]", body).forEach(inp => inp.addEventListener("change", () => save(c.id, { [inp.dataset.f]: inp.value })));
  $$("[data-goto]", body).forEach(b => b.onclick = () => selectTab(b.dataset.goto));
  $("#taskSel").onchange = async e => {
    let v = e.target.value;
    if (v === "__new") {
      if (!c.project) { toast("Give the card a project first"); e.target.value = c.task_id || ""; return; }
      let t; try { t = await newTaskDialog(projByName(c.project)?.id, c.title); } catch (err) { e.target.value = c.task_id || ""; return toast(err.message); }
      if (!t) { e.target.value = c.task_id || ""; return; }
      state.tasks.push(t); v = t.id;
    }
    save(c.id, { task_id: v || null });
  };
  const ul = $("#unlockProj"); if (ul) ul.onclick = () => save(c.id, { project: "" });
  const cr = $("#copyResume"); if (cr) cr.onclick = () => copyText(resume);
}
function usageLine(u) {
  if (!u || !(u.input || u.output || u.cache_read)) return "";
  return `${tok(u.input + u.cache_write + u.cache_read)} in (${tok(u.cache_read)} cached) · ${tok(u.output)} out`;
}
function infoHTML(c) {
  const a = c.archive || {};
  const rows = [
    ["Status", c.status ? { running: "Working", waiting: "Waiting on you", ended: "Ended" }[c.status] || c.status : ""],
    ["Last activity", fullDate(c.last_activity)], ["Created", fullDate(c.created_at)], ["Archived", fullDate(c.archived_at)],
    ["Messages", c.msg_count || ""], ["Model", c.model], ["Tokens", usageLine(c.usage)],
    ["Library", [a.docs && `${a.docs} documents`, a.links && `${a.links} commits/MRs`, a.files && `${a.files} files touched`, a.logs && `${a.logs} log entries`, a.transcripts && "transcript"].filter(Boolean).join(", ")],
    ["Folder", c.cwd ? { html: `<span class="mono">${esc(c.cwd)}</span>` } : ""], ["Session", c.session_id ? { html: `<span class="mono">${esc(c.session_id)}</span>` } : ""],
  ];
  return rows.filter(r => r[1]).map(([k, v]) => `<dt>${k}</dt><dd>${v.html ?? esc(v)}</dd>`).join("");
}
async function save(id, patch) {
  try { state.cards.set(id, await post(`/api/cards/${enc(id)}`, patch, "PATCH")); render(); if (state.openId === id) renderDetails(true); toast("Saved"); }
  catch (e) { toast("Save failed: " + e.message); }
}
$("#dArchive").onclick = async () => {
  const c = state.cards.get(state.openId); if (!c) return;
  const card = await post(`/api/cards/${enc(c.id)}`, { archived: !c.archived_at }, "PATCH");
  state.cards.set(card.id, card); toast(card.archived_at ? "Archived to the library" : "Restored to the board");
  selectTab(state.tab); render(); if (state.view === "library") renderLibraryMain();
};
$("#dDelete").onclick = async () => {
  if (state.openKind === "task") {
    const t = taskById(state.openId);
    if (!t || !(await confirmUI("Delete task?", `Delete the task <b>${esc(t.title)}</b>? Its cards stay; they just lose the task. Documents attached to the task are removed.`, "Delete task", true))) return;
    await api(`/api/tasks/${enc(t.id)}`, { method: "DELETE" }); closeDrawer(); await refresh(); return;
  }
  const c = state.cards.get(state.openId); if (!c) return;
  const a = c.archive || {}; const lib = a.docs || a.files || a.logs || a.transcripts ? " and everything in its library" : "";
  if (!(await confirmUI("Move to trash?", `Move <b>${esc(c.title)}</b>${lib} to the trash? It's removed for good after ${state.settings.trash_days || 7} days. Use Archive instead to keep it in the library.`, "Move to trash", true))) return;
  await api(`/api/cards/${enc(c.id)}`, { method: "DELETE" }); state.cards.delete(c.id); closeDrawer(); render(); refreshLibrary();
};

// owner library (card or task)
function groupDocs(atts) {
  const groups = new Map();
  for (const a of atts) { if (!groups.has(a.path)) groups.set(a.path, []); groups.get(a.path).push(a); }
  return [...groups.values()].sort((x, y) => y[0].created_at - x[0].created_at);
}
function docRows(owner, groups, emptyMsg) {
  return groups.length ? groups.map(v => { const d = v[0]; return `<div class="doc-row" data-act="doc" data-owner="${esc(owner)}" data-path="${esc(d.path)}">
    <div style="min-width:0"><div class="nm">${esc(d.name)}</div><div class="pt">${esc(d.path.startsWith("generated:") ? "Generated by the hub" : d.path !== d.name ? d.path : "")}</div></div>
    <div class="r"><span class="tag plain">${KIND[d.kind] || esc(d.kind)}</span><br>${v.length > 1 ? v.length + " versions · " : ""}${ago(d.created_at)}</div></div>`; }).join("")
    : `<div class="empty">${emptyMsg}</div>`;
}
function linksHTML(links) {
  if (!links.length) return `<div class="empty">No commits or merge requests linked yet.</div>`;
  return `<div class="files">${links.map(l => `<div><span class="tag plain">${l.kind === "commit" ? "Commit" : l.kind === "mr" ? "MR" : "PR"}</span><span class="p">${safeUrl(l.url) ? `<a href="${safeHref(l.url)}" target="_blank" rel="noopener">${esc(l.title || l.key)}</a>` : esc(l.title || l.key)}${l.repo ? ` <span class="arch">${esc(l.repo)}${l.kind === "commit" ? " · " + esc(l.key.slice(0, 8)) : ""}</span>` : ""}</span><span class="a">${ago(l.at)}</span></div>`).join("")}</div>`;
}
function usageTable(rows) {
  if (!rows.length) return "";
  return `<div class="t-wrap"><table class="list"><thead><tr><th>Model</th><th class="num">Input</th><th class="num">Cache read</th><th class="num">Cache write</th><th class="num">Output</th></tr></thead><tbody>
    ${rows.map(u => `<tr><td>${esc(u.model)}</td><td class="num">${tok(u.input)}</td><td class="num">${tok(u.cache_read)}</td><td class="num">${tok(u.cache_write)}</td><td class="num">${tok(u.output)}</td></tr>`).join("")}</tbody></table></div>`;
}
async function renderCardLibrary() {
  const id = state.openId, body = $("#dBody");
  body.innerHTML = `<div class="sub">Loading…</div>`;
  let arc; try { arc = await api(`/api/cards/${enc(id)}/archive`); } catch (e) { body.innerHTML = `<div class="notice">${esc(e.message)}</div>`; return; }
  if (state.openId !== id || state.tab !== "library") return;
  state.archive = { owner: id, ...arc };
  const c = state.cards.get(id);
  const docs = groupDocs(arc.attachments.filter(a => a.kind !== "transcript"));
  const tr = arc.attachments.find(a => a.kind === "transcript");
  body.innerHTML = `
    <div class="row" style="gap:8px">
      <button class="btn small" id="uploadBtn">Upload files</button>
      <button class="btn small" id="bundleBtn">Download all (.zip)</button>
    </div>
    <div class="sec-h"><h3>Documents</h3><span class="count">${docs.length}</span></div>
    ${docRows(id, docs, "No documents yet. Handoffs and docs written during sessions are saved here automatically; use Generate handoff or upload your own.")}
    <div class="sec-h"><h3>Commits and merge requests</h3><span class="count">${arc.links.length}</span></div>
    ${linksHTML(arc.links)}
    <div class="sec-h"><h3>Work log</h3><span class="count">${arc.worklog.length}</span></div>
    ${arc.worklog.length ? arc.worklog.slice(0, 60).map(w => `<div class="log"><div class="h"><span>${fullDate(w.at)}</span><span class="spacer"></span><button class="btn link" data-act="toggleLog">Expand</button></div><div class="b">${esc(w.text)}</div></div>`).join("") : `<div class="empty">No summaries yet.</div>`}
    <div class="sec-h"><h3>Files created or edited</h3><span class="count">${arc.files.length}</span></div>
    ${arc.files.length ? `<div class="files">${arc.files.map(f => `<div><span class="p mono">${esc(f.path)}</span><span class="a">${f.last_action === "write" ? "written" : "edited"}${f.edits > 1 ? ` ×${f.edits}` : ""} · ${ago(f.last_at)}</span></div>`).join("")}</div>` : `<div class="empty">No file changes recorded.</div>`}
    ${arc.usage.length ? `<div class="sec-h"><h3>Token usage</h3></div>${usageTable(arc.usage)}` : ""}
    <div class="sec-h"><h3>Transcript</h3></div>
    ${tr ? `<div class="doc-row" id="trDl"><div><div class="nm">${esc(tr.name)}</div><div class="pt">Full ${c?.source === "code" ? "Claude Code session" : "conversation"}, compressed</div></div><div class="r">${size(tr.size)}<br>${ago(tr.created_at)}</div></div>` : `<div class="empty">No transcript archived.</div>`}`;
  $("#uploadBtn").onclick = () => { $("#uploadInput").dataset.owner = id; $("#uploadInput").click(); };
  $("#bundleBtn").onclick = () => download(`/api/cards/${enc(id)}/bundle`, (c?.title || "card").replace(/[^\w.-]+/g, "_") + ".zip");
  const t = $("#trDl"); if (t) t.onclick = () => download(`/api/blobs/${tr.sha}`, tr.name);
}
$("#uploadInput").onchange = async e => {
  const owner = e.target.dataset.owner, files = [...e.target.files]; e.target.value = "";
  for (const f of files) {
    try {
      const q = new URLSearchParams({ kind: "upload", name: f.name, path: f.name, mime: f.type || "" });
      const res = await fetch(`/api/owners/${enc(owner)}/attachments?${q}`, { method: "PUT", body: f, headers: { ...authHeaders(), "Content-Type": "application/octet-stream" } });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
    } catch (err) { return toast(`${f.name}: ${err.message}`); }
  }
  toast(`Uploaded ${files.length} file${files.length > 1 ? "s" : ""}`);
  if (state.openKind === "task") renderTaskDrawer(); else if (state.openId === owner && state.tab === "library") renderCardLibrary();
  if (state.view === "library") renderLibraryMain();
};
async function renderActivity() {
  const id = state.openId, body = $("#dBody");
  body.innerHTML = `<ul class="timeline"><li><span class="k">Loading…</span></li></ul>`;
  try {
    const { events } = await api(`/api/cards/${enc(id)}/events`);
    if (state.openId !== id || state.tab !== "activity") return;
    const label = { created: "Created", UserPromptSubmit: "Prompt", Stop: "Claude finished", SessionStart: "Session started", SessionEnd: "Session ended", moved: "Moved", backfill: "Backfilled", imported: "Imported", sync: "Synced", archived: "Archived", unarchived: "Restored", update: "Updated", context: "Context given", trashed: "Moved to trash", restored: "Restored", "source-deleted": "Deleted at source" };
    body.innerHTML = `<ul class="timeline">${events.map(ev => `<li><span class="k" title="${esc(fullDate(ev.at))}">${ago(ev.at)} · ${esc(label[ev.kind] || ev.kind)}</span><span class="d">${esc(ev.detail)}</span></li>`).join("") || `<li><span class="k">No activity yet</span></li>`}</ul>`;
  } catch {}
}

// task drawer
function openTask(tid) {
  state.openKind = "task"; state.openId = tid;
  $("#dTabs").innerHTML = ""; $("#dArchive").hidden = true; $("#dDelete").hidden = false; $("#dArchived").hidden = true;
  const h = $("#dHandoff"); h.hidden = false; h.dataset.label = "Generate handoff"; h.textContent = "Generate handoff"; h.dataset.busy = "";
  h.onclick = () => generate("handoff", "task", tid, h);
  renderTaskDrawer(); showDrawer();
}
async function renderTaskDrawer() {
  const t = taskById(state.openId); if (!t) return closeDrawer();
  const p = projById(t.project_id);
  $("#dTitle").textContent = t.title; $("#dTag").textContent = `Task in ${p?.name || "no project"}.`;
  const body = $("#dBody");
  const cards = [...state.cards.values()].filter(c => c.task_id === t.id);
  let arc = { attachments: [] }; try { arc = await api(`/api/tasks/${enc(t.id)}`); } catch {}
  const full = t;
  state.archive = { owner: `task:${t.id}`, ...arc };
  body.innerHTML = `
    <label class="field">Title<input class="input" id="tTitle" value="${esc(t.title)}"></label>
    <div class="row">
      <label class="field">Priority<select class="input" id="tPrio">${Object.entries(PRIO).map(([k, v]) => `<option value="${k}" ${k === t.priority ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label class="field">Due<input class="input" type="date" id="tDue" value="${esc(t.due || "")}"></label>
      <label class="field">Project<select class="input" id="tProj">${state.projects.map(x => `<option value="${esc(x.id)}" ${x.id === t.project_id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></label>
    </div>
    <label class="switch"><input type="checkbox" id="tDone" ${t.status === "done" ? "checked" : ""}> Done</label>
    <label class="field">Notes<textarea id="tNotes" placeholder="What this task is for, acceptance criteria…">${esc(full.notes || "")}</textarea></label>
    <div class="sec-h"><h3>Items</h3><span class="count">${cards.length}</span></div>
    ${cards.length ? cards.map(c => `<div class="doc-row" data-card="${esc(c.id)}"><div style="min-width:0"><div class="nm">${esc(c.title)}</div><div class="pt">${esc(srcLabel(c.source))}${c.archived_at ? " · archived" : ""}</div></div><div class="r">${archBits(c)}<br>${ago(c.last_activity)}</div></div>`).join("")
      : `<div class="empty">No items yet. Assign cards from their Details tab or with Select on the board.</div>`}
    <div class="sec-h"><h3>Task documents</h3><button class="btn small" id="tUpload">Upload</button></div>
    ${docRows(`task:${t.id}`, groupDocs(arc.attachments || []), "Generated handoffs for this task and your uploads appear here.")}`;
  $("#tTitle").onchange = e => taskPatch(t.id, { title: e.target.value });
  $("#tNotes").onchange = e => taskPatch(t.id, { notes: e.target.value });
  $("#tDone").onchange = e => taskPatch(t.id, { status: e.target.checked ? "done" : "open" });
  $("#tPrio").onchange = e => taskPatch(t.id, { priority: e.target.value });
  $("#tDue").onchange = e => taskPatch(t.id, { due: e.target.value });
  $("#tProj").onchange = e => taskPatch(t.id, { project_id: e.target.value });
  $("#tUpload").onclick = () => { $("#uploadInput").dataset.owner = `task:${t.id}`; $("#uploadInput").click(); };
}
async function taskPatch(id, b) {
  try { const t = await post(`/api/tasks/${enc(id)}`, b, "PATCH"); Object.assign(taskById(id), t); toast("Saved"); renderBoard(); if (state.view === "library") renderLibraryMain(); if (state.view === "tasks") renderTasksView(); }
  catch (e) { toast(e.message); }
}
