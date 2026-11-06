// ---------- top bar controls
$("#search").addEventListener("input", e => {
  state.q = e.target.value.trim().toLowerCase();
  if (state.view === "board") return renderBoard();
  if (state.view === "tasks") return renderTasksView();
  if (state.libMode !== "project") state.libMode = "project";
  clearTimeout(searchT); searchT = setTimeout(() => state.q ? runSearch() : (renderPicker(), renderLibraryMain()), 220);
});
$("#srcSeg").onclick = e => { const b = e.target.closest("[data-src]"); if (!b) return; state.src = b.dataset.src; store("kb.src", state.src); renderSources(); renderBoard(); };

const addForm = $("#addForm");
addForm.column.innerHTML = Object.entries(COLS).map(([k, v]) => `<option value="${k}">${v[0]}</option>`).join("");
function openAdd(prefill = {}) { addForm.reset(); addForm.column.value = prefill.column || "ideas"; for (const k of ["title", "url", "project", "notes"]) if (prefill[k]) addForm[k].value = prefill[k]; $("#addDlg").showModal(); }
$("#addBtn").onclick = () => openAdd({ project: state.view === "library" ? projById(state.libProjectId)?.name : state.project });
$("#addDlg").addEventListener("close", async () => {
  if ($("#addDlg").returnValue !== "ok") return;
  try { const c = await post("/api/cards", Object.fromEntries(new FormData(addForm))); state.cards.set(c.id, c); await refresh(); toast("Added"); }
  catch (e) { toast("Add failed: " + e.message); }
});
$("#importBtn").onclick = () => $("#importDlg").showModal();
$("#importGo").onclick = async e => {
  e.preventDefault();
  const files = [...$("#importFile").files]; if (!files.length) return toast("Pick a file first");
  let chats = 0, projects = 0; $("#importGo").disabled = true;
  let removed = 0; const prune = $("#importPrune").checked ? "?prune=1" : "";
  try { for (const f of files) { const r = await api("/api/import" + prune, { method: "POST", body: await f.text() }); chats += r.chats; projects += r.projects; removed += r.removed || 0; } }
  catch (err) { toast(err.message); return; } finally { $("#importGo").disabled = false; }
  $("#importDlg").close(); toast(`Imported ${chats} chats and ${projects} projects${removed ? `; ${removed} removed` : ""}`); refresh();
};
