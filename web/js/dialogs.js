// ---------- built-in dialogs (replace the browser's confirm/prompt)
// dialog({ title, message, fields: [{ name, label, type, value, options, placeholder, required }], ok, danger })
// resolves to null on cancel, or { fieldName: value, ... } on confirm ({} when there are no fields).
function dialog({ title, message = "", fields = [], ok = "OK", cancel = "Cancel", danger = false }) {
  const dlg = $("#uiDlg"), form = $("#uiForm");
  if (dlg.open) dlg.close("cancel");
  $("#uiTitle").textContent = title;
  $("#uiMsg").innerHTML = message;
  $("#uiFields").innerHTML = fields.map((f, i) => {
    const id = `uiF${i}`, req = f.required ? " required" : "", ph = f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : "";
    if (f.type === "checkbox") return `<label class="switch"><input type="checkbox" id="${id}" name="${esc(f.name)}" ${f.value ? "checked" : ""}> ${esc(f.label)}</label>`;
    if (f.type === "select") return `<label class="field">${esc(f.label)}<select class="input" id="${id}" name="${esc(f.name)}"${req}>${(f.options || []).map(o => {
      const [v, l] = Array.isArray(o) ? o : [o, o]; return `<option value="${esc(v)}" ${String(v) === String(f.value ?? "") ? "selected" : ""}>${esc(l)}</option>`; }).join("")}</select></label>`;
    if (f.type === "textarea") return `<label class="field">${esc(f.label)}<textarea class="short" id="${id}" name="${esc(f.name)}"${ph}${req}>${esc(f.value || "")}</textarea></label>`;
    return `<label class="field">${esc(f.label)}<input class="input" id="${id}" name="${esc(f.name)}" type="${f.type || "text"}" value="${esc(f.value ?? "")}"${ph}${req} autocomplete="off"></label>`;
  }).join("");
  $("#uiOk").textContent = ok; $("#uiCancel").textContent = cancel;
  $("#uiOk").classList.toggle("danger", danger);
  return new Promise(resolve => {
    const done = () => {
      dlg.removeEventListener("close", done);
      if (dlg.returnValue !== "ok") return resolve(null);
      const out = {};
      fields.forEach((f, i) => { const el = $(`#uiF${i}`); out[f.name] = f.type === "checkbox" ? el.checked : el.value.trim(); });
      resolve(out);
    };
    dlg.returnValue = "";
    dlg.addEventListener("close", done);
    dlg.showModal();
    // Destructive confirmations focus Cancel, so a stray Enter doesn't delete anything.
    (fields.length ? $("#uiF0") : danger ? $("#uiCancel") : $("#uiOk")).focus();
    if (fields.length && $("#uiF0").select) $("#uiF0").select();
  });
}
$("#uiCancel").onclick = () => $("#uiDlg").close("cancel");
document.addEventListener("click", e => { const b = e.target.closest?.("[data-close]"); if (b) $("#" + b.dataset.close).close("cancel"); });
const confirmUI = async (title, message, ok = "OK", danger = false) => !!(await dialog({ title, message, ok, danger }));
const projectOptions = (excludeId) => state.projects.filter(p => p.id !== excludeId).sort((a, b) => a.name.localeCompare(b.name)).map(p => [p.id, p.name]);
async function newTaskDialog(projectId, suggested = "") {
  const fields = [];
  if (!projectId) fields.push({ name: "project_id", label: "Project", type: "select", options: projectOptions(), value: state.taskFilter.project || "" });
  fields.push({ name: "title", label: "Title", value: suggested, required: true, placeholder: "e.g. 1.0 release" },
    { name: "priority", label: "Priority", type: "select", options: Object.entries(PRIO), value: "normal" },
    { name: "due", label: "Due (optional)", type: "date" });
  const r = await dialog({ title: projectId ? `New task in ${projById(projectId)?.name || "project"}` : "New task", fields, ok: "Create task" });
  if (!r || !r.title) return null;
  return post("/api/tasks", { project_id: projectId || r.project_id, title: r.title, priority: r.priority, due: r.due || "" });
}
