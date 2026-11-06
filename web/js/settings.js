// ---------- settings
function parseStatus(k) { try { return JSON.parse(state.settings[k] || "null"); } catch { return null; } }
function statusLine(dot, el, s, fmt, idle) {
  $(dot).className = "dot" + (s ? (s.ok ? " ok" : " bad") : "");
  $(el).textContent = s ? (s.ok ? fmt(s) : `Failed ${ago(s.at)}: ${s.error}`) : idle;
}
function renderSettingsStatus() {
  const st = state.settings;
  const os_ = st.owui_key ? parseStatus("owui_status") : null;
  statusLine("#owuiDot", "#owuiStatus", os_, s => `Last sync ${ago(s.at)}: ${s.chats ?? 0} chats, ${s.changed ?? 0} updated${s.removed ? `, ${s.removed} removed` : ""}`, st.owui_key ? "Waiting for first sync…" : "Not connected");
  statusLine("#litellmDot", "#litellmStatus", st.litellm_key ? parseStatus("litellm_status") : null, s => `Last sync ${ago(s.at)}: ${num(s.requests ?? 0)} local requests in the last ${s.days} days`, st.litellm_key ? "Waiting for first sync…" : "Not connected");
  const held = os_?.held || 0, box = $("#owuiHeld");
  box.hidden = !held;
  if (held) {
    box.innerHTML = `<b>${held}</b> chat${held === 1 ? " is" : "s are"} no longer in Open WebUI. That's a lot at once, so the hub is waiting for you before removing them. <button class="btn small" type="button" id="owuiApply" style="margin-left:6px">Remove ${held}</button>`;
    $("#owuiApply").onclick = async () => {
      const verb = st.source_delete_action === "archive" ? "archive" : "move to the trash";
      if (!(await confirmUI("Apply removals?", `${verb[0].toUpperCase() + verb.slice(1)} <b>${held}</b> chat${held === 1 ? "" : "s"} deleted in Open WebUI? ${verb === "archive" ? "They stay in the library." : `The trash is emptied after ${st.trash_days || 7} days.`}`, "Remove", true))) return;
      await post("/api/integrations/owui/apply-removals", {}); box.textContent = "Removing on the next sync…";
    };
  }
  statusLine("#digestDot", "#digestStatus", parseStatus("digest_status"), s => `Last digests ${ago(s.at)}: ${(s.projects || []).length} projects`, "No digests yet");
  statusLine("#notifyDot", "#notifyStatus", st.notify_url ? parseStatus("notify_status") : null, s => `Last sent ${ago(s.at)}${s.last ? ": " + s.last : ""}`, st.notify_url ? "Configured, nothing sent yet" : "Off");
  statusLine("#backupDot", "#backupStatus", st.backup_configured ? parseStatus("backup_status") : null,
    s => `Last backup ${ago(s.at)}: ${s.snapshot} (${size(s.db_size)}), ${s.blobs_copied} new files, ${s.snapshots} snapshots kept`,
    st.backup_configured ? "No backup yet" : "Off: the server has no HUB_BACKUP_DIR");
  $("#backupNow").disabled = !st.backup_configured;
  statusLine("#indexDot", "#indexStatus", parseStatus("index_status"), s => `Indexed ${ago(s.at)}: ${s.chunks} passages, ${s.with_vectors} with embeddings${st.embeddings_off === "true" ? " (embeddings unavailable; keyword search only)" : ""}`, "Not indexed yet");
}
let settingsFilled = false;
function fillSettings() {
  settingsFilled = Object.keys(state.settings).length > 0;
  for (const el of $$("[data-s]")) {
    const k = el.dataset.s, v = state.settings[k] ?? "";
    if (el.dataset.bool !== undefined) el.checked = v === "true";
    else if (el.dataset.secret !== undefined) { el.value = ""; el.placeholder = v ? "Saved (enter a new value to replace)" : "Not set"; }
    else el.value = v;
  }
  $("#theme").value = load("kb.theme"); $("#tokenInput").value = load("kb.token");
  renderSettingsStatus(); renderAccount(); showSettingsSection(state.settingsSec);
}
function showSettingsSection(id) {
  state.settingsSec = id; store("kb.setSec", id);
  $$("[data-sec]").forEach(b => b.dataset.sec === id ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
  $$("[data-sec-body]").forEach(el => el.classList.toggle("on", el.dataset.secBody === id));
}
function openSettings(section) { if (section) state.settingsSec = section; showView("settings"); }
state.settingsSec = load("kb.setSec") || "general";
$$("[data-sec]").forEach(b => b.onclick = () => showSettingsSection(b.dataset.sec));
async function saveSettings() {
  const body = {};
  for (const el of $$("[data-s]")) {
    const k = el.dataset.s;
    if (el.dataset.bool !== undefined) body[k] = el.checked;
    else if (el.dataset.secret !== undefined) { if (el.value) body[k] = el.value; }
    else body[k] = el.value;
  }
  try { state.settings = { ...state.settings, ...(await post("/api/settings", body)) }; $$("[data-secret]").forEach(el => { el.value = ""; }); fillSettings(); toast("Settings saved"); }
  catch (e) { toast(e.message); }
}
$("#signOut").onclick = async () => { await fetch("/api/auth/logout", { method: "POST" }); location.reload(); };
$("#manageUsers").onclick = manageUsers;
$("#changePw").onclick = async () => {
  const r = await dialog({ title: "Change password", ok: "Change password", fields: [{ name: "current", label: "Current password", type: "password", required: true }, { name: "password", label: "New password (10 characters or more)", type: "password", required: true }] });
  if (r) post("/api/auth/password", r).then(() => toast("Password changed. Other sessions are signed out.")).catch(e => toast(e.message));
};
$("#settingsForm").onsubmit = e => { e.preventDefault(); saveSettings(); };
$$("[data-s]").forEach(el => { if (el.dataset.bool !== undefined || el.tagName === "SELECT") el.addEventListener("change", saveSettings); });
const themeNow = () => load("kb.theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
function showTheme() {
  $$("[data-set-theme]").forEach(b => b.setAttribute("aria-pressed", b.dataset.setTheme === themeNow()));
  $("#themeColor").content = getComputedStyle(document.documentElement).getPropertyValue("--panel").trim();
}
function setTheme(t) { store("kb.theme", t || undefined); applyTheme(t); $("#theme").value = t; showTheme(); }
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", showTheme);
$("#theme").onchange = e => setTheme(e.target.value);
$$("[data-set-theme]").forEach(b => b.onclick = () => setTheme(b.dataset.setTheme));
setTheme(load("kb.theme"));
$("#tokenInput").onchange = e => { store("kb.token", e.target.value || undefined); afterSignIn(); };
$("#owuiSync").onclick = async () => { await saveSettings(); await post("/api/integrations/owui/sync", {}); $("#owuiStatus").textContent = "Syncing…"; };
$("#litellmSync").onclick = async () => { await saveSettings(); await post("/api/integrations/litellm/sync", {}); $("#litellmStatus").textContent = "Syncing…"; };
$("#notifyTest").onclick = async () => { await saveSettings(); try { await post("/api/notify/test", {}); toast("Test notification sent"); } catch (e) { toast("Failed: " + e.message); } };
$("#backupNow").onclick = async () => { $("#backupNow").disabled = true; $("#backupStatus").textContent = "Backing up…"; try { await post("/api/backup", {}); toast("Backup done"); } catch (e) { toast("Backup failed: " + e.message); } $("#backupNow").disabled = false; };
$("#indexNow").onclick = async () => { await saveSettings(); $("#indexStatus").textContent = "Indexing in the background…"; try { await post("/api/index", {}); } catch (e) { toast(e.message); } };
$("#bookmarklet").href = `javascript:(()=>{const t=document.title.replace(/ [-\\u2013|] (Claude|Open WebUI|ChatGPT|Grok)$/,'');window.open(${JSON.stringify(location.origin + "/")}+'#add?'+new URLSearchParams({title:t,url:location.href}),'_blank')})()`;
const hubUrl = location.origin;
$("#mcpCmd").textContent = `claude mcp add --transport http --scope user vb-ai-hub ${hubUrl}/mcp --header "Authorization: Bearer $HUB_TOKEN"`;
$("#hookCmd").textContent = `curl -fsS -H "Authorization: Bearer $HUB_TOKEN" ${hubUrl}/connect/install.sh | HUB_URL=${hubUrl} HUB_TOKEN=$HUB_TOKEN bash`;
$("#cursorCfg").textContent = JSON.stringify({ mcpServers: { "vb-ai-hub": { url: `${hubUrl}/mcp`, headers: { Authorization: "Bearer ${env:HUB_TOKEN}" } } } }, null, 2);
$("#codexCfg").textContent = `[mcp_servers.vb-ai-hub]\nurl = "${hubUrl}/mcp"\nbearer_token_env_var = "HUB_TOKEN"`;
$("#mcpUrl").textContent = `${hubUrl}/mcp`;
$("#ingestCmd").textContent = `curl -X POST ${hubUrl}/api/ingest -H "Authorization: Bearer $HUB_TOKEN" -d '{"source":"chatgpt","id":"chat-1","title":"My chat","worklog":"What was done"}'`;
$$("[data-copy]").forEach(b => b.onclick = () => copyText($("#" + b.dataset.copy).textContent));
$$("[data-tool]").forEach(b => b.onclick = () => {
  $$("[data-tool]").forEach(x => x.setAttribute("aria-pressed", x === b));
  $$("[data-tool-body]").forEach(p => { p.hidden = p.dataset.toolBody !== b.dataset.tool; });
});
$("#bookmarklet").onclick = e => { e.preventDefault(); toast("Drag it to your bookmarks bar"); };
function handleHash() {
  const h = location.hash;
  if (h.startsWith("#add?")) { const p = Object.fromEntries(new URLSearchParams(h.slice(5))); history.replaceState(null, "", location.pathname); openAdd({ ...p, column: "active" }); }
  if (h.startsWith("#card=")) openDrawer(decodeURIComponent(h.slice(6)));
}
