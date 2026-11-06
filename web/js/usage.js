// ---------- usage page
const USAGE_RANGES = { 7: "7 days", 30: "30 days", all: "All time" };
const USAGE_TOP = 8;
let report = null, usageOpenDetails = false;
Object.assign(state, { usageRange: USAGE_RANGES[load("kb.usageRange")] ? load("kb.usageRange") : "30", usageMetric: "total" });

async function loadUsagePage() {
  if (state.view !== "usage") return;
  try { report = await api(`/api/usage/report?today=${localDay()}`); } catch (e) { $("#usageView").innerHTML = `<div class="up"><div class="notice">${esc(e.message)}</div></div>`; return; }
  renderUsagePage();
}
function rangeSeries() {
  const all = report.series, r = state.usageRange;
  if (r !== "all") return all.slice(-Number(r));
  if (all.length <= 62) return all;
  const weeks = [];  // too many days for one bar each
  for (let end = all.length; end > 0; end -= 7) {
    const days = all.slice(Math.max(0, end - 7), end);
    weeks.unshift(days.reduce((w, d) => ({ day: w.day, weekOf: true, total: w.total + d.total, output: w.output + d.output, gpu: w.gpu + d.gpu, cost: w.cost + d.cost }), { day: days[0].day, total: 0, output: 0, gpu: 0, cost: 0 }));
  }
  return weeks;
}
function burn(r) {
  const days = state.usageRange === "all" ? report.series.length : Number(state.usageRange), total = r.claude.total + r.local.total;
  if (r.before === undefined) return { perDay: total / days, note: `Over ${num(days)} days` };
  const change = r.before ? Math.round((total - r.before) / r.before * 100) : null;
  return { perDay: total / days, up: change > 0, note: change === null ? `Nothing in the ${days} days before` : change === 0 ? `About the same as the ${days} days before` : `${change > 0 ? "Up" : "Down"} ${Math.abs(change)}% from the ${days} days before` };
}
function rankRows(rows, label = x => x.name, link = () => "") {
  const cost = state.usageMetric === "cost" && rows.every(r => r.cost !== undefined), val = r => cost ? r.cost : r.total, show = n => cost ? money(n) : tok(n);
  const ordered = cost ? [...rows].filter(r => r.cost > 0).sort((a, b) => b.cost - a.cost) : rows;
  const top = ordered.slice(0, USAGE_TOP), rest = ordered.slice(USAGE_TOP), sum = ordered.reduce((a, r) => a + val(r), 0);
  if (rest.length) top.push({ name: `Other (${rest.length})`, total: rest.reduce((a, r) => a + r.total, 0), cost: rest.reduce((a, r) => a + (r.cost || 0), 0), other: true });
  const max = Math.max(...top.map(val), cost ? .01 : 1);
  return top.map(r => { const name = r.other ? r.name : label(r), share = sum ? Math.round(val(r) / sum * 100) : 0, attrs = r.other ? "" : link(r);
    const body = `<span class="hb-l"><span class="n">${esc(name)}</span><span class="v" title="${cost ? money(r.cost) : num(r.total)}">${show(val(r))}<span class="s">${share < 1 ? "<1" : share}%</span></span></span><span class="hb-t"><i style="width:${Math.max(val(r) / max * 100, 0.5)}%"></i></span>`;
    return attrs ? `<button class="hb" ${attrs}>${body}</button>` : `<div class="hb">${body}</div>`; }).join("");
}
const rankCard = (title, rows, opts = []) => rows.length ? `<section class="up-card"><h2>${esc(title)}</h2>${rankRows(rows, ...opts)}</section>` : "";
function dayRows(series) {
  return `<div class="t-wrap up-days"><table class="list"><thead><tr><th>${series[0]?.weekOf ? "Week of" : "Day"}</th><th class="num">Claude total</th><th class="num">Claude output</th><th class="num">Cost</th><th class="num">${esc(localLabel())}</th></tr></thead><tbody>
    ${[...series].reverse().map(d => `<tr><td>${esc(shortDay(d.day))}</td><td class="num">${num(d.total)}</td><td class="num">${num(d.output)}</td><td class="num">${money(d.cost)}</td><td class="num">${num(d.gpu)}</td></tr>`).join("")}</tbody></table></div>`;
}
function budgetMeter(label, spent, limit, note) {
  const pct = Math.round(spent / limit * 100), over = pct >= 100, level = over ? "over" : pct >= 80 ? "near" : "";
  return `<div class="meter ${level}"><div class="meter-l"><span>${label}</span><span><b>${money(spent)}</b> of ${money(limit)}${over ? ", over budget" : pct >= 80 ? ", near the limit" : ""}</span></div>
    <span class="meter-t"><i style="width:${Math.min(pct, 100)}%"></i></span>${note ? `<div class="sub">${note}</div>` : ""}</div>`;
}
function budgetCard(b) {
  if (!b.monthly && !b.daily) return "";
  return `<section class="up-card up-budget"><h2>Budget</h2>
    ${b.monthly ? budgetMeter("This month", b.month, b.monthly, `On course for ${money(b.projected)} by the end of the month.`) : ""}
    ${b.daily ? budgetMeter("Today", b.day, b.daily, "") : ""}</section>`;
}
function renderUsagePage() {
  const el = $("#usageView");
  if (state.view !== "usage" || !report) return;
  const r = report.ranges[state.usageRange], c = r.claude, l = r.local, b = burn(r), series = rangeSeries(), label = localLabel(), range = USAGE_RANGES[state.usageRange];
  const total = c.total + l.total, input = c.total - c.output + l.input, output = c.output + l.output, metric = state.usageMetric, today = report.series.at(-1);
  const name = state.usageRange === "all" ? "Tokens used, all time" : `Tokens used, last ${range}`;
  const tile = (title, value, exact, note = "", cls = "") => `<div><dt>${title}</dt><dd title="${num(exact)}">${value}</dd>${note ? `<div class="sub ${cls}">${note}</div>` : ""}</div>`;
  const per = series[0]?.weekOf ? "week" : "day", keep = $("#usageView .up-more");
  if (keep) usageOpenDetails = keep.open;
  el.innerHTML = `<div class="up">
    <div class="up-top">
      <div class="up-hero"><span class="sub">${name}</span><b title="${num(total)}">${tok(total)}</b><span class="sub">${tok(input)} in, ${tok(output)} out</span></div>
      <span class="seg" role="group" aria-label="Time range">${Object.entries(USAGE_RANGES).map(([id, t]) => `<button data-urange="${id}" aria-pressed="${id === state.usageRange}">${t}</button>`).join("")}</span>
    </div>
    <dl class="up-kpis">
      ${tile("Burn rate", `${tok(Math.round(b.perDay))}<small> per day</small>`, Math.round(b.perDay), esc(b.note), b.up ? "soon" : "")}
      ${tile("Today", tok(today.total + today.gpu), today.total + today.gpu, `${tok(today.output)} written by Claude`)}
      ${tile("Estimated cost", money(r.cost), Math.round(r.cost), r.cost_before ? `${money(r.cost / (state.usageRange === "all" ? report.series.length : Number(state.usageRange)))} per day at API prices` : "At API prices")}
      ${tile("Claude", tok(c.total), c.total, `${num(c.messages)} messages`)}
      ${tile(esc(label), tok(l.total), l.total, `${num(l.requests)} requests`)}
    </dl>
    ${budgetCard(report.budget)}
    <div class="up-charts">
      <section class="up-card">
        <div class="up-card-h"><h2>Claude ${metric === "cost" ? "cost" : "tokens"} per ${per}</h2><span class="seg" role="group" aria-label="Metric"><button data-umetric="total" aria-pressed="${metric === "total"}">Total</button><button data-umetric="output" aria-pressed="${metric === "output"}">Output</button><button data-umetric="cost" aria-pressed="${metric === "cost"}">Cost</button></span></div>
        <div id="upClaude">${usageChart(series, metric)}</div>
      </section>
      <section class="up-card">
        <div class="up-card-h"><h2>${esc(label)} tokens per ${per}</h2></div>
        <div id="upLocal">${usageChart(series, "gpu")}</div>
      </section>
    </div>
    <div class="up-rank">
      ${rankCard("By project", r.by_project, [p => p.name || "No project", p => projByName(p.name) ? `data-uproj="${esc(projByName(p.name).id)}"` : ""])}
      ${rankCard("By model", r.by_model)}
      ${rankCard("By source", r.by_source, [x => srcLabel(x.name)])}
      ${rankCard(`${label} by workload`, l.by_client, [x => clientName(x.name)])}
      ${rankCard(`${label} by model`, l.by_model)}
    </div>
    <details class="up-more" ${usageOpenDetails ? "open" : ""}><summary>Exact figures, ${range.toLowerCase()}</summary>
      <h3>All tokens</h3>
      <div class="t-wrap"><table class="list"><thead><tr><th>Where</th><th class="num">In</th><th class="num">Out</th><th class="num">Total</th></tr></thead><tbody>
        ${[["Claude", c.total - c.output, c.output], [label, l.input, l.output], ["All", input, output]].map(([n, i, o]) => `<tr><td>${esc(n)}</td><td class="num">${num(i)}</td><td class="num">${num(o)}</td><td class="num"><b>${num(i + o)}</b></td></tr>`).join("")}</tbody></table></div>
      ${r.by_model.length ? `<h3>Claude by model</h3>${usageRows(r.by_model.map(m => [m.name, m]), "Model")}` : ""}
      ${r.cost ? `<h3>Estimated cost by model</h3><div class="t-wrap"><table class="list"><thead><tr><th>Model</th><th class="num">Cost</th></tr></thead><tbody>${r.by_model.filter(m => m.cost > 0).map(m => `<tr><td>${esc(m.name)}</td><td class="num">${money(m.cost)}</td></tr>`).join("")}</tbody></table></div>` : ""}
      ${l.by_client.length ? `<h3>${esc(label)} by workload</h3>${gpuRows(l.by_client.map(x => [clientName(x.name), x]), "Client")}` : ""}
      <h3>${series[0]?.weekOf ? "By week" : "By day"}</h3>${dayRows(series)}
    </details>
    <p class="sub up-note">In counts everything the models read: new input, cache writes and cache reads. Cache reads are context re-read on each turn, so they're most of it. Out is what the models wrote. Claude figures include subagents and workflows. ${esc(label)} counts work your own hardware did instead of a paid API. Project figures leave out sessions that have been deleted. Costs are estimates at the API prices in Settings. On a Claude subscription they show what the same usage would cost on the API, not what you pay.${report.unpriced.length ? ` Models without a price count as $0: ${esc(report.unpriced.slice(0, 6).join(", "))}${report.unpriced.length > 6 ? ` and ${report.unpriced.length - 6} more` : ""}.` : ""}</p>
  </div>`;
  bindChart($("#upClaude .chart"), series, metric);
  bindChart($("#upLocal .chart"), series, "gpu");
  $$("[data-urange]").forEach(x => x.onclick = () => { state.usageRange = x.dataset.urange; store("kb.usageRange", state.usageRange); renderUsagePage(); });
  $$("[data-umetric]").forEach(x => x.onclick = () => { state.usageMetric = x.dataset.umetric; renderUsagePage(); });
  $$("[data-uproj]").forEach(x => x.onclick = () => { state.libProjectId = x.dataset.uproj; state.libMode = "project"; state.reviewPid = null; state.libTab = "status"; store("kb.libTab", "status"); showView("library"); });
}
$("#ticker").onclick = () => showView("usage");
