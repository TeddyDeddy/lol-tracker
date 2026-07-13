"""
@brief Tracker: sync match history and poll for live games.

CLI:
    python -m lol.tracker sync    # one-shot sync of recent matches + ranks
    python -m lol.tracker fullsync # one-shot sync of the entire match history
    python -m lol.tracker watch   # loop: reports when a tracked player starts a game
"""

import asyncio
import pathlib
import sys
import tomllib

from lol import db
from lol.riot import RiotClient

ROOT = pathlib.Path(__file__).parent.parent


def load_config():
    """@brief Load and parse `config.toml` from the project root."""
    return tomllib.loads((ROOT / "config.toml").read_text())


async def resolve_players(con, client, cfg) -> list[dict]:
    """
    @brief Ensure every player from the config has a DB row with a PUUID.

    Looks up each configured player by `riot_id`; any not yet in the DB are
    resolved via the Riot API and inserted.

    @param con    Open sqlite3 connection.
    @param client RiotClient used to resolve missing PUUIDs.
    @param cfg    Parsed `config.toml` (must contain a `players` list).
    @return List of player row dicts, one per configured player.
    """
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
    """
    @brief Fetch a player's entire available match history, paginated.

    The Riot API retains roughly 2 years of history; this pages through it
    100 IDs at a time until a short page signals the end.

    @param client RiotClient to query.
    @param puuid  Player's PUUID.
    @return All match IDs found, oldest-page-last order as returned by the API.
    """
    ids, start = [], 0
    while True:
        batch = await client.get_match_ids(puuid, count=100, start=start)
        ids += batch
        if len(batch) < 100:
            return ids
        start += 100


async def sync_player(con, client, player, count=20, full=False, on_matches_done=None):
    """
    @brief Download a player's new matches and current rank.

    Runs in two phases to work within the 100-req/2-min rate limit: matches
    first (so stats are usable halfway through), then timelines (build
    orders) for whatever was newly stored. A failure on one match/timeline
    doesn't kill the rest of the sync — dedup on `match_id` lets the next
    sync pick up anything missed.

    @param con             Open sqlite3 connection.
    @param client          RiotClient to query.
    @param player          Player row dict (needs `puuid`, `platform`).
    @param count           Max recent matches to check when not doing a full sync.
    @param full            If True, sync the player's entire match history instead of `count`.
    @param on_matches_done Optional async callback `(n_new_matches)` fired after the
           match phase, before timelines are fetched.
    @return Number of new matches stored.
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
    """
    @brief Diff a live-game lookup against the DB's last-known state.

    Clears the `live_games` row when the player is no longer in a game,
    upserts it when a new game is detected, and is a no-op (returns None)
    when the same game was already reported.

    @param con   Open sqlite3 connection.
    @param puuid Player's PUUID.
    @param live  Result of `RiotClient.get_live_game`, or None if not in a game.
    @return Event dict (`puuid`, `game_id`, `champion_id`, `live`) only when
            the player has JUST started a new game not seen before; else None.
    """
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
    """
    @brief Infinite loop: polls live-game status for every configured player.

    @param con      Open sqlite3 connection.
    @param client   RiotClient to query.
    @param cfg      Parsed `config.toml` (reads `tracker.live_poll_seconds`, default 120).
    @param on_start Optional async callback `(player, event)` fired when a
           tracked player is detected starting a new game.
    """
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
    """
    @brief CLI entry point: dispatch `sync` / `fullsync` / `watch`.

    @param cmd One of "sync", "fullsync", "watch". Any other value exits
           with a usage message.
    """
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
