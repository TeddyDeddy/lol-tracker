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


def _round_sort_key(rnd: str, matches: list[dict]) -> tuple:
    """
    @brief Ordering key for one bracket column (round/tab).

    Prefers Leaguepedia's own `N_TabInPage` ordinal (authoritative — it's the
    exact order their own page renders tabs in) when the round's matches were
    ingested with it. Falls back to the old name-based `_round_key` heuristic
    for matches ingested before that field existed.

    @param rnd Round/tab display name (the grouping key used by `bracket()`).
    @param matches Matches already grouped under this round name.
    @return Sortable tuple; N_TabInPage-backed rounds always sort as a group
            before name-heuristic rounds (fine in practice — ingestion runs
            per tournament, so a given tournament's matches are consistently
            either fully migrated or not).
    """
    n = next((m["n_tab_in_page"] for m in matches
              if m.get("n_tab_in_page") is not None), None)
    if n is not None:
        return (0, n)
    return (1,) + _round_key(rnd)


def bracket(con: sqlite3.Connection, op: str) -> list[dict]:
    """
    @brief Build the round-by-round bracket/results structure for a tournament.

    Groups `pro_matches` by round (`round` column, falling back to `tab`, then
    a literal "Zápasy"), orders rounds by `_round_sort_key` and matches within
    a round by Leaguepedia's `N_MatchInTab` ordinal when available (else by
    date). Each column is flagged `is_bracket` — False when its matches carry
    a `group_name` (round-robin/group-stage rounds, e.g. MSI's Play-In, render
    as a results list rather than a bracket tree; see `web/templates/pro_tournament.html`).

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
    cols = [{"round": r, "matches": rounds[r],
             "is_bracket": not any(m.get("group_name") for m in rounds[r])}
            for r in sorted(rounds, key=lambda r: _round_sort_key(r, rounds[r]))]
    _link_feeds(cols)
    return cols


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
