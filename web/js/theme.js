// ---------- theme, set before the page paints so a saved choice never flashes the other one
try { const t = localStorage.getItem("kb.theme"); if (t) document.documentElement.dataset.theme = t; } catch {}
