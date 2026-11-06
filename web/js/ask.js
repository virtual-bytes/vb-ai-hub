// ---------- ask the library
function setLibMode(m) { state.libMode = m; $("#askBtn").setAttribute("aria-current", m === "ask"); $("#trashBtn").setAttribute("aria-current", m === "trash"); renderPicker();
  if (m === "ask") renderAsk(); else if (m === "trash") renderTrash(); else renderLibraryMain(); }
$("#askBtn").onclick = () => setLibMode(state.libMode === "ask" ? "project" : "ask");
$("#trashBtn").onclick = () => setLibMode(state.libMode === "trash" ? "project" : "trash");
let lastAsk = null;
function renderAsk() {
  const main = $("#libMain");
  main.innerHTML = `<h2>Ask the library</h2><div class="sub">Answers come from your docs, work logs, notes and handoffs, with sources. Uses the <code>${esc(state.settings.gen_model || "chat")}</code> model.</div>
    <div class="ask-box"><textarea id="askQ" placeholder="e.g. What did we decide about the database? How is the staging server set up?">${esc(lastAsk?.q || "")}</textarea>
      <div style="display:flex;flex-direction:column;gap:8px"><select class="input" id="askScope"><option value="">All projects</option>${state.projects.map(p => `<option value="${esc(p.id)}" ${p.id === lastAsk?.scope ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
      <button class="btn primary" id="askGo">Ask</button></div></div><div id="askOut"></div>`;
  $("#askGo").onclick = runAsk;
  $("#askQ").addEventListener("keydown", e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) runAsk(); });
  if (lastAsk?.res) showAnswer(lastAsk.res);
  $("#askQ").focus();
}
async function runAsk() {
  const q = $("#askQ").value.trim(); if (!q) return;
  const scope = $("#askScope").value;
  $("#askGo").disabled = true; $("#askOut").innerHTML = `<div class="answer"><span class="spin"></span> <span class="sub">Searching the library and writing an answer…</span></div>`;
  try {
    let res = await post("/api/ask", { question: q, project_id: scope || null });
    while (res && res.status === "running" && res.id) {  // long answers come back as a job to poll
      await new Promise(r => setTimeout(r, 2000));
      const j = await api(`/api/jobs/${res.id}`);
      if (j.status === "error") throw new Error(j.error);
      res = j.status === "done" ? j.result : j;
    }
    lastAsk = { q, scope, res }; if (state.libMode === "ask") showAnswer(res);
  }
  catch (e) { $("#askOut").innerHTML = `<div class="notice">${esc(e.message)}</div>`; }
  finally { const b = $("#askGo"); if (b) b.disabled = false; }
}
function srcAttrs(s) {
  if (s.kind === "doc") return `data-act="doc" data-owner="${esc(s.owner)}" data-path="${esc(s.ref)}"`;
  if (s.owner.startsWith("task:")) return `data-act="task" data-task="${esc(s.owner.slice(5))}"`;
  if (s.owner.startsWith("project:")) return `data-proj="${esc(s.owner.slice(8))}"`;
  return `data-card="${esc(s.owner)}"`;
}
function showAnswer(res) {
  const out = $("#askOut"); if (!out) return;
  let html = md(res.answer).replace(/\[(\d{1,2})\]/g, (m, n) => res.sources[n - 1] ? `<a class="cite" ${srcAttrs(res.sources[n - 1])} href="#">${n}</a>` : m);
  out.innerHTML = `<div class="answer"><div class="md">${html}</div></div>
    <h3>Sources ${res.semantic ? "" : `<span class="arch">keyword match only</span>`}</h3>
    ${res.sources.map((s, i) => `<div class="src" ${srcAttrs(s)}><span class="n">${i + 1}</span><div><div class="meta"><span class="tag plain">${{ log: "Work log", doc: "Document", card: "Item", project: "Project", task: "Task" }[s.kind]}</span>${s.project ? `<span class="tag proj">${esc(s.project)}</span>` : ""}<span class="when">${ago(s.at)}</span></div>
      <div style="font-weight:500;margin-top:4px">${esc(s.kind === "doc" ? s.ref.split("/").pop() + ": " + s.title : s.title)}</div><div class="s">${esc(s.snippet)}</div></div></div>`).join("")}`;
  $$("[data-proj]", out).forEach(h => h.onclick = e => { e.preventDefault(); state.libProjectId = h.dataset.proj; setLibMode("project"); });
}
