"""
@brief Scrape official Riot patch notes as the buff/nerf data source for the
       /pro/meta page.

Leaguepedia's `PatchNotes` Cargo table is unusable in practice — anonymous
access never got past the rate limit in an entire session (6 attempts,
escalating backoff, still `RuntimeError` every time). Riot's own patch-notes
page is static HTML with no rate limit and a stable structure instead:
each champion is a `<div class="patch-change-block">` containing an `<h3
class="change-title">` (name), an optional `<blockquote class="blockquote
context">` (dev commentary, almost always explicitly says "buff"/"nerf"/
"pulling back on nerfs" etc.), and per-ability `<ul><li><strong>Stat</strong>:
old ⇒ <strong>new</strong></li></ul>` blocks. Verified live against patch
26.13 (17 champions, 100% format match) and 25.13 (older URL style).

CLI: `python -m lol.patchnotes <patch>`    # test-parse a single patch
"""

import html as html_lib
import re
import sys

import httpx

from lol import db

HEADERS = {"User-Agent": "lol-tracker-hobby-project (Teddy, tadeasww22@gmail.com)"}
_client = httpx.Client(headers=HEADERS, timeout=15, follow_redirects=True)

# Riot změnil URL slug v průběhu 2025/2026 (starší "patch-X-Y-notes" ->
# novější "league-of-legends-patch-X-Y-notes") — zkusit obě, novější první.
_URL_TEMPLATES = (
    "https://www.leagueoflegends.com/en-us/news/game-updates/"
    "league-of-legends-patch-{maj}-{minor}-notes",
    "https://www.leagueoflegends.com/en-us/news/game-updates/"
    "patch-{maj}-{minor}-notes",
)

# Slova indikující směr balance změny ve vývojářském komentáři/textu změny.
_BUFF_WORDS = ("increased", "buffed", "buff", "reduced cooldown", "lowered cost",
              "faster", "pulling back on", "tuning up", "power up")
_NERF_WORDS = ("decreased", "nerfed", "nerf", "increased cooldown", "increased cost",
              "reduced", "slower", "tapping it back down", "toning down", "power down")

_BLOCK_SPLIT = '<div class="content-border">'
_TITLE_RE = re.compile(r'<h3 class="change-title"[^>]*>\s*<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.S)
_QUOTE_RE = re.compile(r'<blockquote class="blockquote context">(.*?)</blockquote>', re.S)
_ABILITY_RE = re.compile(
    r'<h4[^>]*class="change-detail-title[^"]*"[^>]*>(.*?)</h4>\s*<ul>(.*?)</ul>', re.S)
_UL_RE = re.compile(r"<ul>(.*?)</ul>", re.S)
_LI_RE = re.compile(r'<li[^>]*>(.*?)</li>', re.S)
_IMG_SRC_RE = re.compile(r'<img[^>]*src="([^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")
# Strips every tag EXCEPT <strong>/</strong> — Riot bolds the stat name and
# the new value in each bullet ("**Stat**: old ⇒ **new**"), and keeping that
# emphasis is the whole point of rendering the note as HTML instead of text.
_STRIP_NON_STRONG_RE = re.compile(r"</?(?!strong\b)[a-zA-Z][^>]*>")


def _classify(text: str) -> str:
    """
    @brief Best-effort buff/nerf/adjust classification from patch-note text.

    @param text Dev commentary or joined stat-change text (English).
    @return "buff" or "nerf" if the text unambiguously leans one way, else
            "adjust" (mixed direction or no matching keyword).
    """
    t = (text or "").lower()
    buffed = any(w in t for w in _BUFF_WORDS)
    nerfed = any(w in t for w in _NERF_WORDS)
    if buffed and not nerfed:
        return "buff"
    if nerfed and not buffed:
        return "nerf"
    return "adjust"


def _strip_tags(html: str) -> str:
    """@brief Strip all HTML tags and collapse whitespace down to single spaces."""
    return re.sub(r"\s+", " ", _TAG_RE.sub("", html)).strip()


def _stat_lines(ul_html: str) -> list[str]:
    """@brief Per-bullet HTML, keeping Riot's own `<strong>` emphasis on the
           stat name and new value, stripped of every other tag."""
    lines = []
    for li in _LI_RE.findall(ul_html):
        s = _STRIP_NON_STRONG_RE.sub("", li.replace("⇒", "→"))
        s = re.sub(r"\s+", " ", s).strip()
        if s:
            lines.append(s)
    return lines


def _note_html(block: str) -> str:
    """
    @brief Build the tooltip HTML for one champion's changes: the dev
           commentary (if any) followed by one ability icon + stat list per
           `<h4 ability-title><img>...</h4><ul>...</ul>` group — the same
           visual pairing Riot's own patch notes page uses.

    @param block One champion's `content-border` block HTML (see
           `parse_champion_changes`).
    @return HTML fragment (escaped text inside safe wrapper tags) to store in
            `patch_changes.note` and render via the `.tooltip` div's
            `innerHTML` on the web side (see `web/static/app.js:showTip`).
    """
    quote = _QUOTE_RE.search(block)
    commentary = _strip_tags(quote.group(1)) if quote else ""
    parts = [f'<p class="pn-commentary">{html_lib.escape(commentary)}</p>'] if commentary else []

    groups = _ABILITY_RE.findall(block)
    if groups:
        for header_html, ul_html in groups:
            stats = _stat_lines(ul_html)
            if not stats:
                continue
            icon = _IMG_SRC_RE.search(header_html)
            name = _strip_tags(header_html)
            icon_tag = (f'<img class="pn-icon" src="{html_lib.escape(icon.group(1))}" alt="">'
                       if icon else "")
            # `s` already went through `_stat_lines`, which strips every tag
            # except Riot's own trusted <strong> emphasis — do NOT re-escape
            # it here, that would turn the kept <strong> tags into literal text.
            items = "".join(f"<li>{s}</li>" for s in stats)
            parts.append(f'<div class="pn-ability">{icon_tag}<b>{html_lib.escape(name)}</b></div>'
                         f'<ul class="pn-stats">{items}</ul>')
    else:
        # Older/simpler pages sometimes list stats with no per-ability <h4>
        # grouping (no icon available) — still show them, just unlabeled.
        m = _UL_RE.search(block)
        stats = _stat_lines(m.group(1)) if m else []
        if stats:
            items = "".join(f"<li>{s}</li>" for s in stats)
            parts.append(f'<ul class="pn-stats">{items}</ul>')

    return "".join(parts)


def _patch_urls(patch: str) -> list[str]:
    """
    @brief Build candidate Riot patch-notes URLs for a patch string.

    @param patch e.g. "26.13" or "26.01" — Riot's own URLs never zero-pad the
           minor number ("26.01" is patch-26-1-notes, not patch-26-01-notes).
    @return Candidate URLs in try-order, or [] if `patch` isn't in the
            "MAJOR.MINOR" shape (e.g. an esports-only split tag "25.S1.1").
    """
    maj, _, minor = patch.partition(".")
    if not maj or not minor:
        return []
    if minor.isdigit():
        minor = str(int(minor))
    return [tmpl.format(maj=maj, minor=minor) for tmpl in _URL_TEMPLATES]


def fetch_patch_html(patch: str) -> str | None:
    """
    @brief Download the raw HTML of a patch's official Riot notes page.

    @param patch Patch string as reported in `pro_games.patch`.
    @return HTML text, or None if neither known URL slug exists for this
            patch (not yet released, a hotfix with no dedicated page, or a
            non-client patch label like an esports split tag "25.S1.1").
    """
    for url in _patch_urls(patch):
        try:
            r = _client.get(url)
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            return r.text
    return None


def parse_champion_changes(html: str) -> list[tuple[str, str, str]]:
    """
    @brief Extract every real champion balance change from a patch notes page.

    Skips item/augment/system blocks — they reuse the IDENTICAL
    `patch-change-block` markup as champion blocks, but their `change-title`
    link points at `/how-to-play/` instead of `/champions/{slug}/`, which is
    the only reliable discriminator (section-heading order isn't stable
    enough to depend on, and TFT content has been observed bleeding into
    unrelated fetches of the same URL via other tooling).

    @param html Raw HTML of one patch notes page (see `fetch_patch_html`).
    @return List of (champion, kind, note) — kind is buff/nerf/adjust; note is
            an HTML fragment (dev commentary + per-ability icon/stat-list
            groups, see `_note_html`) meant for the web tooltip, not plain text.
    """
    out = []
    for block in html.split(_BLOCK_SPLIT)[1:]:
        title = _TITLE_RE.search(block)
        if not title or "/champions/" not in title.group(1):
            continue
        champion = _strip_tags(title.group(2))
        if not champion:
            continue
        quote = _QUOTE_RE.search(block)
        commentary = _strip_tags(quote.group(1)) if quote else ""
        note = _note_html(block)
        kind = _classify(commentary or _strip_tags(note))
        out.append((champion, kind, note))
    return out


def ingest_patch_notes(con, patches: list[str]) -> int:
    """
    @brief Fetch official Riot patch notes for the given patches and upsert
           champion balance changes into `patch_changes`.

    Only covers patches that actually appear in `pro_games.patch` — no point
    fetching the whole balance-change history when we only need context for
    events we've actually ingested.

    @param con     Open sqlite3 connection.
    @param patches Patch version strings as `pro_games.patch` reports them.
    @return Number of (patch, champion) rows upserted.
    """
    n = 0
    for patch in patches:
        if not patch:
            continue
        html = fetch_patch_html(patch)
        if not html:
            continue
        for champion, kind, note in parse_champion_changes(html):
            con.execute("INSERT OR REPLACE INTO patch_changes VALUES (?,?,?,?)",
                       (patch, champion, kind, note))
            n += 1
    con.commit()
    return n


if __name__ == "__main__":
    patch = sys.argv[1] if len(sys.argv) > 1 else "26.13"
    html = fetch_patch_html(patch)
    if not html:
        print(f"patch {patch}: stránka nenalezena")
        sys.exit(1)
    changes = parse_champion_changes(html)
    print(f"patch {patch}: {len(changes)} championů")
    for champion, kind, note in changes:
        print(f"  {champion:20s} {kind:7s} {_strip_tags(note)[:100]}")
