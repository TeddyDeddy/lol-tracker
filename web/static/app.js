/* Sdílený tooltip + line chart (rolling winrate) + live badge refresh. */
const tooltip = document.getElementById("tooltip");

function showTip(html, x, y) {
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  const pad = 14;
  tooltip.style.left = Math.min(x + pad, window.innerWidth - tooltip.offsetWidth - 8) + "px";
  tooltip.style.top = (y - tooltip.offsetHeight - pad < 0 ? y + pad : y - tooltip.offsetHeight - pad) + "px";
}
function hideTip() { tooltip.hidden = true; }

/* tooltipy pro elementy s data-tip (champion pool) */
for (const el of document.querySelectorAll("[data-tip]")) {
  el.addEventListener("mousemove", e => showTip(el.dataset.tip, e.clientX, e.clientY));
  el.addEventListener("mouseleave", hideTip);
}

/**
 * @brief Draw a minimal LP sparkline into a `.rank-chart` element.
 *
 * Autoscales to the series' own min/max (with a small padding) rather than a
 * fixed 0-100 range, since LP has no universal ceiling. Series are usually
 * short/flat right now (rank history only just started being recorded) —
 * this still renders correctly and will show real trends as history grows.
 *
 * @param el Container element with a `data-series` JSON attribute:
 *           [{lp, tier, division, when}, ...], chronological.
 */
function renderRankSpark(el) {
  const data = JSON.parse(el.dataset.series);
  const W = el.clientWidth || 260, H = 60;
  const M = { l: 4, r: 4, t: 8, b: 8 };
  const lps = data.map(d => d.lp);
  const lo = Math.min(...lps), hi = Math.max(...lps);
  const pad = Math.max(5, (hi - lo) * 0.15);
  const yMin = lo - pad, yMax = hi + pad;
  const xs = i => M.l + (i / (data.length - 1)) * (W - M.l - M.r);
  const ys = v => M.t + (1 - (v - yMin) / (yMax - yMin || 1)) * (H - M.t - M.b);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const add = attrs => {
    const line = document.createElementNS(NS, "polyline");
    for (const [k, v] of Object.entries(attrs)) line.setAttribute(k, v);
    svg.appendChild(line);
    return line;
  };
  const pts = data.map((d, i) => `${xs(i)},${ys(d.lp)}`).join(" ");
  add({ points: pts, fill: "none", stroke: "#3987e5", "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round" });
  const last = data[data.length - 1];
  const dot = document.createElementNS(NS, "circle");
  dot.setAttribute("cx", xs(data.length - 1));
  dot.setAttribute("cy", ys(last.lp));
  dot.setAttribute("r", 3.5);
  dot.setAttribute("fill", "#3987e5");
  dot.setAttribute("stroke", "#1a1a19");
  dot.setAttribute("stroke-width", 1.5);
  svg.appendChild(dot);
  el.appendChild(svg);
  el.title = `${last.tier} ${last.division} · ${last.lp} LP`;
}
for (const el of document.querySelectorAll(".rank-chart")) renderRankSpark(el);

/**
 * @brief Wire up click-to-sort headers on a `table.sortable`.
 *
 * Reads `data-<key>` on each row for the sort key's value; numeric columns
 * compare as numbers, everything else falls back to locale string compare.
 *
 * @param table The `<table class="sortable">` element to make sortable.
 */
function makeSortable(table) {
  const tbody = table.querySelector("tbody");
  for (const th of table.querySelectorAll("th[data-sort]")) {
    let asc = false;
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      asc = !asc;
      const rows = [...tbody.querySelectorAll("tr")];
      rows.sort((a, b) => {
        const av = a.dataset[key], bv = b.dataset[key];
        const an = parseFloat(av), bn = parseFloat(bv);
        const cmp = Number.isNaN(an) || Number.isNaN(bn)
          ? av.localeCompare(bv) : an - bn;
        return asc ? cmp : -cmp;
      });
      for (const r of rows) tbody.appendChild(r);
      for (const h of table.querySelectorAll("th[data-sort]"))
        h.classList.remove("sort-asc", "sort-desc");
      th.classList.add(asc ? "sort-asc" : "sort-desc");
    });
  }
}
for (const table of document.querySelectorAll("table.sortable")) makeSortable(table);

/* ---------- whole-row click on `.row-click` rows (uses the row's own <a>) ---------- */
for (const tr of document.querySelectorAll("tr.row-click")) {
  tr.addEventListener("click", e => {
    if (e.target.closest("a")) return;  // real links keep their own behavior
    const link = tr.querySelector("a");
    if (link) location.href = link.href;
  });
}

/**
 * @brief Whole-card click on `.card-click` cards that also contain a nested
 *        secondary link (e.g. "meta →").
 *
 * A card can't be a single `<a>` when it needs a second, differently-targeted
 * link inside it (nested anchors are invalid HTML) — so the card carries
 * `data-href` for the primary target and this delegates clicks to it, except
 * when the click landed on a real `<a>`, which keeps its own href.
 */
for (const card of document.querySelectorAll(".card-click")) {
  card.addEventListener("click", e => {
    if (e.target.closest("a")) return;
    if (card.dataset.href) location.href = card.dataset.href;
  });
}

/**
 * @brief Wire up phase tab buttons (Základní část / Play-off / Play-In / ...)
 *        on a tournament page to switch which `.phase-panel` is visible.
 *
 * Redraws every bracket in the shown panel's connectors on switch — a
 * double-elimination phase renders THREE separate `.bracket.playoffs` roots
 * (upper/lower/grand-final), not one, so this must redraw all of them, not
 * just the first. A bracket drawn while its panel was `hidden` gets
 * zero-sized rects from `getBoundingClientRect`, so the initial draw() on
 * page load is a no-op for any non-default tab and must be redone once the
 * panel becomes visible.
 */
function initPhaseTabs() {
  const tabs = document.querySelector(".phase-tabs");
  if (!tabs) return;
  const buttons = [...tabs.querySelectorAll(".phase-tab")];
  const panels = [...document.querySelectorAll(".phase-panel")];
  for (const btn of buttons) {
    btn.addEventListener("click", () => {
      for (const b of buttons) b.classList.remove("active");
      btn.classList.add("active");
      for (const p of panels) p.hidden = p.dataset.phase !== btn.dataset.phase;
      const shown = panels.find(p => p.dataset.phase === btn.dataset.phase);
      for (const bracket of shown ? shown.querySelectorAll(".bracket.playoffs") : [])
        if (bracket._redraw) bracket._redraw();
    });
  }
}
initPhaseTabs();

/* ---------- live badge refresh (à 60 s) ---------- */
async function refreshLive() {
  try {
    const rows = await (await fetch("/api/live")).json();
    const liveIds = new Set(rows.map(r => r.riot_id));
    for (const b of document.querySelectorAll(".live-badge"))
      b.hidden = !liveIds.has(b.dataset.riotId);
    const summary = document.getElementById("live-summary");
    if (summary)
      summary.textContent = rows.length
        ? `🎮 právě hraje: ${rows.map(r => r.riot_id).join(", ")}`
        : "";
  } catch {}
}
refreshLive();
setInterval(refreshLive, 60000);
