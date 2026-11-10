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
function srcData(s) {
  if (s.kind === "doc") return { act: "doc", owner: s.owner, path: s.ref };
  if (s.kind === "task") return { act: "task", task: s.owner.slice(5) };
  if (s.kind === "project") return { proj: s.owner.slice(8) };
  return { card: s.owner };
}
const srcAttrs = s => Object.entries(srcData(s)).map(([k, v]) => `data-${k}="${esc(v)}"`).join(" ");
// Citation marks like [2] become links only in the answer's text: never inside code, a link or an attribute.
function citeLinks(el, sources) {
  const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, { acceptNode: n => n.parentElement.closest("code, pre, a") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  const nodes = []; while (walk.nextNode()) nodes.push(walk.currentNode);
  for (const node of nodes) {
    const parts = node.nodeValue.split(/(\[\d{1,2}\])/); if (parts.length < 2) continue;
    const frag = document.createDocumentFragment();
    for (const part of parts) {
      const m = part.match(/^\[(\d{1,2})\]$/), s = m && sources[m[1] - 1];
      if (!s) { frag.append(part); continue; }
      const a = Object.assign(document.createElement("a"), { className: "cite", href: "#", textContent: m[1] });
      Object.assign(a.dataset, srcData(s)); frag.append(a);
    }
    node.replaceWith(frag);
  }
}
function showAnswer(res) {
  const out = $("#askOut"); if (!out) return;
  out.innerHTML = `<div class="answer"><div class="md">${md(res.answer)}</div></div>
    <h3>Sources ${res.semantic ? "" : `<span class="arch">keyword match only</span>`}</h3>
    ${res.sources.map((s, i) => `<div class="src" ${srcAttrs(s)}><span class="n">${i + 1}</span><div><div class="meta"><span class="tag plain">${{ log: "Work log", doc: "Document", card: "Item", project: "Project", task: "Task" }[s.kind]}</span>${s.project ? `<span class="tag proj">${esc(s.project)}</span>` : ""}<span class="when">${ago(s.at)}</span></div>
      <div style="font-weight:500;margin-top:4px">${esc(s.kind === "doc" ? s.ref.split("/").pop() + ": " + s.title : s.title)}</div><div class="s">${esc(s.snippet)}</div></div></div>`).join("")}`;
  citeLinks($(".answer .md", out), res.sources);
  $$("[data-proj]", out).forEach(h => h.onclick = e => { e.preventDefault(); state.libProjectId = h.dataset.proj; setLibMode("project"); });
}
