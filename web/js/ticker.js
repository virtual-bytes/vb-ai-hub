// ---------- token ticker
const localDay = () => { const d = new Date(), z = n => String(n).padStart(2, "0"); return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`; };
const num = n => (n || 0).toLocaleString();
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
function tick(el, to) {
  const from = el._v || 0; el._v = to; cancelAnimationFrame(el._raf);
  if (from === to || !from || reduceMotion()) { el.textContent = num(to); return; }
  const t0 = performance.now(), dur = 1400;
  const step = t => { const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
    el.textContent = num(Math.round(from + (to - from) * e)); if (p < 1) el._raf = requestAnimationFrame(step); };
  el._raf = requestAnimationFrame(step);
}
let usage = null, usageT;
async function loadUsage() {
  try { usage = await api(`/api/usage/stats?today=${localDay()}`); } catch { return; }
  tick($("#tkToday"), usage.today.total); tick($("#tkWeek"), usage.week.total); tick($("#tkMonth"), usage.month.total);
  tick($("#tkIn"), usage.all.total - usage.all.output + usage.gpu.all.input); tick($("#tkOutAll"), usage.all.output + usage.gpu.all.output);
  renderLocalLabel();
  tick($("#tkGpuToday"), usage.gpu.today.total); tick($("#tkGpuAll"), usage.gpu.all.total);
  loadUsagePage();
  loadStatus();
}
function usageRows(rows, first) {
  return `<div class="t-wrap"><table class="list"><thead><tr><th>${first}</th><th class="num">New input</th><th class="num">Cache write</th><th class="num">Cache read</th><th class="num">Output</th><th class="num">Total</th><th class="num">Messages</th></tr></thead><tbody>
    ${rows.map(([name, u]) => `<tr><td>${esc(name)}</td><td class="num">${tok(u.input)}</td><td class="num">${tok(u.cache_write)}</td><td class="num">${tok(u.cache_read)}</td><td class="num">${tok(u.output)}</td><td class="num"><b>${tok(u.total)}</b></td><td class="num">${num(u.messages)}</td></tr>`).join("")}</tbody></table></div>`;
}
const localLabel = () => state.settings.local_label || "Local models";
function clientName(alias) {
  for (const line of (state.settings.client_names || "").split("\n")) { const i = line.indexOf("="); if (i > 0 && line.slice(0, i).trim() === alias) return line.slice(i + 1).trim(); }
  return { openwebui: "Open WebUI", "?": "Unlabelled" }[alias] || alias;
}
function gpuRows(rows, first) {
  return `<div class="t-wrap"><table class="list"><thead><tr><th>${first}</th><th class="num">Requests</th><th class="num">Input</th><th class="num">Output</th><th class="num">Total</th></tr></thead><tbody>
    ${rows.map(([name, x]) => `<tr><td>${esc(name)}</td><td class="num">${num(x.requests)}</td><td class="num">${tok(x.input)}</td><td class="num">${tok(x.output)}</td><td class="num"><b>${tok(x.total)}</b></td></tr>`).join("") || `<tr><td colspan="5" class="sub">No usage yet.</td></tr>`}</tbody></table></div>`;
}
function renderLocalLabel() { $$("[data-local-label]").forEach(el => { el.textContent = localLabel(); }); }
const shortDay = d => new Date(d + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
function niceTop(v) {
  const p = 10 ** Math.floor(Math.log10(v)), n = v / p;
  return [1, 1.5, 2, 3, 4, 5, 7.5, 10].find(x => x >= n) * p;
}
const money = n => !n ? "$0" : n < .01 ? "<$0.01" : n < 100 ? "$" + n.toFixed(2) : "$" + Math.round(n).toLocaleString();
const chartValue = (metric, n) => metric === "cost" ? money(n) : tok(n);
function usageChart(series, metric) {
  const vals = series.map(d => d[metric]), most = Math.max(...vals, 0), top = most ? niceTop(most) : 1;
  const when = d => (d.weekOf ? "Week of " : "") + shortDay(d.day), mid = series[Math.floor(series.length / 2)];
  if (!most) return `<div class="chart-none sub">Nothing in this period.</div>`;
  return `<div class="chart">
      <div class="chart-y" aria-hidden="true"><span>${chartValue(metric, top)}</span><span>${chartValue(metric, top / 2)}</span><span>0</span></div>
      <div class="chart-plot"><i class="chart-line" style="top:0"></i><i class="chart-line" style="top:50%"></i>
        <div class="bars">${series.map((d, i) => `<div class="bar" tabindex="0" data-i="${i}" aria-label="${esc(when(d))}: ${metric === "cost" ? money(vals[i]) : num(vals[i]) + " tokens"}"><i style="height:${vals[i] ? Math.max(1, vals[i] / top * 100) : 0}%"></i></div>`).join("")}</div>
        <div class="chart-x"><span>${shortDay(series[0].day)}</span><span>${shortDay(mid.day)}</span><span>${series.at(-1).weekOf ? "This week" : "Today"}</span></div></div></div>`;
}
function bindChart(chart, series, metric) {
  if (!chart) return;
  const plot = $(".chart-plot", chart), hide = () => $(".chart-tip", plot)?.remove();
  const show = bar => {
    let tip = $(".chart-tip", plot);
    if (!tip) { tip = document.createElement("div"); tip.className = "chart-tip"; plot.append(tip); }
    const d = series[+bar.dataset.i], r = bar.getBoundingClientRect(), c = plot.getBoundingClientRect();
    tip.textContent = `${d.weekOf ? "Week of " : ""}${shortDay(d.day)}: ${metric === "cost" ? money(d[metric]) : num(d[metric])}`;
    tip.style.left = Math.min(c.width - 80, Math.max(80, r.left - c.left + r.width / 2)) + "px"; tip.style.top = "2px";
  };
  chart.onmousemove = e => { const bar = e.target.closest(".bar"); bar ? show(bar) : hide(); };
  chart.onmouseleave = hide;
  chart.onfocusin = e => { const bar = e.target.closest(".bar"); if (bar) show(bar); };
  chart.onfocusout = hide;
}
