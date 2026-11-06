// ---------- tasks view
function renderTasksView() {
  const f = state.taskFilter, t = todayStr(), weekEnd = new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10);
  const counts = {}; for (const c of state.cards.values()) if (c.task_id && !c.deleted_at) counts[c.task_id] = (counts[c.task_id] || 0) + 1;
  let list = state.tasks.filter(x => (f.done || x.status !== "done") && (!f.project || x.project_id === f.project)
    && (!state.q || (x.title + " " + (x.notes || "") + " " + (projById(x.project_id)?.name || "")).toLowerCase().includes(state.q)));
  list.sort((a, b) => (a.status === "done") - (b.status === "done") || (a.due || "9999") .localeCompare(b.due || "9999") || PRIO_ORDER[a.priority] - PRIO_ORDER[b.priority]);
  const groups = [["Overdue", x => x.status !== "done" && x.due && x.due < t], ["Due this week", x => x.status !== "done" && x.due && x.due >= t && x.due <= weekEnd],
    ["Later", x => x.status !== "done" && x.due && x.due > weekEnd], ["No due date", x => x.status !== "done" && !x.due], ["Done", x => x.status === "done"]];
  const row = x => { const done = x.status === "done", cls = dueClass(x.due, done), n = counts[x.id] || 0;
    return `<div class="trow${state.openKind === "task" && state.openId === x.id ? " open" : ""}${done ? " done" : ""}">
      <input type="checkbox" data-tx="${esc(x.id)}" ${done ? "checked" : ""} aria-label="Done: ${esc(x.title)}">
      <button data-act="task" data-task="${esc(x.id)}"><span class="n">${esc(x.title)}</span>
        ${x.priority !== "normal" ? `<span class="prio ${x.priority}">${PRIO[x.priority]}</span>` : ""}
        ${f.project ? "" : `<span class="sub">${esc(projById(x.project_id)?.name || "")}</span>`}
        ${n ? `<span class="sub">${n} item${n === 1 ? "" : "s"}</span>` : ""}
        ${x.due ? `<span class="due ${cls}">${dueLabel(x.due)}</span>` : ""}</button></div>`; };
  const body = groups.map(([name, fn]) => { const g = list.filter(fn); return g.length ? `<h3 class="${name === "Overdue" ? "over" : ""}">${name} <span class="count">${g.length}</span></h3>${g.map(row).join("")}` : ""; }).join("");
  const draft = $("#tfQuick")?.value || "", typing = document.activeElement?.id === "tfQuick";
  $("#tasksView").innerHTML = `<div class="tasks-page">
    <form class="tf-add" id="tfAdd"><input class="input" id="tfQuick" placeholder="Add a task, then press Enter" aria-label="New task" autocomplete="off" value="${esc(draft)}"><label class="switch"><input type="checkbox" id="tfDone" ${f.done ? "checked" : ""}> Show done</label></form>
    ${body || `<div class="empty">No tasks yet. Add one above, then assign sessions and chats to it.</div>`}</div>`;
  if (typing) $("#tfQuick").focus();
  $("#tfDone").onchange = e => { f.done = e.target.checked; renderTasksView(); };
  $("#tfAdd").onsubmit = async e => {
    e.preventDefault();
    const title = $("#tfQuick").value.trim(); if (!title) return;
    if (!state.projects.length) return toast("Create a project first");
    try {
      const made = f.project ? await post("/api/tasks", { project_id: f.project, title, priority: "normal", due: "" }) : await newTaskDialog(null, title);
      if (made) { $("#tfQuick").value = ""; await refresh(); renderTasksView(); }
    } catch (err) { toast(err.message); }
  };
  $$("[data-tx]").forEach(el => el.onchange = () => taskPatch(el.dataset.tx, { status: el.checked ? "done" : "open" }));
}
