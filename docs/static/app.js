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

/* ---------- rolling winrate line chart ---------- */
const chartEl = document.getElementById("wr-chart");
if (chartEl) {
  const data = JSON.parse(chartEl.dataset.series);
  const W = chartEl.clientWidth || 900, H = 220;
  const M = { l: 40, r: 14, t: 12, b: 24 };
  const xs = i => M.l + (i / (data.length - 1)) * (W - M.l - M.r);
  const ys = wr => M.t + (1 - wr / 100) * (H - M.t - M.b);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const add = (tag, attrs, parent = svg) => {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    parent.appendChild(el);
    return el;
  };

  // mřížka: 0/25/50/75/100 %
  for (const v of [0, 25, 50, 75, 100]) {
    add("line", { x1: M.l, x2: W - M.r, y1: ys(v), y2: ys(v),
                  stroke: "#2c2c2a", "stroke-width": 1 });
    add("text", { x: M.l - 8, y: ys(v) + 4, "text-anchor": "end",
                  fill: "#898781", "font-size": 11 }).textContent = v + " %";
  }
  // 50% referenční linka o chlup výraznější
  add("line", { x1: M.l, x2: W - M.r, y1: ys(50), y2: ys(50),
                stroke: "#383835", "stroke-width": 1 });

  // plocha (wash 10 %) + čára 2px
  const pts = data.map((d, i) => `${xs(i)},${ys(d.wr)}`).join(" ");
  add("polygon", { points: `${M.l},${ys(0)} ${pts} ${W - M.r},${ys(0)}`,
                   fill: "#3987e5", opacity: 0.1 });
  add("polyline", { points: pts, fill: "none", stroke: "#3987e5",
                    "stroke-width": 2, "stroke-linejoin": "round",
                    "stroke-linecap": "round" });

  // koncový bod s ringem + přímý popisek poslední hodnoty
  const last = data[data.length - 1];
  add("circle", { cx: xs(data.length - 1), cy: ys(last.wr), r: 5,
                  fill: "#3987e5", stroke: "#1a1a19", "stroke-width": 2 });
  add("text", { x: xs(data.length - 1) - 6, y: ys(last.wr) - 10,
                "text-anchor": "end", fill: "#ffffff", "font-size": 12,
                "font-weight": 600 }).textContent = last.wr.toFixed(0) + " %";

  // crosshair + hover
  const cross = add("line", { y1: M.t, y2: H - M.b, stroke: "#898781",
                              "stroke-width": 1, visibility: "hidden" });
  const dot = add("circle", { r: 5, fill: "#3987e5", stroke: "#1a1a19",
                              "stroke-width": 2, visibility: "hidden" });
  svg.addEventListener("mousemove", e => {
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const i = Math.max(0, Math.min(data.length - 1,
        Math.round(((px - M.l) / (W - M.l - M.r)) * (data.length - 1))));
    const d = data[i];
    cross.setAttribute("x1", xs(i)); cross.setAttribute("x2", xs(i));
    cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", xs(i)); dot.setAttribute("cy", ys(d.wr));
    dot.setAttribute("visibility", "visible");
    showTip(`<b>${d.wr.toFixed(0)} % WR</b><br>okno 10 her · ${d.when}`,
            e.clientX, e.clientY);
  });
  svg.addEventListener("mouseleave", () => {
    cross.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
    hideTip();
  });
  chartEl.appendChild(svg);
}

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
