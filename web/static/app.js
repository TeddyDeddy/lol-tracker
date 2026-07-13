/* Sdílený tooltip + line chart (rolling winrate) + live badge refresh. */
const tooltip = document.getElementById("tooltip");

function showTip(html, x, y) {
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  const pad = 14;
  tooltip.style.left = Math.min(x + pad, window.innerWidth - tooltip.offsetWidth - 8) + "px";
  tooltip.style.top = (y - tooltip.offsetHeight - pad < 0 ? y + pad : y - tooltip.offsetHeight - pad) + "px";
}
/** @brief Hide the shared tooltip. */
function hideTip() { tooltip.hidden = true; }

/**
 * @brief Wire up tooltips for every `[data-tip]` element (e.g. champion pool rows).
 *
 * Position is computed once on `mouseenter`, not on `mousemove` — recomputing
 * continuously made the tooltip jitter/drift while hovering over a wide element.
 */
for (const el of document.querySelectorAll("[data-tip]")) {
  el.addEventListener("mouseenter", e => showTip(el.dataset.tip, e.clientX, e.clientY));
  el.addEventListener("mouseleave", hideTip);
}

/* refresher callbacks for each champ-search box, keyed by target container id — makeSortable
   calls these after re-ordering rows so a search+collapse cap re-applies against the new
   (sorted) row order instead of the original page-load order. */
const champSearchRefreshers = {};

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
      if (champSearchRefreshers[table.id]) champSearchRefreshers[table.id]();
    });
  }
}
for (const table of document.querySelectorAll("table.sortable")) makeSortable(table);

/**
 * @brief Wire up a champion search box: filters `[data-name]` rows/cards in the target
 *        container by substring match, and — if the input carries `data-collapse="N"` — caps
 *        the match list to the first N (in current DOM order) with a paired `.show-more`
 *        button to reveal the rest. Typing a query always searches the full set, ignoring
 *        the cap, and auto-hides the button while a query is active.
 *
 * Registers its refresh function in `champSearchRefreshers` keyed by container id, so
 * `makeSortable` can re-run it after a sort — otherwise the cap would keep showing the
 * pre-sort top N instead of the top N in the newly sorted order.
 *
 * @param input `<input class="champ-search" data-target="...">` element.
 */
function initChampSearch(input) {
  const container = document.getElementById(input.dataset.target);
  if (!container) return;
  const cap = parseInt(input.dataset.collapse, 10) || 0;
  const moreBtn = document.querySelector(`.show-more[data-target="${input.dataset.target}"]`);
  let showAll = false;

  function apply() {
    const q = input.value.trim().toLowerCase();
    const capped = cap && !showAll && !q;
    let shown = 0;
    for (const el of container.querySelectorAll("[data-name]")) {
      const matches = el.dataset.name.toLowerCase().includes(q);
      const visible = matches && !(capped && shown >= cap);
      el.hidden = !visible;
      if (visible) shown++;
    }
    if (moreBtn && cap) {
      const total = container.querySelectorAll("[data-name]").length;
      moreBtn.hidden = !!q || total <= cap;
      moreBtn.textContent = showAll ? "▲ méně" : `▼ zobrazit všechny (${total})`;
    }
  }

  input.addEventListener("input", apply);
  if (moreBtn) moreBtn.addEventListener("click", () => { showAll = !showAll; apply(); });
  if (input.dataset.target) champSearchRefreshers[input.dataset.target] = apply;
  apply();
}
for (const input of document.querySelectorAll(".champ-search")) initChampSearch(input);

/**
 * @brief Flag `input[list]` text that can't possibly complete to a known option (e.g. a
 *        typo'd champion name) by toggling `.invalid` on every keystroke.
 *
 * Checks as soon as no `<datalist>` option starts with what's typed so far,
 * rather than waiting for an exact-match check on submit.
 */
for (const input of document.querySelectorAll("input[list]")) {
  const datalist = input.list;
  if (!datalist) continue;
  const names = [...datalist.options].map(o => o.value.toLowerCase());
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    input.classList.toggle("invalid", !!q && !names.some(n => n.startsWith(q)));
  });
}

/**
 * @brief Whole-row click on `.row-click` rows, delegating to the row's own `<a>`.
 *
 * Lets a table row act as a single clickable link without wrapping every
 * cell in an anchor; clicks on a real `<a>` inside the row keep their own
 * target instead of being overridden.
 */
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
