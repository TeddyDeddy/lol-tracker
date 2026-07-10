from lol import db, patchnotes

# Minimal fixture mirroring Riot's real patch-notes markup (verified live
# against patch 26.13 and 25.13) — one champion block with a dev-commentary
# blockquote + two per-ability stat lists, one item block that must be
# skipped (identical `patch-change-block` markup, but the change-title link
# points at /how-to-play/ instead of /champions/{slug}/).
_FIXTURE_HTML = """
<header class="header-primary"><h2 id="patch-champions">Champions</h2></header>
<div class="content-border">
  <div class="patch-change-block white-stone accent-before">
    <div>
      <h3 class="change-title" id="patch-aphelios">
        <a href="https://www.leagueoflegends.com/en-us/champions/aphelios/">Aphelios</a>
      </h3>
      <blockquote class="blockquote context">
        <p>We're pulling back on some of those earlier nerfs.</p>
      </blockquote>
      <hr class="divider">
      <h4 class="change-detail-title ability-title">Calibrum</h4>
      <ul>
        <li><strong>Passive Mark Damage</strong>: 15 (10% bonus AD) &#8658; <strong>15 (15% bonus AD)</strong></li>
      </ul>
      <hr class="divider">
      <h4 class="change-detail-title ability-title">Severum</h4>
      <ul>
        <li><strong>Q Damage</strong>: 19 &#8658; <strong>20</strong></li>
      </ul>
    </div>
  </div>
</div>
<div class="content-border">
  <div class="patch-change-block white-stone accent-before">
    <div>
      <h3 class="change-title" id="patch-brand"><a href="https://www.leagueoflegends.com/en-us/champions/brand/">Brand</a></h3>
      <blockquote class="blockquote context"><p>We're nerfing his early passive detonation damage.</p></blockquote>
      <ul><li><strong>Passive Damage</strong>: 100 &#8658; <strong>80</strong></li></ul>
    </div>
  </div>
</div>
<header class="header-primary"><h2 id="patch-items">Items</h2></header>
<div class="content-border">
  <div class="patch-change-block white-stone accent-before">
    <div>
      <h3 class="change-title" id="patch-Dorans-Helm"><a href="https://www.leagueoflegends.com/en-us/how-to-play/">Doran's Helm</a></h3>
      <blockquote class="blockquote context"><p>We overshot the buff, tapping it back down.</p></blockquote>
      <ul><li><strong>Health</strong>: 140 &#8658; <strong>150</strong></li></ul>
    </div>
  </div>
</div>
""".replace("&#8658;", "⇒")


def test_parse_champion_changes_extracts_only_champion_blocks():
    changes = patchnotes.parse_champion_changes(_FIXTURE_HTML)
    names = [c for c, _, _ in changes]
    assert names == ["Aphelios", "Brand"]          # Doran's Helm (item) excluded


def test_parse_champion_changes_joins_stat_bullets_with_arrow():
    changes = dict((c, n) for c, _, n in patchnotes.parse_champion_changes(_FIXTURE_HTML))
    note = changes["Aphelios"]
    assert "pulling back on some of those earlier nerfs" in note
    assert "Passive Mark Damage: 15 (10% bonus AD) → 15 (15% bonus AD)" in note
    assert "Q Damage: 19 → 20" in note


def test_parse_champion_changes_classifies_from_commentary():
    changes = dict((c, k) for c, k, _ in patchnotes.parse_champion_changes(_FIXTURE_HTML))
    # "pulling back on...nerfs" matches BOTH word lists (buff phrase, and
    # "nerf" as a substring of "nerfs") -> correctly falls to the safe
    # "adjust" fallback rather than guessing a direction from a substring hit.
    assert changes["Aphelios"] == "adjust"
    assert changes["Brand"] == "nerf"        # "nerfing his...damage" -> nerf


def test_classify_mixed_signal_is_adjust():
    assert patchnotes._classify("increased base damage but reduced cooldown range") == "adjust"


def test_patch_urls_strips_leading_zero_from_minor():
    # real bug found live: "26.01" must build ".../patch-26-1-notes", not
    # ".../patch-26-01-notes" — Riot's own URLs never zero-pad the minor
    # number, so the padded form always 404s.
    urls = patchnotes._patch_urls("26.01")
    assert any(u.endswith("patch-26-1-notes") for u in urls)
    assert not any("26-01" in u for u in urls)


def test_fetch_patch_html_returns_none_for_malformed_patch_string():
    assert patchnotes.fetch_patch_html("") is None
    assert patchnotes.fetch_patch_html("notapatch") is None


def test_ingest_patch_notes_skips_patches_with_no_page(monkeypatch):
    """No live network in tests — stub fetch_patch_html to simulate an
    unreleased/hotfix-only patch with no dedicated notes page."""
    monkeypatch.setattr(patchnotes, "fetch_patch_html", lambda patch: None)
    con = db.connect(":memory:")
    n = patchnotes.ingest_patch_notes(con, ["99.99"])
    assert n == 0
    assert con.execute("SELECT COUNT(*) FROM patch_changes").fetchone()[0] == 0


def test_ingest_patch_notes_upserts_parsed_changes(monkeypatch):
    monkeypatch.setattr(patchnotes, "fetch_patch_html", lambda patch: _FIXTURE_HTML)
    con = db.connect(":memory:")
    n = patchnotes.ingest_patch_notes(con, ["26.13"])
    assert n == 2
    row = con.execute(
        "SELECT kind, note FROM patch_changes WHERE patch = ? AND champion = ?",
        ("26.13", "Brand")).fetchone()
    assert row["kind"] == "nerf"
    assert "nerfing his early passive detonation damage" in row["note"]
