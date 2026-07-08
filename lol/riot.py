"""Async klient pro Riot API s rate limiterem.

Platformy (eun1, euw1) pro spectator/league, regionální routing (europe)
pro account-v1 a match-v5. Limity platí per host, proto limiter per host.
"""

import asyncio
import os
import time
from collections import defaultdict, deque

import httpx

# Personal/development key: 20 req/1 s, 100 req/2 min (per region)
DEFAULT_LIMITS = ((20, 1.0), (100, 120.0))


class RateLimiter:
    def __init__(self, limits=DEFAULT_LIMITS):
        self.limits = limits
        self.calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = time.monotonic()
                longest = max(w for _, w in self.limits)
                while self.calls and now - self.calls[0] > longest:
                    self.calls.popleft()
                wait = 0.0
                for n, window in self.limits:
                    recent = [t for t in self.calls if now - t <= window]
                    if len(recent) >= n:
                        wait = max(wait, recent[-n] + window - now)
                if wait <= 0:
                    self.calls.append(now)
                    return
                await asyncio.sleep(wait)


class RiotClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ["RIOT_API_KEY"]
        self._http = httpx.AsyncClient(
            headers={"X-Riot-Token": self.api_key}, timeout=10
        )
        self._limiters: dict[str, RateLimiter] = defaultdict(RateLimiter)

    async def _get(self, host: str, path: str, **params):
        await self._limiters[host].acquire()
        r = await self._http.get(f"https://{host}.api.riotgames.com{path}", params=params)
        if r.status_code == 429:  # pojistka — limiter by měl 429 předejít
            await asyncio.sleep(float(r.headers.get("Retry-After", 1)))
            r = await self._http.get(f"https://{host}.api.riotgames.com{path}", params=params)
        r.raise_for_status()
        return r.json()

    async def get_account(self, game_name: str, tag: str, region: str = "europe"):
        """Riot ID -> účet s PUUID (account-v1)."""
        return await self._get(region, f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag}")

    async def get_match_ids(self, puuid: str, region: str = "europe",
                            count: int = 5, start: int = 0):
        return await self._get(region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                               count=count, start=start)

    async def get_match(self, match_id: str, region: str = "europe"):
        return await self._get(region, f"/lol/match/v5/matches/{match_id}")

    async def get_timeline(self, match_id: str, region: str = "europe"):
        return await self._get(region, f"/lol/match/v5/matches/{match_id}/timeline")

    async def get_live_game(self, puuid: str, platform: str):
        """spectator-v5; vrací None, když hráč zrovna nehraje (404)."""
        try:
            return await self._get(platform, f"/lol/spectator/v5/active-games/by-summoner/{puuid}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def get_league_entries(self, puuid: str, platform: str):
        """Ranky (league-v4) — solo/flex."""
        return await self._get(platform, f"/lol/league/v4/entries/by-puuid/{puuid}")

    async def aclose(self):
        await self._http.aclose()
