// ---------- trash
async function renderTrash() {
  const main = $("#libMain");
  let d; try { d = await api("/api/cards?trash=1"); } catch { return; }
  if (state.libMode !== "trash") return;
  const days = state.settings.trash_days || 7;
  main.innerHTML = `<h2>Trash</h2><div class="sub">Deleted items, including chats deleted in Open WebUI or claude.ai and sessions deleted in Claude Code. Emptied after ${days} day${days == 1 ? "" : "s"}.</div>
    ${d.cards.length ? `<div class="row" style="margin:12px 0"><button class="btn small" id="trRestoreAll">Restore all</button><button class="btn small danger" id="trEmpty">Empty trash</button></div>
    <div class="t-wrap"><table class="list"><thead><tr><th>Title</th><th>Source</th><th>Project</th><th>Last activity</th><th></th></tr></thead><tbody>
    ${d.cards.map(c => `<tr><td>${esc(c.title)}</td><td><span class="tag ${srcClass(c.source)}">${esc(srcLabel(c.source))}</span></td><td>${esc(c.project)}</td><td>${ago(c.last_activity)}</td>
      <td style="white-space:nowrap"><button class="btn small" data-restore="${esc(c.id)}">Restore</button> <button class="btn small danger" data-purge="${esc(c.id)}">Delete forever</button></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty" style="margin-top:14px">Trash is empty.</div>`}`;
  const ids = d.cards.map(c => c.id);
  $$("[data-restore]").forEach(b => b.onclick = async () => { await post(`/api/cards/${enc(b.dataset.restore)}/restore`, {}); toast("Restored"); await refresh(); renderTrash(); });
  $$("[data-purge]").forEach(b => b.onclick = async () => { if (!(await confirmUI("Delete forever?", "Delete this item and its whole library (documents, work log, transcript) permanently? This can't be undone.", "Delete forever", true))) return; await api(`/api/cards/${enc(b.dataset.purge)}?purge=1`, { method: "DELETE" }); renderTrash(); });
  const ra = $("#trRestoreAll"); if (ra) ra.onclick = async () => { await post("/api/cards/bulk", { ids, restore: true }); toast(`Restored ${ids.length}`); await refresh(); renderTrash(); };
  const te = $("#trEmpty"); if (te) te.onclick = async () => { if (!(await confirmUI("Empty the trash?", `Permanently delete <b>${ids.length}</b> item${ids.length === 1 ? "" : "s"} and their libraries? This can't be undone.`, "Empty trash", true))) return; await post("/api/cards/bulk", { ids, purge: true }); renderTrash(); };
}
