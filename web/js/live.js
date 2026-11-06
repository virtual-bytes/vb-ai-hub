// ---------- live
let es, libT, retryT, retryIn = 5000, wasLost = false, laterVersion = "";
function checkVersion(v) {
  $("#updateBar").hidden = v === $('meta[name="hub-version"]').content || v === laterVersion;
  $("#updateLater").onclick = () => { laterVersion = v; $("#updateBar").hidden = true; };
}
$("#updateReload").onclick = () => location.reload();
function connect() {
  es?.close();
  const token = load("kb.token");
  es = new EventSource("/api/stream" + (token ? "?token=" + encodeURIComponent(token) : ""));
  es.onopen = () => {
    $("#live").classList.add("on"); $("#live").title = "Live"; retryIn = 5000;
    if (wasLost) { wasLost = false; refresh(); loadUsage(); }
  };
  es.onerror = () => {
    $("#live").classList.remove("on"); $("#live").title = "Reconnecting…"; wasLost = true;
    // A dropped connection retries by itself, but an HTTP error such as the proxy's 502 during a restart ends the stream.
    if (es.readyState === EventSource.CLOSED) { clearTimeout(retryT); retryT = setTimeout(connect, retryIn); retryIn = Math.min(retryIn * 2, 60000); }
  };
  es.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.kind === "hello") checkVersion(m.data.version);
    else if (m.kind === "card") {
      const isNew = !state.cards.has(m.data.id); state.cards.set(m.data.id, m.data); render();
      if (isNew && !m.data.archived_at) toast(`New: ${m.data.title}`);
      if (state.view === "library") { clearTimeout(libT); libT = setTimeout(refreshLibrary, 1500); }
    } else if (m.kind === "delete") { state.cards.delete(m.data.id); render(); }
    else if (m.kind === "settings") { Object.assign(state.settings, m.data); renderLocalLabel(); if (state.view === "settings") renderSettingsStatus(); }
    else if (m.kind === "job") onJob(m.data);
    else if (m.kind === "usage") { clearTimeout(usageT); usageT = setTimeout(loadUsage, 1000); }
    else if (m.kind === "reload") { clearTimeout(libT); libT = setTimeout(refresh, 400); }
    else if (m.kind === "library" && state.view === "library") { clearTimeout(libT); libT = setTimeout(refreshLibrary, 800); }
  };
}
async function refresh() {
  try {
    const d = await api("/api/cards");
    const archived = [...state.cards.values()].filter(c => c.archived_at);
    state.cards = new Map([...archived, ...d.cards].map(c => [c.id, c]));
    Object.assign(state, { columns: d.columns, settings: d.settings, projects: d.projects, tasks: d.tasks });
    renderLocalLabel();
    render(); renderTitle(); loadAttention();
    if (state.view === "library") refreshLibrary();
    if (state.view === "settings" && !settingsFilled) fillSettings();
    if (state.view === "usage") renderUsagePage();
  } catch (e) { if (e.message !== "unauthorized") toast("Could not load: " + e.message); }
}
