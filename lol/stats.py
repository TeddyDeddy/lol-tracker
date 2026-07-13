"""
@brief Player statistics and records computed from the local personal-tracker DB.

CLI: `python -m lol.stats "GameName#TAG"`. All functions return plain dicts/
lists so both the web app and the Discord bot can consume them directly.
"""

import datetime
import pathlib
import sqlite3
import sys

from lol import db

ROOT = pathlib.Path(__file__).parent.parent

# Skupiny queue_id pro filtrování módů (None = všechny hry)
QUEUE_GROUPS = {
    "All": None,  # ASCII klíč — jde do URL (wget2 láme ne-ASCII query při statickém exportu)
    "SoloQ": (420,),
    "Flex": (440,),
    "Normal": (400, 430, 480, 490),
    "ARAM": (450,),
    "URF": (900, 1900, 1010),
    "Swarm": (1810, 1820, 1830),
    "Arena": (1700, 1750),
}

# Módy se srovnatelnou škálou killů/CS/damage — pro rekordy, když je zvolen mód
# "All". Bez tohle by např. Swarm (PvE, jiné škály) nebo URF/Arena (bez CS,
# nafouknuté killy) přebily skutečné SR/ARAM rekordy.
STANDARD_QUEUES = QUEUE_GROUPS["SoloQ"] + QUEUE_GROUPS["Flex"] + QUEUE_GROUPS["Normal"] + QUEUE_GROUPS["ARAM"]

# queue_id -> zobrazované jméno (i pro módy mimo QUEUE_GROUPS — viz queue_counts).
QUEUE_NAMES = {
    420: "SoloQ", 440: "Flex", 400: "Draft", 430: "Blind",
    450: "ARAM", 480: "Swiftplay", 490: "Quickplay",
    900: "URF", 1900: "URF", 1010: "URF",
    700: "Clash", 710: "Clash", 720: "Clash",
    1400: "Ultimate Spellbook",
    1700: "Arena", 1750: "Arena",
    1810: "Swarm", 1820: "Swarm", 1830: "Swarm",
}

RECORDS = [
    ("Nejvíc killů", "kills", "MAX"),
    ("Nejvíc smrtí", "deaths", "MAX"),
    ("Nejvíc asistencí", "assists", "MAX"),
    ("Nejvíc CS", "cs", "MAX"),
    ("Nejvíc dmg do championů", "damage", "MAX"),
    ("Nejvíc goldů", "gold", "MAX"),
]


def _when(game_creation_ms: int) -> str:
    """
    @brief Format a Riot API epoch-millisecond timestamp as a Czech date.

    @param game_creation_ms Match creation timestamp in epoch milliseconds (Riot `gameCreation`).
    @return Date string in `D.M.YYYY` form (no zero-padding), e.g. "20.2.2025".
    """
    d = datetime.datetime.fromtimestamp(game_creation_ms / 1000)
    return f"{d.day}.{d.month}.{d.year}"


def fmt_duration(seconds: int) -> str:
    """@brief Format seconds as "M:SS"."""
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_int(n: int) -> str:
    """@brief Format an integer with space-separated thousands, e.g. "12 345"."""
    return f"{n:,}".replace(",", " ")


def _puuid(con: sqlite3.Connection, riot_id: str) -> str | None:
    """
    @brief Resolve a riot_id to its PUUID.

    Always filter by PUUID, never by `match_participants.riot_id` — that
    column stores the name as of match time, so an account rename would
    silently drop older games from a riot_id-based filter.

    @param con     Open sqlite3 connection.
    @param riot_id "GameName#TAG" identifier.
    @return The player's PUUID, or None if not tracked.
    """
    row = con.execute(
        "SELECT puuid FROM players WHERE riot_id = ?", (riot_id,)).fetchone()
    return row["puuid"] if row else None


def _queue_filter(queues) -> str:
    """@brief SQL fragment restricting to the given queue IDs, or "" for no filter."""
    if not queues:
        return ""
    return f" AND m.queue_id IN ({','.join(str(q) for q in queues)})"


def _filters(queues, season=None) -> str:
    """
    @brief SQL fragment combining the queue filter and an optional season (year) filter.

    @param queues Queue-id tuple, or None for all.
    @param season Optional year to filter to.
    @return SQL fragment starting with " AND ..." (or "" if no filters apply).
    """
    f = _queue_filter(queues)
    if season:
        f += (" AND strftime('%Y', m.game_creation/1000, 'unixepoch')"
              f" = '{int(season)}'")
    return f


def seasons(con: sqlite3.Connection) -> list[str]:
    """@brief Years we have any matches for, newest first."""
    return [r[0] for r in con.execute(
        "SELECT DISTINCT strftime('%Y', game_creation/1000, 'unixepoch')"
        " FROM matches WHERE game_creation > 0 ORDER BY 1 DESC")]


def champion_list(con: sqlite3.Connection, riot_id: str) -> list[str]:
    """
    @brief Every champion a player has played at least once, alphabetically.

    Feeds the champion-search `<datalist>` autocomplete.

    @param con     Open sqlite3 connection.
    @param riot_id Player's current riot id.
    @return Sorted list of champion names.
    """
    return [r[0] for r in con.execute(
        "SELECT DISTINCT champion FROM match_participants"
        " WHERE puuid = ? ORDER BY champion", (_puuid(con, riot_id),))]


def records(con: sqlite3.Connection, riot_id: str, queues=None, season=None,
           limit: int = 5) -> list[dict]:
    """
    @brief Find this player's single-game standout stats (top-`limit` per RECORDS
           entry, plus longest game and worst KDA), each linking back to its match.

    @param con     Open sqlite3 connection.
    @param riot_id Player's current riot id.
    @param queues Queue-id tuple to filter by (see QUEUE_GROUPS), or None for all.
    @param season Optional year filter.
    @param limit How many ranked games to keep per category (index 0 = the record).
    @return List of {label, entries}, entries being up to `limit` dicts of
            val, match_id, champion, game_creation, duration, win, kills, deaths,
            assists, queue_id — ranked best-first.
    """
    qf = _filters(queues, season)
    puuid = _puuid(con, riot_id)
    out = []
    for label, col, agg in RECORDS:
        rows = con.execute(
            f"SELECT p.{col} AS val, p.match_id, p.champion, m.game_creation,"
            f" m.duration, p.win, p.kills, p.deaths, p.assists, m.queue_id"
            f" FROM match_participants p JOIN matches m USING (match_id)"
            f" WHERE p.puuid = ?{qf} AND p.{col} IS NOT NULL"
            f" ORDER BY p.{col} DESC LIMIT ?",
            (puuid, limit),
        ).fetchall()
        if rows:
            out.append({"label": label, "entries": [dict(r) for r in rows]})
    rows = con.execute(
        "SELECT m.duration AS val, p.match_id, p.champion, m.game_creation,"
        " m.duration, p.win, p.kills, p.deaths, p.assists, m.queue_id"
        " FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ?{qf} ORDER BY m.duration DESC LIMIT ?",
        (puuid, limit),
    ).fetchall()
    if rows:
        out.append({"label": "Nejdelší hra", "entries": [dict(r) for r in rows]})
    # nejhorší KDA, min. 5 smrtí ať to není náhoda z jedné hry s jednou smrtí
    rows = con.execute(
        "SELECT p.match_id, p.champion, m.game_creation, m.duration, p.win,"
        " p.kills, p.deaths, p.assists, m.queue_id,"
        " 1.0 * (p.kills + p.assists) / p.deaths AS val"
        " FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ? AND p.deaths >= 5{qf} ORDER BY val ASC LIMIT ?",
        (puuid, limit),
    ).fetchall()
    if rows:
        out.append({"label": "Nejhorší KDA", "entries": [dict(r) for r in rows]})
    return out


def queue_counts(con: sqlite3.Connection, riot_id: str, season=None) -> list[dict]:
    """
    @brief Count games per real queue_id for a player, labeled via QUEUE_NAMES and
           aggregated by label (so e.g. both Arena queue ids collapse into one "Arena" row).

    Data-driven: reflects whatever queues the player actually has games in, not just the
    handful of modes wired up as filter pills in QUEUE_GROUPS (see `mode_pills` for those).

    @param con     Open sqlite3 connection.
    @param riot_id Player's current riot id.
    @param season  Optional year filter.
    @return List of {mode, games}, nonzero only, sorted by games desc.
    """
    puuid = _puuid(con, riot_id)
    rows = con.execute(
        "SELECT m.queue_id, COUNT(*) n FROM match_participants p"
        " JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ?{_filters(None, season)} GROUP BY m.queue_id",
        (puuid,)).fetchall()
    agg: dict[str, int] = {}
    for r in rows:
        label = QUEUE_NAMES.get(r["queue_id"], f"Queue {r['queue_id']}")
        agg[label] = agg.get(label, 0) + r["n"]
    return sorted(({"mode": k, "games": v} for k, v in agg.items()),
                  key=lambda x: -x["games"])


def mode_pills(con: sqlite3.Connection, riot_id: str, season=None) -> list[str]:
    """
    @brief Which QUEUE_GROUPS filter pills to show for a player: "All" plus every
           named group the player has at least one game in (season-filtered if given).

    @param con     Open sqlite3 connection.
    @param riot_id Player's current riot id.
    @param season  Optional year filter.
    @return QUEUE_GROUPS keys with games > 0, "All" always first.
    """
    puuid = _puuid(con, riot_id)
    out = ["All"]
    for name, queues in QUEUE_GROUPS.items():
        if name == "All":
            continue
        qf = _filters(queues, season)
        n = con.execute(
            "SELECT COUNT(*) FROM match_participants p JOIN matches m USING (match_id)"
            f" WHERE p.puuid = ?{qf}", (puuid,)).fetchone()[0]
        if n:
            out.append(name)
    return out


def summary(con: sqlite3.Connection, riot_id: str, queues=None, season=None,
           champion: str | None = None) -> dict | None:
    """
    @brief Aggregate win/loss/KDA/top-champion stats for a player.

    @param con      Open sqlite3 connection.
    @param riot_id  Player's current riot id.
    @param queues   Queue-id tuple to filter by, or None for all.
    @param season   Optional year filter.
    @param champion Optional exact champion name filter, applied server-side
           across the whole history (not just the current page).
    @return Dict with games/wins/winrate/kda/kills/deaths/assists/top_champs,
            or None if the player has no matching games.
    """
    qf = _filters(queues, season)
    params = [_puuid(con, riot_id)]
    if champion:
        qf += " AND p.champion = ?"
        params.append(champion)
    puuid = params[0]
    row = con.execute(
        "SELECT COUNT(*) games, SUM(win) wins, SUM(kills) k, SUM(deaths) d,"
        " SUM(assists) a FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ?{qf}",
        tuple(params),
    ).fetchone()
    if not row["games"]:
        return None
    top = con.execute(
        "SELECT champion, COUNT(*) games, SUM(win) wins"
        " FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ?{qf} GROUP BY champion ORDER BY games DESC LIMIT 5",
        tuple(params),
    ).fetchall()
    return {"games": row["games"], "wins": row["wins"],
            "winrate": 100 * row["wins"] / row["games"],
            "kda": (row["k"] + row["a"]) / max(row["d"], 1),
            "kills": row["k"], "deaths": row["d"], "assists": row["a"],
            "top_champs": [dict(t) for t in top]}


def matchups(con: sqlite3.Connection, riot_id: str, min_games=3,
             queues=None, season=None) -> list[dict]:
    """
    @brief Winrate by lane opponent (same role on the enemy team), per champion pair.

    @param con      Open sqlite3 connection.
    @param riot_id  Player's current riot id.
    @param min_games Minimum number of encounters required to include a pairing.
    @param queues   Queue-id tuple to filter by, or None for all.
    @param season   Optional year filter.
    @return List of {my_champ, enemy, games, wins}, best winrate first.
    """
    qf = _filters(queues, season)
    return [dict(r) for r in con.execute(
        "SELECT me.champion my_champ, op.champion enemy,"
        " COUNT(*) games, SUM(me.win) wins"
        " FROM match_participants me"
        " JOIN matches m ON m.match_id = me.match_id"
        " JOIN match_participants op ON op.match_id = me.match_id"
        "  AND op.team_id != me.team_id AND op.role = me.role AND me.role != ''"
        f" WHERE me.puuid = ?{qf}"
        " GROUP BY me.champion, op.champion HAVING COUNT(*) >= ?"
        " ORDER BY 1.0 * SUM(me.win) / COUNT(*) DESC, COUNT(*) DESC",
        (_puuid(con, riot_id), min_games),
    )]


def champ_matchups(con: sqlite3.Connection, riot_id: str, champion: str,
                   min_games=2, queues=None, season=None) -> list[dict]:
    """
    @brief Winrate for one champion against each lane opponent it has faced.

    @param con       Open sqlite3 connection.
    @param riot_id   Player's current riot id.
    @param champion  Champion to compute matchups for.
    @param min_games Minimum number of encounters required to include an opponent.
    @param queues    Queue-id tuple to filter by, or None for all.
    @param season    Optional year filter.
    @return List of {enemy, games, wins}, best winrate first.
    """
    qf = _filters(queues, season)
    return [dict(r) for r in con.execute(
        "SELECT op.champion enemy, COUNT(*) games, SUM(me.win) wins"
        " FROM match_participants me"
        " JOIN matches m ON m.match_id = me.match_id"
        " JOIN match_participants op ON op.match_id = me.match_id"
        "  AND op.team_id != me.team_id AND op.role = me.role AND me.role != ''"
        f" WHERE me.puuid = ? AND me.champion = ?{qf}"
        " GROUP BY op.champion HAVING COUNT(*) >= ?"
        " ORDER BY 1.0 * SUM(me.win) / COUNT(*) DESC, COUNT(*) DESC",
        (_puuid(con, riot_id), champion, min_games),
    )]


def top_rune_pages(con: sqlite3.Connection, riot_id: str, champion: str,
                   queues=None, season=None, limit=3) -> list[dict]:
    """
    @brief Most-played full rune pages for a champion.

    @param con      Open sqlite3 connection.
    @param riot_id  Player's current riot id.
    @param champion Champion to look up.
    @param queues   Queue-id tuple to filter by, or None for all.
    @param season   Optional year filter.
    @param limit    Max distinct rune pages to return.
    @return List of {perks, primary_style, sub_style, keystone, games, wins},
            most-played first.
    """
    qf = _filters(queues, season)
    return [dict(r) for r in con.execute(
        "SELECT p.perks, p.primary_style, p.sub_style, p.keystone,"
        " COUNT(*) games, SUM(p.win) wins"
        " FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ? AND p.champion = ? AND p.perks IS NOT NULL{qf}"
        " GROUP BY p.perks ORDER BY COUNT(*) DESC LIMIT ?",
        (_puuid(con, riot_id), champion, limit),
    )]


def top_final_items(con: sqlite3.Connection, riot_id: str, champion: str,
                    queues=None, season=None, limit=10) -> list[dict]:
    """
    @brief Most common items in the final build for a champion.

    @param con      Open sqlite3 connection.
    @param riot_id  Player's current riot id.
    @param champion Champion to look up.
    @param queues   Queue-id tuple to filter by, or None for all.
    @param season   Optional year filter.
    @param limit    Max distinct items to return.
    @return Tuple of (list of {item, games, wins, pct}, total games considered),
            items sorted by presence desc.
    """
    qf = _filters(queues, season)
    rows = con.execute(
        "SELECT p.items, p.win FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ? AND p.champion = ?{qf}",
        (_puuid(con, riot_id), champion)).fetchall()
    counts: dict[str, list] = {}
    for r in rows:
        for it in set((r["items"] or "").split()):
            c = counts.setdefault(it, [0, 0])
            c[0] += 1
            c[1] += r["win"]
    total = max(len(rows), 1)
    out = [{"item": it, "games": c[0], "wins": c[1], "pct": 100 * c[0] / total}
           for it, c in counts.items()]
    return sorted(out, key=lambda x: -x["games"])[:limit], len(rows)


def item_orders(con: sqlite3.Connection, riot_id: str, champion: str,
                completed: set[int], queues=None, season=None, limit=3):
    """
    @brief Most common order of the first 3 completed items (from timeline purchases).

    @param con      Open sqlite3 connection.
    @param riot_id  Player's current riot id.
    @param champion Champion to look up.
    @param completed Set of item IDs considered "completed" (vs. components).
    @param queues   Queue-id tuple to filter by, or None for all.
    @param season   Optional year filter.
    @param limit    Max distinct orders to return.
    @return Tuple of (list of {items, games}, total matches with a completed
            sequence considered), orders sorted by frequency desc.
    """
    qf = _filters(queues, season)
    rows = con.execute(
        "SELECT e.match_id, e.item_id, e.ts"
        " FROM item_events e"
        " JOIN match_participants p ON p.match_id = e.match_id AND p.puuid = e.puuid"
        " JOIN matches m ON m.match_id = e.match_id"
        f" WHERE p.puuid = ? AND p.champion = ?{qf}"
        " ORDER BY e.match_id, e.ts",
        (_puuid(con, riot_id), champion)).fetchall()
    per_match: dict[str, list[int]] = {}
    for r in rows:
        seq = per_match.setdefault(r["match_id"], [])
        if r["item_id"] in completed and r["item_id"] not in seq:
            seq.append(r["item_id"])
    counts: dict[tuple, int] = {}
    for seq in per_match.values():
        if len(seq) >= 2:
            key = tuple(seq[:3])
            counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda x: -x[1])[:limit]
    return [{"items": list(k), "games": v} for k, v in top], len(per_match)


def recent_games(con: sqlite3.Connection, riot_id: str, limit=20,
                 queues=None, season=None, offset=0,
                 champion: str | None = None) -> list[dict]:
    """
    @brief Most recent games for a player, newest first.

    @param con      Open sqlite3 connection.
    @param riot_id  Player's current riot id.
    @param limit    Max rows to return.
    @param queues   Queue-id tuple to filter by, or None for all.
    @param season   Optional year filter.
    @param offset   Row offset, for pagination.
    @param champion Optional exact champion name filter, applied server-side
           across the whole history (not just the current page).
    @return List of per-game dicts ordered by game creation desc.
    """
    qf = _filters(queues, season)
    params = [_puuid(con, riot_id)]
    if champion:
        qf += " AND p.champion = ?"
        params.append(champion)
    params += [limit, offset]
    return [dict(r) for r in con.execute(
        "SELECT p.match_id, p.champion, p.kills, p.deaths, p.assists, p.win, p.cs,"
        " p.items, p.keystone, m.queue_id, m.game_creation, m.duration"
        " FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ?{qf} ORDER BY m.game_creation DESC LIMIT ? OFFSET ?",
        tuple(params),
    )]


def match_rosters(con: sqlite3.Connection, match_ids: list[str]) -> dict[str, list[dict]]:
    """
    @brief Look up all 10 participants for each of a set of matches.

    @param con       Open sqlite3 connection.
    @param match_ids Match IDs to look up.
    @return Dict of match_id -> list of {riot_id, champion, team_id, puuid}.
    """
    if not match_ids:
        return {}
    ph = ",".join("?" * len(match_ids))
    out: dict[str, list[dict]] = {}
    for r in con.execute(
            "SELECT match_id, riot_id, champion, team_id, puuid"
            f" FROM match_participants WHERE match_id IN ({ph})"
            " ORDER BY match_id, team_id", match_ids):
        out.setdefault(r["match_id"], []).append(dict(r))
    return out


def main(riot_id: str):
    """@brief CLI entry point: print a summary + records for one player."""
    con = db.connect(str(ROOT / "lol.db"))
    s = summary(con, riot_id)
    if not s:
        sys.exit(f"Žádné zápasy pro {riot_id} — pusť nejdřív sync.")
    print(f"=== {riot_id} — {s['games']} her, {s['winrate']:.1f}% WR, "
          f"KDA {s['kda']:.2f} ({s['kills']}/{s['deaths']}/{s['assists']}) ===")
    print("Top champy:", ", ".join(
        f"{t['champion']} ({t['games']} her, {100 * t['wins'] / t['games']:.0f}% WR)"
        for t in s["top_champs"]))
    print("\n--- Rekordy ---")
    for r in records(con, riot_id):
        val = fmt_duration(r["val"]) if r["label"] == "Nejdelší hra" else fmt_int(r["val"])
        print(f"{r['label']:26} {val:>8}  ({r['champion']}, {_when(r['game_creation'])},"
              f" {'WIN' if r['win'] else 'LOSS'}, {r['duration'] // 60} min)")
    con.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('Použití: python -m lol.stats "GameName#TAG"')
    main(sys.argv[1])
