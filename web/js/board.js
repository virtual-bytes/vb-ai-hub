// ---------- filters
const EXTRA_SOURCES = ["chatgpt", "grok", "cursor", "codex"];  // a tab appears once the source has cards
function renderSources() {
  const used = new Set([...state.cards.values()].filter(c => !c.archived_at).map(c => c.source));
  const extra = EXTRA_SOURCES.filter(s => used.has(s) || s === state.src);
  const tabs = [["all", "All"], ["code", "Claude Code"], ["chat", "claude.ai"], ["owui", "Open WebUI"], ...extra.map(s => [s, SRC[s]]), ["other", "Other"]];
  const html = tabs.map(([id, label]) => `<button data-src="${id}" aria-pressed="${id === state.src}">${esc(label)}</button>`).join("");
  if ($("#srcSeg").innerHTML !== html) $("#srcSeg").innerHTML = html;
}
function srcMatch(s) {
  if (state.src === "all") return true;
  if (state.src === "chat") return s === "chat" || s === "project";
  if (state.src === "other") return !["code", "chat", "project", "owui", ...EXTRA_SOURCES].includes(s);
  return s === state.src;
}
function matches(c) {
  if (!srcMatch(c.source)) return false;
  if (state.project && c.project !== state.project) return false;
  if (state.q) {
    const hay = (c.title + " " + c.project + " " + c.last_prompt + " " + c.notes + " " + c.cwd + " " + c.summary).toLowerCase();
    if (!state.q.split(/\s+/).every(w => hay.includes(w))) return false;
  }
  return true;
}

// ---------- board
function archBits(c) {
  const a = c.archive || {}; const bits = [];
  if (a.docs) bits.push(`${a.docs} doc${a.docs > 1 ? "s" : ""}`);
  if (a.links) bits.push(`${a.links} commit${a.links > 1 ? "s" : ""}`);
  else if (a.files) bits.push(`${a.files} file${a.files > 1 ? "s" : ""}`);
  return bits.length ? `<span class="arch">${bits.join(" · ")}</span>` : "";
}
const STATUS = { running: "Working", waiting: "Waiting", ended: "Ended" };
const cardStatus = c => c.status && c.column !== "done" ? `<span class="status"><span class="dot ${esc(c.status)}"></span>${STATUS[c.status] || esc(c.status)}</span>` : "";
function cardHTML(c) {
  const sub = c.last_prompt && c.last_prompt !== c.title ? `<div class="prompt">${esc(c.last_prompt)}</div>` : "";
  const tk = c.task_id && taskById(c.task_id);
  const task = tk ? `<span class="task-n${dueClass(tk.due, tk.status === "done") === "over" ? " over" : ""}" title="${esc(tk.due ? dueLabel(tk.due) : "Task")}">${esc(tk.title)}</span>` : "";
  return `<div class="card${state.selected.has(c.id) ? " sel" : ""}${state.openId === c.id ? " open" : ""}" draggable="${!state.selecting}" data-id="${esc(c.id)}" tabindex="0">
    <div class="title">${esc(c.title || "Untitled")}</div>${sub}
    <div class="meta"><span>${esc(srcLabel(c.source))}</span>${c.project && c.project !== srcLabel(c.source) ? `<span>${esc(c.project)}</span>` : ""}${task}${archBits(c)}
      <span class="spacer"></span>${cardStatus(c)}<span class="when" title="${esc(fullDate(c.last_activity))}">${ago(c.last_activity)}</span></div></div>`;
}
function rowHTML(c) {
  const meta = [...new Set([c.project, srcLabel(c.source)])].filter(Boolean).map(esc).join(", ");
  const done = c.column === "done";
  return `<div class="card lrow${state.selected.has(c.id) ? " sel" : ""}${state.openId === c.id ? " open" : ""}" data-id="${esc(c.id)}" tabindex="0">
    <div class="t"><div class="title">${esc(c.title || "Untitled")}</div><div class="meta"><span>${meta}</span>${archBits(c)}${cardStatus(c)}</div></div>
    <span class="when" title="${esc(fullDate(c.last_activity))}">${ago(c.last_activity)}</span>
    <button class="btn small" data-act="moveCard" data-id="${esc(c.id)}" data-col="${done ? "active" : "done"}">${done ? "Reopen" : "Done"}</button></div>`;
}
function sorted(col) {
  return [...state.cards.values()].filter(c => c.column === col && !c.archived_at && matches(c)).sort((a, b) =>
    col === "done" ? b.last_activity - a.last_activity : (a.position - b.position) || (b.last_activity - a.last_activity));
}
const LIST_ORDER = [["needs_me", "Needs you"], ["active", "Working now"], ["ideas", "Ideas"], ["done", "Done"]];
function renderBoard() {
  const list = state.boardMode === "list", board = $("#board");
  $("#modeBtn").textContent = list ? "Show as columns" : "Show as list";
  board.classList.toggle("selecting", state.selecting); board.classList.toggle("as-list", list);
  $("#colTabs").innerHTML = list ? "" : ["needs_me", "active", "ideas", "done"].map(col =>
    `<button role="tab" data-bcol="${col}" aria-selected="${col === state.boardCol}">${COLS[col][0]} <span class="count">${sorted(col).length}</span></button>`).join("");
  $$("[data-bcol]").forEach(b => b.onclick = () => { state.boardCol = b.dataset.bcol; renderBoard(); });
  if (list) {
    board.innerHTML = LIST_ORDER.map(([col, label]) => {
      const rows = sorted(col), open = !state.closedGroups.has(col);
      return `<section class="lgroup"><h2><button data-act="toggleGroup" data-col="${col}" aria-expanded="${open}">${label} <span class="count">${rows.length}</span></button></h2>
        ${open ? rows.slice(0, col === "done" && !state.showAllDone ? DONE_LIMIT : undefined).map(rowHTML).join("") || `<div class="sub lnone">Nothing here</div>` : ""}</section>`;
    }).join("");
    return;
  }
  board.innerHTML = state.columns.map(col => {
    let list = sorted(col); const total = list.length; let more = "";
    if (col === "done" && !state.showAllDone && total > DONE_LIMIT) { list = list.slice(0, DONE_LIMIT); more = `<button class="btn small more" data-act="showAllDone">Show all ${total}</button>`; }
    return `<section class="column${col === state.boardCol ? " cur" : ""}" data-col="${col}">
      <div class="column-head"><h2>${COLS[col][0]}</h2><span class="count">${total}</span><span class="hint">${COLS[col][1]}</span></div>
      <div class="cards">${list.map(cardHTML).join("") || `<div class="empty">Nothing here</div>`}</div>${more}</section>`;
  }).join("");
}
function renderProjectsSelect() {
  const names = state.projects.map(p => p.name).sort((a, b) => a.localeCompare(b));
  $("#projNames").innerHTML = names.map(n => `<option value="${esc(n)}">`).join("");
  $("#bulkProj").innerHTML = `<option value="">Project…</option>` + names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  $("#bulkTask").innerHTML = `<option value="">Task…</option><option value="__none">No task</option>` + state.projects.map(p => {
    const ts = state.tasks.filter(t => t.project_id === p.id && t.status !== "done"); return ts.length ? `<optgroup label="${esc(p.name)}">${ts.map(t => `<option value="${esc(t.id)}">${esc(t.title)}</option>`).join("")}</optgroup>` : "";
  }).join("");
}
function render() {
  if (state.view === "board") renderBoard();
  if (state.view === "tasks" && !$("#tasksView").contains(document.activeElement)) renderTasksView();
  renderProjectsSelect(); renderPicker(); renderSources();
  if (state.openId && state.openKind === "card" && state.tab === "details") renderDetails(false);
}

// drag and drop
let dragId = null;
document.addEventListener("dragstart", e => { const el = e.target.closest?.(".card"); if (!el || state.selecting) return; dragId = el.dataset.id; el.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", dragId); });
document.addEventListener("dragend", e => { e.target.closest?.(".card")?.classList.remove("dragging"); $$(".column.drop").forEach(c => c.classList.remove("drop")); dragId = null; });
document.addEventListener("dragover", e => { const col = e.target.closest?.(".column"); if (!col || !dragId) return; e.preventDefault(); $$(".column.drop").forEach(c => c !== col && c.classList.remove("drop")); col.classList.add("drop"); });
document.addEventListener("drop", async e => {
  const colEl = e.target.closest?.(".column"); if (!colEl || !dragId) return;
  e.preventDefault(); colEl.classList.remove("drop");
  const col = colEl.dataset.col, id = dragId;
  const sib = $$(".card", colEl).filter(el => el.dataset.id !== id);
  const after = sib.find(el => { const r = el.getBoundingClientRect(); return e.clientY < r.top + r.height / 2; });
  const idx = after ? sib.indexOf(after) : sib.length;
  const pos = i => state.cards.get(sib[i]?.dataset.id)?.position;
  const prev = idx > 0 ? pos(idx - 1) : undefined, next = idx < sib.length ? pos(idx) : undefined;
  const position = prev === undefined && next === undefined ? 0 : prev === undefined ? next - 1 : next === undefined ? prev + 1 : (prev + next) / 2;
  const c = state.cards.get(id); if (!c) return;
  Object.assign(c, { column: col, position }); render();
  try { state.cards.set(id, await post(`/api/cards/${enc(id)}`, { column: col, position }, "PATCH")); }
  catch (err) { toast("Move failed: " + err.message); refresh(); }
});

// ---------- selection + bulk
function setSelecting(on) {
  state.selecting = on; if (!on) state.selected.clear();
  $("#selectBtn").setAttribute("aria-pressed", on); updateBulkbar(); renderBoard();
}
function updateBulkbar() { $("#bulkbar").hidden = !state.selecting; $("#bulkCount").textContent = `${state.selected.size} selected`; }
$("#selectBtn").onclick = () => setSelecting(!state.selecting);
$("#modeBtn").onclick = () => { state.boardMode = state.boardMode === "list" ? "columns" : "list"; store("kb.boardMode", state.boardMode); renderBoard(); };
$("#bulkClear").onclick = () => setSelecting(false);
$("#bulkCol").innerHTML = `<option value="">Move to…</option>` + Object.entries(COLS).map(([k, v]) => `<option value="${k}">${v[0]}</option>`).join("");
async function doBulk(body, msg) {
  if (!state.selected.size) return toast("Select some cards first");
  try { const r = await post("/api/cards/bulk", { ids: [...state.selected], ...body }); toast(msg(r)); state.selected.clear(); updateBulkbar(); await refresh(); }
  catch (e) { toast(e.message); }
}
$("#bulkCol").onchange = e => { const v = e.target.value; e.target.value = ""; if (v) doBulk({ set: { column: v } }, r => `Moved ${r.updated}`); };
$("#bulkProj").onchange = e => { const v = e.target.value; e.target.value = ""; if (v) doBulk({ set: { project: v } }, r => `${r.updated} moved to ${v}`); };
$("#bulkTask").onchange = e => { const v = e.target.value; e.target.value = ""; if (v) doBulk({ set: { task_id: v === "__none" ? null : v } }, r => `${r.updated} updated`); };
$("#bulkArchive").onclick = () => doBulk({ set: { archived: true } }, r => `Archived ${r.updated}`);
$("#bulkDelete").onclick = async () => {
  if (!state.selected.size) return toast("Select some cards first");
  if (await confirmUI("Move to trash?", `Move <b>${state.selected.size}</b> card${state.selected.size === 1 ? "" : "s"} to the trash? They're removed for good after ${state.settings.trash_days || 7} days.`, "Move to trash", true))
    doBulk({ delete: true }, r => `Moved ${r.deleted} to the trash`);
};

// ---------- global clicks
document.addEventListener("click", e => {
  const card = e.target.closest?.(".card, [data-card]");
  if (card && !e.target.closest("button, a:not([data-card]), input, select, textarea")) {
    e.preventDefault();
    const id = card.dataset.id || card.dataset.card;
    if (state.selecting && card.classList.contains("card")) { state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id); card.classList.toggle("sel"); return updateBulkbar(); }
    return openDrawer(id);
  }
  if (e.target.closest?.("[data-gen]")) return;
  const act = e.target.closest?.("[data-act]"); if (!act) return;
  const a = act.dataset.act;
  if (a === "showAllDone") { state.showAllDone = true; renderBoard(); }
  if (a === "moveCard") save(act.dataset.id, { column: act.dataset.col });
  if (a === "toggleGroup") { const c = act.dataset.col; state.closedGroups.has(c) ? state.closedGroups.delete(c) : state.closedGroups.add(c); renderBoard(); }
  if (a === "doc") openDoc(act.dataset.owner, act.dataset.path);
  if (a === "task") openTask(act.dataset.task);
  if (a === "toggleLog") act.closest(".log").classList.toggle("open");
});
document.addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.classList?.contains("card")) openDrawer(e.target.dataset.id);
  if (e.key === "Escape" && !$("dialog[open]")) { if (state.openId) closeDrawer(); else if (state.selecting) setSelecting(false); }
  if (e.key === "/" && document.activeElement === document.body) { e.preventDefault(); $("#search").focus(); }
});
