"""Statistiky a rekordy hráče z lokální DB.

CLI: python -m lol.stats "GameName#TAG"
Funkce vrací dicty — použije je i Discord bot (fáze 2).
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
    "Normal": (400, 430, 490),
    "ARAM": (450,),
    "Arena": (1700, 1750),
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
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _puuid(con: sqlite3.Connection, riot_id: str) -> str | None:
    """riot_id -> puuid. Filtrovat vždy přes puuid — mp.riot_id je jméno
    v době zápasu a po přejmenování účtu by se starší hry nezapočítaly."""
    row = con.execute(
        "SELECT puuid FROM players WHERE riot_id = ?", (riot_id,)).fetchone()
    return row["puuid"] if row else None


def _queue_filter(queues) -> str:
    if not queues:
        return ""
    return f" AND m.queue_id IN ({','.join(str(q) for q in queues)})"


def _filters(queues, season=None) -> str:
    """SQL fragment: filtr módu + sezóny (rok hry)."""
    f = _queue_filter(queues)
    if season:
        f += (" AND strftime('%Y', m.game_creation/1000, 'unixepoch')"
              f" = '{int(season)}'")
    return f


def seasons(con: sqlite3.Connection) -> list[str]:
    """Roky, ze kterých máme zápasy (nejnovější první)."""
    return [r[0] for r in con.execute(
        "SELECT DISTINCT strftime('%Y', game_creation/1000, 'unixepoch')"
        " FROM matches WHERE game_creation > 0 ORDER BY 1 DESC")]


def records(con: sqlite3.Connection, riot_id: str, queues=None, season=None) -> list[dict]:
    """
    @brief Find this player's single-game standout stats (best-of-N per RECORDS entry,
           plus longest game and worst KDA), each linking back to the source match.

    @param riot_id Player's current riot id.
    @param queues Queue-id tuple to filter by (see QUEUE_GROUPS), or None for all.
    @param season Optional year filter.
    @return List of record dicts: label, val, match_id, champion, game_creation,
            duration, win, kills, deaths, assists.
    """
    qf = _filters(queues, season)
    puuid = _puuid(con, riot_id)
    out = []
    for label, col, agg in RECORDS:
        row = con.execute(
            f"SELECT p.{col} AS val, p.match_id, p.champion, m.game_creation,"
            f" m.duration, p.win, p.kills, p.deaths, p.assists"
            f" FROM match_participants p JOIN matches m USING (match_id)"
            f" WHERE p.puuid = ?{qf} ORDER BY p.{col} DESC LIMIT 1",
            (puuid,),
        ).fetchone()
        if row and row["val"] is not None:
            out.append({"label": label, **dict(row)})
    row = con.execute(
        "SELECT m.duration AS val, p.match_id, p.champion, m.game_creation,"
        " m.duration, p.win, p.kills, p.deaths, p.assists"
        " FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ?{qf} ORDER BY m.duration DESC LIMIT 1",
        (puuid,),
    ).fetchone()
    if row:
        out.append({"label": "Nejdelší hra", **dict(row)})
    # nejhorší KDA, min. 5 smrtí ať to není náhoda z jedné hry s jednou smrtí
    row = con.execute(
        "SELECT p.match_id, p.champion, m.game_creation, m.duration, p.win,"
        " p.kills, p.deaths, p.assists,"
        " 1.0 * (p.kills + p.assists) / p.deaths AS val"
        " FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ? AND p.deaths >= 5{qf} ORDER BY val ASC LIMIT 1",
        (puuid,),
    ).fetchone()
    if row:
        out.append({"label": "Nejhorší KDA", **dict(row)})
    return out


def queue_counts(con: sqlite3.Connection, riot_id: str, season=None) -> list[dict]:
    """
    @brief Count games per queue-mode group (SoloQ/Flex/Normal/ARAM/Arena) for a player.

    @param riot_id Player's current riot id.
    @param season Optional year filter.
    @return List of {mode, games} in QUEUE_GROUPS order (excluding "All"), zero-count
            modes included so the caller can render a stable breakdown.
    """
    puuid = _puuid(con, riot_id)
    out = []
    for name, queues in QUEUE_GROUPS.items():
        if name == "All":
            continue
        qf = _filters(queues, season)
        n = con.execute(
            "SELECT COUNT(*) FROM match_participants p JOIN matches m USING (match_id)"
            f" WHERE p.puuid = ?{qf}", (puuid,)).fetchone()[0]
        out.append({"mode": name, "games": n})
    return out


def summary(con: sqlite3.Connection, riot_id: str, queues=None, season=None) -> dict | None:
    qf = _filters(queues, season)
    puuid = _puuid(con, riot_id)
    row = con.execute(
        "SELECT COUNT(*) games, SUM(win) wins, SUM(kills) k, SUM(deaths) d,"
        " SUM(assists) a FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ?{qf}",
        (puuid,),
    ).fetchone()
    if not row["games"]:
        return None
    top = con.execute(
        "SELECT champion, COUNT(*) games, SUM(win) wins"
        " FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ?{qf} GROUP BY champion ORDER BY games DESC LIMIT 5",
        (puuid,),
    ).fetchall()
    return {"games": row["games"], "wins": row["wins"],
            "winrate": 100 * row["wins"] / row["games"],
            "kda": (row["k"] + row["a"]) / max(row["d"], 1),
            "kills": row["k"], "deaths": row["d"], "assists": row["a"],
            "top_champs": [dict(t) for t in top]}


def matchups(con: sqlite3.Connection, riot_id: str, min_games=3,
             queues=None, season=None) -> list[dict]:
    """Winrate podle lane protivníka (stejná role v enemy týmu), min_games střetnutí."""
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
    """Winrate daného championa proti lane protivníkům."""
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
    """Nejčastější celé runové stránky na championovi."""
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
    """Nejčastější itemy ve finálním buildu (podíl her, winrate s itemem)."""
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
    """Nejčastější pořadí prvních 3 dokončených itemů (z timeline nákupů)."""
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
                 queues=None, season=None, offset=0) -> list[dict]:
    qf = _filters(queues, season)
    return [dict(r) for r in con.execute(
        "SELECT p.match_id, p.champion, p.kills, p.deaths, p.assists, p.win, p.cs,"
        " p.items, p.keystone, m.queue_id, m.game_creation, m.duration"
        " FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ?{qf} ORDER BY m.game_creation DESC LIMIT ? OFFSET ?",
        (_puuid(con, riot_id), limit, offset),
    )]


def match_rosters(con: sqlite3.Connection, match_ids: list[str]) -> dict[str, list[dict]]:
    """match_id -> všech 10 účastníků (riot_id, champion, team_id, puuid)."""
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
