state.src = load("kb.src") || "all";
state.boardMode = load("kb.boardMode") || "columns";
renderSources();
window.addEventListener("hashchange", handleHash);
setInterval(() => { if (!document.hidden && state.view === "board" && !state.openId) renderBoard(); }, 30000);
showView(VIEWS[load("kb.view")] ? load("kb.view") : "board");
fetch("/api/auth/status", { headers: authHeaders() }).then(r => r.json()).then(s => { me = s.user; }).catch(() => {});
refresh().then(handleHash); connect(); loadUsage();
setInterval(loadUsage, 300000);  // also rolls "today" over at midnight
