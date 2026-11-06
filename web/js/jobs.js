// ---------- generation jobs
async function generate(kind, scope, id, btn, opts = {}) {
  try {
    if (btn) { btn.dataset.busy = "1"; btn.textContent = "Generating…"; }
    const job = await post("/api/generate", { kind, scope, id, review: kind === "handoff" && scope === "project", ...opts });
    state.jobs[job.id] = { ...job, btn, label: btn?.dataset.label };
    toast(`Generating ${kind} with ${state.settings.gen_model || "chat"}… this can take a minute`);
  } catch (e) { toast(e.message); if (btn) { btn.dataset.busy = ""; btn.textContent = btn.dataset.label; } }
}
function onJob(j) {
  const local = state.jobs[j.id]; if (!local) return;
  if (j.status === "done" || j.status === "error") {
    if (local.btn) { local.btn.dataset.busy = ""; local.btn.textContent = local.label; }
    delete state.jobs[j.id];
    if (j.status === "error") { if (state.view === "library") renderLibraryMain(); return toast(`${j.kind} failed: ${j.error}`); }
    if (j.review && state.view === "library" && state.libMode === "project" && state.libProjectId === j.target && !editingProject()) state.reviewPid = j.target;
    toast(j.review ? "Handoff draft ready to review" : `${j.kind === "digest" ? "Digest" : "Handoff"} saved to the library`);
    if (state.openId && state.openKind === "card" && j.scope === "card" && j.target === state.openId) selectTab("library");
    if (state.openKind === "task" && j.scope === "task" && j.target === state.openId) renderTaskDrawer();
    if (state.view === "library") renderLibraryMain();
  }
}
function genBtn(label, kind, scope, id, cls = "") { return `<button class="btn small genbtn ${cls}" data-gen="${kind}|${scope}|${esc(id)}" data-label="${esc(label)}">${esc(label)}</button>`; }
document.addEventListener("click", e => { const b = e.target.closest?.("[data-gen]"); if (!b) return; const [k, s, i] = b.dataset.gen.split("|"); generate(k, s, i, b); });
