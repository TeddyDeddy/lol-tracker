"""Tracker: sync match history + polling live games.

CLI:
    python -m lol.tracker sync    # jednorázový sync historie + ranků
    python -m lol.tracker watch   # smyčka: hlásí, když někdo začne hrát
"""

import asyncio
import pathlib
import sys
import tomllib

from lol import db
from lol.riot import RiotClient

ROOT = pathlib.Path(__file__).parent.parent


def load_config():
    return tomllib.loads((ROOT / "config.toml").read_text())


async def resolve_players(con, client, cfg) -> list[dict]:
    """Zajistí, že každý hráč z configu má v DB řádek s PUUID."""
    players = []
    for p in cfg["players"]:
        row = con.execute(
            "SELECT * FROM players WHERE riot_id = ?", (p["riot_id"],)
        ).fetchone()
        if row is None:
            name, _, tag = p["riot_id"].partition("#")
            account = await client.get_account(name, tag)
            con.execute(
                "INSERT OR IGNORE INTO players (puuid, riot_id, platform) VALUES (?,?,?)",
                (account["puuid"], p["riot_id"], p["platform"]),
            )
            con.commit()
            row = con.execute(
                "SELECT * FROM players WHERE riot_id = ?", (p["riot_id"],)
            ).fetchone()
        players.append(dict(row))
    return players


async def all_match_ids(client, puuid: str) -> list[str]:
    """Celá dostupná historie (API drží ~2 roky), stránkování po 100."""
    ids, start = [], 0
    while True:
        batch = await client.get_match_ids(puuid, count=100, start=start)
        ids += batch
        if len(batch) < 100:
            return ids
        start += 100


async def sync_player(con, client, player, count=20, full=False, on_matches_done=None):
    """Stáhne nové zápasy hráče a aktuální rank. full=True: celá historie.

    Dvě fáze kvůli rate limitu (100 req/2 min): nejdřív zápasy (staty
    použitelné v polovině času), pak timelines (build ordery). Chyba u
    jednoho zápasu nezabije zbytek — dedup dovolí navázat dalším syncem.
    """
    if full:
        match_ids = await all_match_ids(client, player["puuid"])
    else:
        match_ids = await client.get_match_ids(player["puuid"], count=count)
    new = [m for m in match_ids if not con.execute(
        "SELECT 1 FROM matches WHERE match_id = ?", (m,)).fetchone()]
    stored = []
    for match_id in new:
        try:
            db.insert_match(con, await client.get_match(match_id))
            stored.append(match_id)
        except Exception as e:
            print(f"match {match_id}: {e}", flush=True)
    for entry in await client.get_league_entries(player["puuid"], player["platform"]):
        db.insert_rank_snapshot(con, player["puuid"], entry)
    if on_matches_done:
        await on_matches_done(len(stored))
    for match_id in stored:
        try:
            db.insert_item_events(con, match_id, await client.get_timeline(match_id))
        except Exception as e:
            print(f"timeline {match_id}: {e}", flush=True)
    return len(stored)


def live_game_event(con, puuid: str, live: dict | None) -> dict | None:
    """Porovná live stav s DB. Vrátí event dict, když hráč PRÁVĚ začal hrát."""
    row = con.execute("SELECT * FROM live_games WHERE puuid = ?", (puuid,)).fetchone()
    if live is None:
        if row:
            con.execute("DELETE FROM live_games WHERE puuid = ?", (puuid,))
            con.commit()
        return None
    if row and row["game_id"] == live["gameId"]:
        return None  # už ohlášeno
    me = next(p for p in live["participants"] if p["puuid"] == puuid)
    con.execute(
        "INSERT OR REPLACE INTO live_games (puuid, game_id, champion_id, notified_at)"
        " VALUES (?,?,?,CURRENT_TIMESTAMP)",
        (puuid, live["gameId"], me["championId"]),
    )
    con.commit()
    return {"puuid": puuid, "game_id": live["gameId"],
            "champion_id": me["championId"], "live": live}


async def watch(con, client, cfg, on_start=None):
    """Nekonečná smyčka: každých N sekund zkontroluje live games."""
    interval = cfg["tracker"].get("live_poll_seconds", 120)
    players = await resolve_players(con, client, cfg)
    print(f"Sleduji {len(players)} hráčů, interval {interval} s. Ctrl+C = konec.")
    while True:
        for player in players:
            live = await client.get_live_game(player["puuid"], player["platform"])
            event = live_game_event(con, player["puuid"], live)
            if event:
                msg = f"🎮 {player['riot_id']} začal hrát (champion id {event['champion_id']})"
                print(msg)
                if on_start:
                    await on_start(player, event)
        await asyncio.sleep(interval)


async def main(cmd: str):
    cfg = load_config()
    con = db.connect(str(ROOT / "lol.db"))
    client = RiotClient()
    try:
        if cmd in ("sync", "fullsync"):
            for player in await resolve_players(con, client, cfg):
                n = await sync_player(con, client, player,
                                      cfg["tracker"].get("match_sync_count", 20),
                                      full=(cmd == "fullsync"))
                print(f"✔ {player['riot_id']}: {n} nových zápasů", flush=True)
        elif cmd == "watch":
            await watch(con, client, cfg)
        else:
            sys.exit("Použití: python -m lol.tracker <sync|fullsync|watch>")
    finally:
        await client.aclose()
        con.close()


if __name__ == "__main__":
    from lol.verify import load_env
    load_env()
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
