// ---------- handoff review: a generated draft waits here until it's saved or discarded
let rv = null;
const stripFooter = t => { const i = t.lastIndexOf("\n---\n_Generated "); return i < 0 ? t : t.slice(0, i); };
const docLines = t => stripFooter(t).split("\n").map(l => l.trimEnd()).filter(Boolean);
const lineKey = s => s.replace(/^(\s*)\d+\.\s/, "$1#. ");  // a list item that only moved up hasn't changed
function lineDiff(a, b) {
  const ka = a.map(lineKey), kb = b.map(lineKey);
  let top = 0, end = 0;
  while (top < ka.length && top < kb.length && ka[top] === kb[top]) top++;
  while (end < ka.length - top && end < kb.length - top && ka[ka.length - 1 - end] === kb[kb.length - 1 - end]) end++;
  const x = ka.slice(top, ka.length - end), y = kb.slice(top, kb.length - end), w = y.length + 1, lcs = new Uint32Array((x.length + 1) * w);
  for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--)
    lcs[i * w + j] = x[i] === y[j] ? lcs[(i + 1) * w + j + 1] + 1 : Math.max(lcs[(i + 1) * w + j], lcs[i * w + j + 1]);
  const mid = [];
  let i = 0, j = 0;
  while (i < x.length || j < y.length) {
    if (i < x.length && j < y.length && x[i] === y[j]) { mid.push({ t: "same", s: b[top + j] }); i++; j++; }
    else if (j < y.length && (i === x.length || lcs[i * w + j + 1] > lcs[(i + 1) * w + j])) mid.push({ t: "add", s: b[top + j++] });
    else mid.push({ t: "del", s: a[top + i++] });
  }
  return [...b.slice(0, top).map(s => ({ t: "same", s })), ...mid, ...b.slice(b.length - end).map(s => ({ t: "same", s }))];
}
function diffHTML(rows) {
  const say = { add: "Added: ", del: "Removed: ", same: "" }, mark = { add: "+", del: "−", same: "" };
  return rows.map(r => { const h = /^#{1,6}\s+/.test(r.s);
    return `<div class="dl ${r.t}${h ? " h" : ""}"><span class="mk" aria-hidden="true">${mark[r.t]}</span><span class="tx"><span class="vh">${say[r.t]}</span>${esc(h ? r.s.replace(/^#+\s+/, "") : r.s)}</span></div>`; }).join("");
}
async function blobText(sha) {
  if (!handoffText.has(sha)) handoffText.set(sha, await (await fetchBlob(`/api/blobs/${sha}`)).text());
  return handoffText.get(sha);
}
const generating = pid => Object.values(state.jobs).some(j => j.kind === "handoff" && j.scope === "project" && j.target === pid);
const sameSources = () => rv.checked.size === rv.draft.used.length && rv.draft.used.every(id => rv.checked.has(id));
const sourceMeta = s => s.id === "previous" ? `Previous handoff, ${ago(s.at)}` : `${srcLabel(s.source)}, ${ago(s.at)}`;

async function renderReview(d) {
  const main = $("#libMain"), pid = d.project.id;
  let r; try { r = await api(`/api/projects/${enc(pid)}/draft`); } catch (e) { main.innerHTML = `<div class="notice">${esc(e.message)}</div>`; return; }
  if (state.reviewPid !== pid) return;
  if (!r.draft) { state.reviewPid = null; return renderLibraryMain(); }
  if (rv?.pid !== pid || rv.at !== r.draft.created_at) {
    const skip = new Set(r.draft.excluded);
    rv = { pid, at: r.draft.created_at, mode: r.versions.length ? "changes" : "full", compare: 0, checked: new Set(r.sources.map(s => s.id).filter(id => !skip.has(id))) };
  }
  Object.assign(rv, r, { name: d.project.name });
  const busy = generating(pid), next = r.versions.length + 1;
  main.innerHTML = `<div class="pw">
    <div class="ph">
      <div class="ttl"><button class="btn link" id="rvBack">${esc(d.project.name)}</button><h2>Review handoff draft</h2><div class="sub">Generated ${ago(r.draft.created_at)}. It isn't in the library until you save it.</div></div>
      <button class="btn small" id="rvDiscard">Discard</button>
      <button class="btn small" id="rvRegen" data-label="Regenerate" ${busy ? "disabled" : ""}>${busy ? "Generating…" : "Regenerate"}</button>
      <button class="btn small primary" id="rvSave" ${busy ? "disabled" : ""}>Save as version ${next}</button>
    </div>
    <div class="pw-cols" id="pBody">
      <div class="wide">
        <div class="row pw-bar" style="margin-top:0">
          ${r.versions.length ? `<span class="seg" role="group" aria-label="View"><button data-rvmode="changes" aria-pressed="${rv.mode === "changes"}">Changes</button><button data-rvmode="full" aria-pressed="${rv.mode === "full"}">Full text</button></span>
          <label class="rv-cmp">Compared with <select class="input" id="rvCompare">${r.versions.map((v, i) => `<option value="${i}" ${i === rv.compare ? "selected" : ""}>Version ${r.versions.length - i}, ${ago(v.created_at)}</option>`).join("")}</select></label>
          <span class="spacer"></span><span class="sub" id="rvCount"></span>` : `<span class="sub">This is the first version, so there's nothing to compare it with.</span>`}
        </div>
        <div id="rvDoc"><div class="sub">Loading…</div></div>
      </div>
      <div class="side">
        <h3>Built from</h3>
        <div class="sub" id="rvUsed"></div>
        ${r.sources.map(s => `<label class="rv-src"><input type="checkbox" data-rvsrc="${esc(s.id)}" ${rv.checked.has(s.id) ? "checked" : ""}><span><span class="n">${esc(s.title)}</span><span class="sub">${esc(sourceMeta(s))}</span></span></label>`).join("")}
        <div class="sub soon" id="rvStale"></div>
      </div>
    </div></div>`;
  paintSources(); paintReviewDoc();
  $("#rvBack").onclick = () => { state.reviewPid = null; renderLibraryMain(); };
  $$("[data-rvmode]").forEach(b => b.onclick = () => { rv.mode = b.dataset.rvmode; $$("[data-rvmode]").forEach(x => x.setAttribute("aria-pressed", x === b)); paintReviewDoc(); });
  if ($("#rvCompare")) $("#rvCompare").onchange = e => { rv.compare = +e.target.value; paintReviewDoc(); };
  $$("[data-rvsrc]").forEach(c => c.onchange = () => { c.checked ? rv.checked.add(c.dataset.rvsrc) : rv.checked.delete(c.dataset.rvsrc); paintSources(); });
  $("#rvRegen").onclick = e => {
    if (!rv.checked.size) return toast("Pick at least one source");
    e.target.disabled = true; $("#rvSave").disabled = true;
    generate("handoff", "project", pid, e.target, { exclude: rv.sources.map(s => s.id).filter(id => !rv.checked.has(id)) });
  };
  $("#rvSave").onclick = async () => {
    try { await post(`/api/projects/${enc(pid)}/draft/save`, {}); toast(`Saved as version ${next}`); state.reviewPid = null; state.libTab = "overview"; store("kb.libTab", "overview"); await refreshLibrary(); renderLibraryMain(); }
    catch (e) { toast(e.message); }
  };
  $("#rvDiscard").onclick = async () => {
    if (!(await confirmUI("Discard this draft?", "The draft is deleted and nothing is added to the library.", "Discard", true))) return;
    try { await api(`/api/projects/${enc(pid)}/draft`, { method: "DELETE" }); toast("Draft discarded"); state.reviewPid = null; renderLibraryMain(); } catch (e) { toast(e.message); }
  };
}
function paintSources() {
  $("#rvUsed").textContent = `${rv.checked.size} of ${rv.sources.length} source${rv.sources.length === 1 ? "" : "s"} selected`;
  $("#rvStale").textContent = sameSources() ? "" : "This draft used different sources. Regenerate to update it.";
}
async function paintReviewDoc() {
  const out = $("#rvDoc"), { mode, compare, draft, versions } = rv, count = $("#rvCount");
  if (mode === "full" || !versions.length) { out.innerHTML = `<div class="md">${md(stripFooter(draft.text))}</div>`; if (count) count.textContent = ""; return; }
  let old; try { old = await blobText(versions[compare].sha); } catch (e) { out.innerHTML = `<div class="notice">${esc(e.message)}</div>`; return; }
  if (rv.mode !== mode || rv.compare !== compare || !$("#rvDoc")) return;
  const rows = lineDiff(docLines(old), docLines(draft.text)), n = t => rows.filter(r => r.t === t).length;
  out.innerHTML = diffHTML(rows);
  count.textContent = n("add") + n("del") ? `${n("add")} added, ${n("del")} removed` : "No changes";
}
