"""Jednorázový backfill item_events z match timelines. Idempotentní."""
import asyncio

from lol.verify import load_env

load_env()
from lol import db
from lol.riot import RiotClient


async def main():
    con = db.connect("lol.db")
    client = RiotClient()
    todo = [r[0] for r in con.execute(
        "SELECT match_id FROM matches WHERE match_id NOT IN"
        " (SELECT DISTINCT match_id FROM item_events)")]
    print(f"stahuji timelines: {len(todo)} zápasů", flush=True)
    for i, mid in enumerate(todo, 1):
        try:
            db.insert_item_events(con, mid, await client.get_timeline(mid))
        except Exception as e:
            print(f"{mid}: {e}", flush=True)
        if i % 100 == 0:
            print(f"{i}/{len(todo)}", flush=True)
    print("hotovo", flush=True)
    await client.aclose()

asyncio.run(main())
