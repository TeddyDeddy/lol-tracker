"""Ověření Riot API klíče: python -m lol.verify "GameName#TAG" eun1"""

import asyncio
import pathlib
import sys

from lol.riot import RiotClient


def load_env():
    env = pathlib.Path(__file__).parent.parent / ".env"
    if env.exists():
        import os
        for line in env.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


async def main(riot_id: str, platform: str):
    game_name, _, tag = riot_id.partition("#")
    client = RiotClient()
    try:
        account = await client.get_account(game_name, tag)
        puuid = account["puuid"]
        print(f"✔ Účet: {account['gameName']}#{account['tagLine']}  PUUID: {puuid[:12]}…")

        for entry in await client.get_league_entries(puuid, platform):
            print(f"✔ Rank {entry['queueType']}: {entry['tier']} {entry['rank']} "
                  f"{entry['leaguePoints']} LP ({entry['wins']}W/{entry['losses']}L)")

        match_ids = await client.get_match_ids(puuid, count=3)
        print(f"✔ Poslední zápasy: {', '.join(match_ids) or 'žádné'}")
        if match_ids:
            match = await client.get_match(match_ids[0])
            me = next(p for p in match["info"]["participants"] if p["puuid"] == puuid)
            print(f"✔ Poslední hra: {me['championName']} "
                  f"{me['kills']}/{me['deaths']}/{me['assists']} "
                  f"{'WIN' if me['win'] else 'LOSS'} "
                  f"({match['info']['gameDuration'] // 60} min)")

        live = await client.get_live_game(puuid, platform)
        if live:
            me = next(p for p in live["participants"] if p["puuid"] == puuid)
            print(f"✔ PRÁVĚ HRAJE: champion id {me['championId']} (gameId {live['gameId']})")
        else:
            print("✔ Live game: teď nehraje (spectator-v5 vrátil 404 — to je správně)")
    finally:
        await client.aclose()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit('Použití: python -m lol.verify "GameName#TAG" <eun1|euw1>')
    load_env()
    asyncio.run(main(sys.argv[1], sys.argv[2]))
