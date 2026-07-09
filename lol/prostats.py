"""Statistiky pro-scény nad tabulkami pro_* (zdroj: Leaguepedia).

Čisté funkce nad sqlite — používá web (web/app.py). Picky/bany jsou
v pro_games jako CSV v pořadí draftu.
"""

import re
import sqlite3

from lol.leaguepedia import LEAGUE_SHORT


def short(league: str | None) -> str:
    return LEAGUE_SHORT.get(league or "", league or "?")


def _split(csv: str | None) -> list[str]:
    return [c.strip() for c in (csv or "").split(",") if c.strip()]


def tournament_games(con: sqlite3.Connection, op: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM pro_games WHERE overview_page = ? ORDER BY date", (op,))]


def tournament_champs(con: sqlite3.Connection, op: str) -> list[dict]:
    """Per champion: picks, bans, presence %, winrate (z picků)."""
    games = tournament_games(con, op)
    stats: dict[str, dict] = {}

    def bump(champ, key, win=0):
        s = stats.setdefault(champ, {"champion": champ, "picks": 0, "bans": 0,
                                     "wins": 0})
        s[key] += 1
        s["wins"] += win

    for g in games:
        for team, picks in ((1, g["team1_picks"]), (2, g["team2_picks"])):
            team_name = g["team1"] if team == 1 else g["team2"]
            for c in _split(picks):
                bump(c, "picks", int(g["winner"] == team_name))
        for bans in (g["team1_bans"], g["team2_bans"]):
            for c in _split(bans):
                bump(c, "bans")
    n = max(len(games), 1)
    out = []
    for s in stats.values():
        s["games"] = len(games)
        s["presence"] = 100 * (s["picks"] + s["bans"]) / n
        s["winrate"] = 100 * s["wins"] / s["picks"] if s["picks"] else None
        out.append(s)
    return sorted(out, key=lambda s: -s["presence"])


def tournament_teams(con: sqlite3.Connection, op: str) -> list[dict]:
    """Týmy turnaje s bilancí her."""
    teams: dict[str, dict] = {}
    for g in tournament_games(con, op):
        for name in (g["team1"], g["team2"]):
            if not name:
                continue
            t = teams.setdefault(name, {"team": name, "games": 0, "wins": 0})
            t["games"] += 1
            t["wins"] += int(g["winner"] == name)
    return sorted(teams.values(), key=lambda t: (-t["wins"], t["team"]))


def _round_key(name: str) -> tuple:
    """Přirozené pořadí kol podle NÁZVU — fallback pro staré řádky bez
    N_TabInPage (viz `_round_sort_key`). Round 1 < Round 2 … < Semifinals < Finals."""
    m = re.search(r"(\d+)", name)
    if m:
        return (0, int(m.group(1)))
    lowered = name.lower()
    for i, kw in enumerate(("quarter", "semi", "final")):
        if kw in lowered:
            return (1, i)
    return (2, 0)


def _round_is_bracket(name: str, matches: list[dict], has_finals: bool) -> bool:
    """
    @brief Decide whether a round/tab is bracket-shaped (elimination) or
           standings-shaped (round-robin/group).

    Neither a per-round structural signal alone works here. "One match per
    team this round" looks like a clean bracket tell, but real LPL playoff
    brackets break it: their bracket format lets a team play TWO series
    within one "Round N" tab (a lower-bracket drop replayed in the same
    round label), so that check misclassifies genuine bracket rounds as
    standings. Cross-round "do losers reappear?" tracking looked promising
    too, but breaks at the season→playoffs seam — a team can lose one
    regular-season match and still legitimately qualify for playoffs by
    overall standings, so a "loser" from the last round-robin week
    reappears in the first bracket round for reasons that have nothing to
    do with round-robin vs. bracket shape.

    What actually holds across every tournament shape found in this DB
    (round-robin-only, bracket-only, season+playoffs combined, play-in+
    bracket combined): round NAMING plus one page-level fact (whether a
    Finals/Semifinal/Quarterfinal round exists anywhere on the page).
      1. `group_name` set -> standings (authoritative, once a future ingest
         populates it for tournaments ingested before it existed).
      2. Round name says "week" -> standings; "bracket" -> bracket
         (Leaguepedia literally names MSI-style rounds "Bracket Round N");
         "final" (also matches semifinal/quarterfinal) -> bracket;
         "group"/"swiss" -> standings. "play-in" is deliberately NOT in this
         list: real Play-In stages (LPL/LCK "Play-In Round N") are elimination
         gauntlets, not round-robins — they fall through to rule 3 like any
         other numbered round and get classified bracket via `has_finals`.
      3. Remaining ambiguous case ("Round N"/"Play-In Round N" with no other
         signal): bracket only if this OverviewPage ALSO has a Finals/
         Semifinal/Quarterfinal-named round somewhere — real playoff brackets
         (Play-In included) end in one; standalone round-robin splits that
         reuse "Round N" for match days (e.g. PCS Split 3, LTA Split 1) never do.

    @param name Round/tab display name.
    @param matches Matches already grouped under this round.
    @param has_finals Whether this OverviewPage has any Finals/Semifinal/
           Quarterfinal-named round — used only for the ambiguous "Round N" case.
    @return True if the round should render as a bracket column.
    """
    if any(m.get("group_name") for m in matches):
        return False
    lowered = name.lower()
    if "week" in lowered:
        return False
    if "bracket" in lowered or any(kw in lowered for kw in ("final", "semifinal", "quarterfinal")):
        return True
    if any(kw in lowered for kw in ("group", "swiss")):
        return False
    return has_finals


def _round_sort_key(rnd: str, matches: list[dict], is_bracket: bool) -> tuple:
    """
    @brief Ordering key for one bracket/standings column (round/tab).

    Prefers Leaguepedia's own `N_TabInPage` ordinal (authoritative — it's the
    exact order their own page renders tabs in) when the round's matches were
    ingested with it. Otherwise sorts ALL standings-kind rounds before ALL
    bracket-kind rounds, then falls back to the name-based `_round_key`
    heuristic within each group.

    The standings-before-bracket bucketing (rather than relying purely on
    `_round_key`) matters for tournaments that combine a regular season and
    playoffs on one page (e.g. LTA splits): "Week 1" and "Round 1" both
    extract the digit 1 under `_round_key` and would otherwise tie/interleave,
    fragmenting what should be two clean phases. It also fixes play-in-style
    events (e.g. LPL Grand Finals' "Play-In" tab), which used to sort dead
    last after "Finals" under the old name-only heuristic.

    When the authoritative ordinal is present, it's additionally prefixed by
    `n_page` (Leaguepedia's page-break ordinal). `n_tab_in_page` RESETS on
    every page break — long tournaments get split across multiple Leaguepedia
    sub-pages (e.g. First Stand's group stage is page 1, its bracket is page
    2), so two rounds on different pages can share the same n_tab_in_page
    (page 1's "Groups Day 1" and page 2's "Semifinals" are both tab 1) and
    would tie/interleave without the page number leading the sort.

    @param rnd Round/tab display name (the grouping key used by `bracket()`).
    @param matches Matches already grouped under this round name.
    @param is_bracket This round's `_round_is_bracket` result.
    @return Sortable tuple.
    """
    n = next((m["n_tab_in_page"] for m in matches
              if m.get("n_tab_in_page") is not None), None)
    if n is not None:
        page = next((m["n_page"] for m in matches
                     if m.get("n_page") is not None), 0) or 0
        return (0, page, n)
    return (1, 0, int(is_bracket)) + _round_key(rnd)


def bracket(con: sqlite3.Connection, op: str) -> list[dict]:
    """
    @brief Build the round-by-round bracket/results structure for a tournament.

    Groups `pro_matches` by round (`round` column, falling back to `tab`, then
    a literal "Zápasy"), orders rounds by `_round_sort_key` and matches within
    a round by Leaguepedia's `N_MatchInTab` ordinal when available (else by
    date). Each column is flagged `is_bracket` via `_round_is_bracket`.

    @param op Tournament's Leaguepedia OverviewPage.
    @return List of {round, matches, is_bracket}, in play order. Each match
            dict additionally carries `feeds_from` (see `_link_feeds`).
    """
    rounds: dict[str, list[dict]] = {}
    for m in con.execute(
            "SELECT * FROM pro_matches WHERE overview_page = ?"
            " ORDER BY date", (op,)):
        m = dict(m)
        rnd = m["round"] or m["tab"] or "Zápasy"
        rounds.setdefault(rnd, []).append(m)
    for matches in rounds.values():
        matches.sort(key=lambda m: (
            m["n_match_in_tab"] if m.get("n_match_in_tab") is not None else 1 << 30,
            m["date"] or ""))
    has_finals = any(
        kw in r.lower() for r in rounds for kw in ("final", "semifinal", "quarterfinal"))
    shapes = {r: _round_is_bracket(r, rounds[r], has_finals) for r in rounds}
    cols = [{"round": r, "matches": rounds[r], "is_bracket": shapes[r]}
            for r in sorted(rounds,
                            key=lambda r: _round_sort_key(r, rounds[r], shapes[r]))]
    _link_feeds(cols)
    return cols


def _standings(matches: list[dict]) -> list[dict]:
    """
    @brief Compute a simple win/loss standings table for round-robin/group matches.

    This is a display approximation, not an official ranking — real leagues
    break ties with head-to-head results, game differential, etc., which
    aren't replicated here. Sorts by win rate, then series wins.

    @param matches `pro_matches` rows already filtered to one standings-kind phase.
    @return List of {team, wins, losses, games, game_wins, game_losses, winrate}.
    """
    teams: dict[str, dict] = {}
    for m in matches:
        if not m.get("winner"):
            continue
        for side, opp in ((1, 2), (2, 1)):
            team = m.get(f"team{side}")
            if not team:
                continue
            t = teams.setdefault(team, {"team": team, "wins": 0, "losses": 0,
                                        "game_wins": 0, "game_losses": 0})
            t["wins" if m["winner"] == team else "losses"] += 1
            t["game_wins"] += m.get(f"team{side}_score") or 0
            t["game_losses"] += m.get(f"team{opp}_score") or 0
    out = []
    for t in teams.values():
        games = t["wins"] + t["losses"]
        t["games"] = games
        t["winrate"] = 100 * t["wins"] / games if games else 0.0
        out.append(t)
    return sorted(out, key=lambda t: (-t["winrate"], -t["wins"]))


def _phase_label(kind: str, round_names: list[str]) -> str:
    """
    @brief Human display label for a phase tab, guessed from its round names.

    @param kind "bracket" or "standings" (see `tournament_phases`).
    @param round_names Round/tab names making up this phase.
    @return Czech display label for the phase tab button.
    """
    text = " ".join(round_names).lower()
    if "play-in" in text or "play in" in text:
        return "Play-In"
    if kind == "bracket":
        return "Play-off"
    if "swiss" in text:
        return "Swiss stage"
    if "group" in text:
        return "Skupinová fáze"
    return "Základní část"


def _is_play_in(round_name: str) -> bool:
    """@brief Whether a round/tab name marks it as a Play-In stage."""
    lowered = round_name.lower()
    return lowered.startswith("play-in") or lowered.startswith("play in")


def _bracket_sections(rounds: list[dict]) -> dict:
    """
    @brief Split a bracket-kind phase's rounds into upper/lower/grand-final
           sections when it's structurally double-elimination, or leave it as
           one tree when it's single-elimination — including gauntlet-style
           multi-decider stages (Play-In) that give some losers a second life
           but don't converge onto one final match.

    Leaguepedia exposes no upper/lower flag (`Phase`/`GroupName` are empty
    even on freshly-ingested data), so this reconstructs it purely from match
    results, in play order: a loss sends a team to the lower bracket UNLESS
    they play again later in the phase (a second life = they were still in
    the upper bracket when they lost — this also correctly handles teams
    seeded directly into the lower bracket with zero prior losses, e.g. LEC's
    5th-8th seeds, since it never looks at loss COUNT, only "do they play
    again"). The single deciding match is tagged Grand Final — but ONLY when
    the phase's last round has exactly one match; when it has several (Play-
    In's parallel decider round, several teams promoting at once with no
    single champion to converge on), there's nothing to call a "final", so
    the whole phase falls back to one tree instead of guessing wrong.

    @param rounds Bracket-kind rounds (as built by `bracket()`), in play order.
    @return `{"double": False, "rounds": rounds}` for a single/gauntlet tree,
            or `{"double": True, "upper": [...], "lower": [...], "final": [...]}`
            — same round-dict shape, each column filtered to that section's
            matches (empty columns dropped, original order preserved). Upper/
            lower get their own `_link_feeds` pass (connectors only within a
            section); the final section's match(es) get `feeds_from = []`
            since it's rendered as its own separate bracket root.
    """
    matches = [m for r in rounds for m in r["matches"]]
    later: dict[str, list[int]] = {}
    for i, m in enumerate(matches):
        for t in (m["team1"], m["team2"]):
            if t:
                later.setdefault(t, []).append(i)
    losses: dict[str, int] = {}
    tags: dict[str, str] = {}
    any_second_life = False
    for i, m in enumerate(matches):
        t1, t2, w = m["team1"], m["team2"], m["winner"]
        if not (t1 and t2 and w):
            continue
        loser = t2 if w == t1 else t1
        plays_again = any(j > i for j in later.get(loser, ()))
        if plays_again:
            any_second_life = True
        tags[m["match_id"]] = "U" if plays_again else "L"
        losses[loser] = losses.get(loser, 0) + 1

    last_round_size = len(rounds[-1]["matches"]) if rounds else 0
    if not any_second_life or last_round_size != 1:
        return {"double": False, "rounds": rounds}
    tags[rounds[-1]["matches"][0]["match_id"]] = "GF"

    def pick(kind: str) -> list[dict]:
        cols = []
        for r in rounds:
            ms = [m for m in r["matches"] if tags.get(m["match_id"]) == kind]
            if ms:
                cols.append({"round": r["round"], "matches": ms, "is_bracket": True})
        return cols

    upper, lower, final = pick("U"), pick("L"), pick("GF")
    _link_feeds(upper)
    _link_feeds(lower)
    for r in final:
        for m in r["matches"]:
            m["feeds_from"] = []
    return {"double": True, "upper": upper, "lower": lower, "final": final}


def tournament_phases(con: sqlite3.Connection, op: str) -> list[dict]:
    """
    @brief Group a tournament's `bracket()` rounds into UI-navigable phases.

    A phase is a maximal run of consecutive rounds that share both kind
    (bracket/standings) AND Play-In-ness — e.g. a season+playoffs page
    combined into one OverviewPage becomes two phases: "Základní část" then
    "Play-off". A Play-In stage followed by the main bracket becomes two
    SEPARATE bracket-kind phases (not merged) even though both are "bracket",
    because they're different elimination pools: a team's Play-In loss must
    not carry over as a loss when `_bracket_sections` reconstructs the main
    bracket's upper/lower split. A plain single-shape tournament (most
    playoff-only or season-only pages) yields exactly one phase; the web
    layer skips the tab UI entirely in that case.

    Standings-kind phases get a computed win/loss table (`phase["standings"]`,
    see `_standings`); bracket-kind phases get `phase["sections"]` (see
    `_bracket_sections`) instead — either a single tree or an upper/lower/
    grand-final split.

    @param op Tournament's Leaguepedia OverviewPage.
    @return List of {label, kind, rounds, standings, sections}, in play order.
    """
    rounds = bracket(con, op)
    phases: list[dict] = []
    for r in rounds:
        kind = "bracket" if r["is_bracket"] else "standings"
        key = (kind, _is_play_in(r["round"]))
        if phases and phases[-1]["_key"] == key:
            phases[-1]["rounds"].append(r)
        else:
            phases.append({"kind": kind, "rounds": [r], "_key": key})
    for p in phases:
        del p["_key"]
        p["label"] = _phase_label(p["kind"], [r["round"] for r in p["rounds"]])
        if p["kind"] == "standings":
            all_matches = [m for r in p["rounds"] for m in r["matches"]]
            p["standings"] = _standings(all_matches)
            p["sections"] = None
        else:
            p["standings"] = []
            p["sections"] = _bracket_sections(p["rounds"])
    return phases


def _link_feeds(cols: list[dict]) -> None:
    """
    @brief Derive bracket connector edges from team progression.

    A match is "fed by" the last earlier match each of its two teams played —
    Leaguepedia doesn't expose an explicit feeder-match field, so this is
    still a heuristic. Sets `m["feeds_from"]`. Also re-sorts a column's
    matches by average feeder position in the previous column, but ONLY when
    that column lacks Leaguepedia's own `N_MatchInTab` ordering — when the
    authoritative order is present (set by `bracket()`), it's trusted as-is
    rather than overridden by the heuristic.

    @param cols Bracket columns as built by `bracket()`, mutated in place.
    """
    last_match: dict[str, str] = {}   # tým -> match_id posledního zápasu
    for ci, col in enumerate(cols):
        for m in col["matches"]:
            feeds = [last_match[t] for t in (m["team1"], m["team2"])
                     if t and t in last_match]
            m["feeds_from"] = list(dict.fromkeys(feeds))
        for m in col["matches"]:
            for t in (m["team1"], m["team2"]):
                if t:
                    last_match[t] = m["match_id"]
        has_authoritative_order = col["matches"] and all(
            m.get("n_match_in_tab") is not None for m in col["matches"])
        if ci > 0 and not has_authoritative_order:
            prev_pos = {m["match_id"]: i
                        for i, m in enumerate(cols[ci - 1]["matches"])}

            def key(m):
                ps = [prev_pos[f] for f in m["feeds_from"] if f in prev_pos]
                return sum(ps) / len(ps) if ps else len(prev_pos)
            col["matches"].sort(key=key)


def series_games(con: sqlite3.Connection, match_id: str) -> list[dict]:
    """Hry jedné série vč. soupisek per hra."""
    games = [dict(r) for r in con.execute(
        "SELECT * FROM pro_games WHERE match_id = ? ORDER BY date", (match_id,))]
    for g in games:
        g["players"] = [dict(r) for r in con.execute(
            "SELECT * FROM pro_player_games WHERE game_id = ?"
            " ORDER BY team, CASE role WHEN 'Top' THEN 0 WHEN 'Jungle' THEN 1"
            " WHEN 'Mid' THEN 2 WHEN 'Bot' THEN 3 WHEN 'Support' THEN 4 END",
            (g["game_id"],))]
    return games


def team_form(con: sqlite3.Connection, team: str, before: str | None = None,
              n: int = 5) -> list[int]:
    """Posledních n sérií týmu (1=výhra), nejnovější první."""
    where, params = "", [team, team]
    if before:
        where, params = " AND date < ?", [team, team, before]
    return [int(r["winner"] == team) for r in con.execute(
        "SELECT winner FROM pro_matches"
        f" WHERE (team1 = ? OR team2 = ?) AND winner IS NOT NULL{where}"
        " ORDER BY date DESC LIMIT ?", (*params, n))]


def player_summary(con: sqlite3.Connection, player: str) -> list[dict]:
    """Per turnaj: hry, WR, KDA + champion breakdown (pool po splitech)."""
    rows = [dict(r) for r in con.execute(
        "SELECT pg.overview_page, t.name, t.date_start, p.champion,"
        " COUNT(*) games, SUM(p.win) wins,"
        " SUM(p.kills) k, SUM(p.deaths) d, SUM(p.assists) a"
        " FROM pro_player_games p"
        " JOIN pro_games pg ON pg.game_id = p.game_id"
        " LEFT JOIN pro_tournaments t ON t.overview_page = pg.overview_page"
        " WHERE p.player = ?"
        " GROUP BY pg.overview_page, p.champion"
        " ORDER BY t.date_start DESC, games DESC", (player,))]
    out: dict[str, dict] = {}
    for r in rows:
        t = out.setdefault(r["overview_page"], {
            "overview_page": r["overview_page"],
            "name": r["name"] or r["overview_page"],
            "date_start": r["date_start"],
            "games": 0, "wins": 0, "k": 0, "d": 0, "a": 0, "champs": []})
        t["games"] += r["games"]; t["wins"] += r["wins"]
        t["k"] += r["k"]; t["d"] += r["d"]; t["a"] += r["a"]
        t["champs"].append(r)
    for t in out.values():
        t["winrate"] = 100 * t["wins"] / t["games"]
        t["kda"] = (t["k"] + t["a"]) / max(t["d"], 1)
    return list(out.values())


def search_players(con: sqlite3.Connection, needle: str, limit=20) -> list[str]:
    return [r["player"] for r in con.execute(
        "SELECT player, COUNT(*) n FROM pro_player_games"
        " WHERE player LIKE ? GROUP BY player ORDER BY n DESC LIMIT ?",
        (f"%{needle}%", limit))]


def _presence(games: list[dict]) -> dict[str, dict]:
    """champ -> {picks, bans, presence} nad množinou her."""
    stats: dict[str, dict] = {}
    for g in games:
        for c in _split(g["team1_picks"]) + _split(g["team2_picks"]):
            stats.setdefault(c, {"picks": 0, "bans": 0})["picks"] += 1
        for c in _split(g["team1_bans"]) + _split(g["team2_bans"]):
            stats.setdefault(c, {"picks": 0, "bans": 0})["bans"] += 1
    n = max(len(games), 1)
    for s in stats.values():
        s["presence"] = 100 * (s["picks"] + s["bans"]) / n
    return stats


def event_meta_shift(con: sqlite3.Connection, event_op: str,
                     baseline_days: int = 60) -> dict:
    """Presence na eventu vs. hlavní ligy ~2 měsíce před ním + buff/nerf labely.

    Vrací {"event": turnaj, "patches": [...], "rows": [champ, before, at,
    delta, kind, note], "baseline_games": n}.
    """
    t = con.execute("SELECT * FROM pro_tournaments WHERE overview_page = ?",
                    (event_op,)).fetchone()
    if not t:
        return {}
    event_games = tournament_games(con, event_op)
    if not event_games:
        return {}
    baseline = [dict(r) for r in con.execute(
        "SELECT * FROM pro_games WHERE overview_page != ?"
        " AND date < ? AND date >= date(?, ?)",
        (event_op, t["date_start"], t["date_start"], f"-{baseline_days} days"))]
    at, before = _presence(event_games), _presence(baseline)
    patches = sorted({g["patch"] for g in event_games if g["patch"]})
    changes = {r["champion"]: dict(r) for r in con.execute(
        f"SELECT * FROM patch_changes WHERE patch IN ({','.join('?' * len(patches))})",
        patches)} if patches else {}
    rows = []
    for champ in set(at) | set(changes):
        a = at.get(champ, {}).get("presence", 0)
        b = before.get(champ, {}).get("presence", 0)
        ch = changes.get(champ, {})
        rows.append({"champion": champ, "before": b, "at": a, "delta": a - b,
                     "picks": at.get(champ, {}).get("picks", 0),
                     "bans": at.get(champ, {}).get("bans", 0),
                     "kind": ch.get("kind"), "note": ch.get("note")})
    rows.sort(key=lambda r: -abs(r["delta"]))
    return {"event": dict(t), "patches": patches, "rows": rows,
            "baseline_games": len(baseline), "event_games": len(event_games)}
