// ---------- views and the side menu
const VIEWS = { board: "Board", tasks: "Tasks", library: "Library", usage: "Usage", settings: "Settings" };
function renderTitle() { $("#viewTitle").textContent = VIEWS[state.view]; }
function showView(v) {
  state.view = v; store("kb.view", v);
  $$("[data-view]").forEach(b => b.dataset.view === v ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
  for (const id in VIEWS) $(`#${id}View`).classList.toggle("on", id === v);
  $("#srcSeg").hidden = $("#selectBtn").hidden = $("#modeBtn").hidden = v !== "board";
  $("#addBtn").hidden = !["board", "library"].includes(v);
  $("#search").hidden = v === "settings" || v === "usage";
  $("#askBtn").hidden = $("#trashBtn").hidden = v !== "library";
  $("#projPick").hidden = v === "settings" || v === "usage";
  if (v !== "board" && state.selecting) setSelecting(false);
  $("#search").placeholder = v === "board" ? "Search cards" : v === "tasks" ? "Search tasks" : "Search the library";
  closeRail(); renderTitle(); renderPicker();
  if (v === "library") refreshLibrary(); else if (v === "tasks") renderTasksView(); else if (v === "settings") fillSettings(); else if (v === "usage") { renderUsagePage(); loadUsagePage(); } else renderBoard();
}
$$("[data-view]").forEach(b => b.onclick = () => showView(b.dataset.view));
function openRail() { $("#rail").classList.add("open"); $("#scrim").classList.add("open"); }
function closeRail() { $("#rail").classList.remove("open"); $("#scrim").classList.remove("open"); }
$("#moreBtn").onclick = openRail;
$("#scrim").onclick = closeRail;
function railProjects() {
  const stats = {};
  for (const c of state.cards.values()) {
    const s = stats[c.project] ||= { open: 0, last: 0 };
    if (!c.archived_at) s.open++;
    s.last = Math.max(s.last, c.last_activity || 0);
  }
  return state.projects.map(p => ({ ...p, open: stats[p.name]?.open || 0, last: stats[p.name]?.last || p.updated_at || 0 }))
    .sort((a, b) => (a.status === "archived") - (b.status === "archived") || b.last - a.last);
}
function railCurrent() {
  if (state.view === "library") return state.libMode === "project" && !state.q ? state.libProjectId : null;
  return state.view === "tasks" ? state.taskFilter.project : projByName(state.project)?.id || "";  // "" means all projects
}
function renderPicker() {
  const cur = railCurrent(), find = $("#projFind").value.trim().toLowerCase();
  const flags = Object.fromEntries((state.overview || []).map(p => [p.id, p.overdue ? ["bad", "Overdue tasks"] : p.unrecorded ? ["waiting", "No recent handoff"] : null]));
  const row = (id, name, count, dim, flag) => `<button class="pick-row${dim ? " dim" : ""}" data-pid="${esc(id)}" aria-current="${id === (cur || "")}"><span class="n">${esc(name)}</span>${flag ? `<span class="dot ${flag[0]}" title="${flag[1]}"></span><span class="vh">${flag[1]}</span>` : ""}<span class="c">${count || ""}</span></button>`;
  const projects = railProjects().filter(p => p.name.toLowerCase().includes(find));
  $("#projList").innerHTML = (find ? "" : row("", "All projects")) + projects.map(p => row(p.id, p.name, p.open, p.status !== "active", flags[p.id])).join("") || `<div class="sub pick-none">No projects match.</div>`;
  $$(".pick-row").forEach(b => b.onclick = () => pickProject(b.dataset.pid));
  $("#projLabel").textContent = projById(cur)?.name || "All projects";
  const need = Object.values(flags).filter(Boolean).length;
  $("#projFlag").innerHTML = need && !cur ? `<span class="dot waiting" title="${need} need attention"></span><span class="vh">${need} need attention</span>` : "";
}
$("#projFind").oninput = renderPicker;
$("#projPick").addEventListener("toggle", () => { if ($("#projPick").open) { $("#projFind").value = ""; renderPicker(); $("#projFind").focus(); } });
document.addEventListener("click", e => { if (!e.target.closest?.("#projPick")) $("#projPick").open = false; });
document.addEventListener("keydown", e => { if (e.key === "Escape") $("#projPick").open = false; });
function pickProject(id) {
  const p = projById(id);
  state.project = p?.name || ""; state.taskFilter.project = id;
  $("#projPick").open = false;
  state.libProjectId = p ? id : null;
  if (state.view === "library" && state.q) { state.q = ""; $("#search").value = ""; }
  renderTitle();
  if (state.view === "library") setLibMode("project"); else if (state.view === "tasks") { renderPicker(); renderTasksView(); } else { renderPicker(); renderBoard(); }
}
async function refreshLibrary() {
  if (state.view !== "library") return;
  try { state.libProjects = (await api("/api/library")).projects; } catch { return; }
  if (!state.libProjects.some(p => p.id === state.libProjectId)) state.libProjectId = null;
  renderPicker();
  if (state.q) runSearch(); else if (state.libMode === "ask") { if (!$("#askQ")) renderAsk(); } else if (state.libMode === "trash") renderTrash(); else if (!editingProject()) renderLibraryMain();
}
$("#newProject").onclick = async () => {
  $("#projPick").open = false;
  const r = await dialog({ title: "New project", fields: [{ name: "name", label: "Name", required: true, placeholder: "e.g. My app" }, { name: "description", label: "Description (optional)", type: "textarea" }], ok: "Create project" });
  if (!r || !r.name) return; const name = r.name;
  try { const p = await post("/api/projects", { name, description: r.description }); state.libProjectId = p.id; await refresh(); refreshLibrary(); } catch (e) { toast(e.message); }
};
let searchT;
async function runSearch() {
  const q = state.q, main = $("#libMain");
  renderPicker();
  let res; try { res = (await api(`/api/search?q=${enc(q)}`)).results; } catch { return; }
  if (state.q !== q) return;
  const kindLabel = { card: "Item", doc: "Document", log: "Work log", project: "Project", task: "Task" };
  const snip = s => esc(s).replace(/\[\[/g, "<mark>").replace(/\]\]/g, "</mark>");
  main.innerHTML = `<h2>Search</h2><div class="sub">${res.length}${res.length === 60 ? "+" : ""} results for “${esc(q)}”</div><h3>Results</h3>` + (res.map(r => {
    const owner = r.card_id;
    const attrs = r.kind === "doc" ? `data-act="doc" data-owner="${esc(owner)}" data-path="${esc(r.ref)}"`
      : owner.startsWith("task:") ? `data-act="task" data-task="${esc(owner.slice(5))}"`
      : owner.startsWith("project:") ? `data-proj="${esc(owner.slice(8))}"` : `data-card="${esc(owner)}"`;
    return `<div class="hit" ${attrs}><div class="meta"><span class="tag plain">${kindLabel[r.kind]}</span><span class="tag ${srcClass(r.source)}">${esc(srcLabel(r.source))}</span>${r.project ? `<span class="tag proj">${esc(r.project)}</span>` : ""}<span class="when">${ago(r.last_activity)}</span></div>
      <div style="font-weight:500;margin-top:6px">${esc(r.kind === "doc" ? r.title + ": " + r.card_title : r.card_title)}</div><div class="snip">${snip(r.snip)}</div></div>`;
  }).join("") || `<div class="empty">No matches.</div>`);
  $$("[data-proj]", main).forEach(h => h.onclick = () => { state.libProjectId = h.dataset.proj; state.q = ""; $("#search").value = ""; refreshLibrary(); });
}
