// ---------- markdown (small, escape-first)
function inline(s) {
  return s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^\w])_([^_\s][^_]*)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function md(src) {
  const lines = esc(src).split("\n"); const out = []; let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) { const buf = []; i++; while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]); i++; out.push(`<pre><code>${buf.join("\n")}</code></pre>`); continue; }
    let m;
    if ((m = l.match(/^(#{1,6})\s+(.*)/))) { const n = Math.min(m[1].length, 4); out.push(`<h${n}>${inline(m[2])}</h${n}>`); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { out.push("<hr>"); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => inline(c.trim()));
      let h = `<table><thead><tr>${cells(l).map(c => `<th>${c}</th>`).join("")}</tr></thead><tbody>`; i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) h += `<tr>${cells(lines[i++]).map(c => `<td>${c}</td>`).join("")}</tr>`;
      out.push(h + "</tbody></table>"); continue;
    }
    if (/^&gt;\s?/.test(l)) { const buf = []; while (i < lines.length && /^&gt;\s?/.test(lines[i])) buf.push(lines[i++].replace(/^&gt;\s?/, "")); out.push(`<blockquote>${inline(buf.join("<br>"))}</blockquote>`); continue; }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(l)) {
      const ordered = /^\s*\d/.test(l); const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        let item = lines[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, "");
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) item += " " + lines[i++].trim();
        items.push(`<li>${inline(item.replace(/^\[ \]\s/, "☐ ").replace(/^\[x\]\s/i, "☑ "))}</li>`);
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`); continue;
    }
    if (!l.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|&gt;|\s*([-*+]|\d+[.)])\s+|\s*\|.*\|\s*$)/.test(lines[i])) buf.push(lines[i++]);
    if (!buf.length) buf.push(lines[i++]);
    out.push(`<p>${inline(buf.join("\n"))}</p>`);
  }
  return out.join("\n");
}
