"""
@brief Async Riot API client with a per-host rate limiter.

Platform hosts (eun1, euw1, …) serve spectator-v5/league-v4; the regional
host (europe, americas, …) serves account-v1/match-v5. Rate limits are
enforced per host by Riot, so `RiotClient` keeps one `RateLimiter` per host
it has talked to.
"""

import asyncio
import os
import time
from collections import defaultdict, deque

import httpx

# Personal/development key: 20 req/1 s, 100 req/2 min (per region)
DEFAULT_LIMITS = ((20, 1.0), (100, 120.0))


class RateLimiter:
    """
    @brief Sliding-window rate limiter shared by all calls to one API host.

    @param limits Sequence of (max_calls, window_seconds) pairs that must
           all hold simultaneously, e.g. `((20, 1.0), (100, 120.0))` for
           "20 calls/sec AND 100 calls/2 min".
    """

    def __init__(self, limits=DEFAULT_LIMITS):
        self.limits = limits
        self.calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """
        @brief Block until a call is allowed under every configured window,
               then record it.
        """
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
    """
    @brief Thin async wrapper over the Riot Games REST API.

    Holds one shared `httpx.AsyncClient` and one `RateLimiter` per host
    (lazily created via `defaultdict`), so every endpoint call goes through
    `_get()` and is automatically throttled and authenticated.

    @param api_key Riot dev API key. Falls back to the `RIOT_API_KEY`
           environment variable if not given.
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ["RIOT_API_KEY"]
        self._http = httpx.AsyncClient(
            headers={"X-Riot-Token": self.api_key}, timeout=10
        )
        self._limiters: dict[str, RateLimiter] = defaultdict(RateLimiter)

    async def _get(self, host: str, path: str, **params):
        """
        @brief Rate-limited authenticated GET against one Riot API host.

        @param host   Host segment, e.g. "europe" or "eun1".
        @param path   API path starting with "/", e.g. "/lol/match/v5/matches/{id}".
        @param params Query-string parameters.
        @return Parsed JSON response body.
        @throws httpx.HTTPStatusError On any non-2xx response (including a
                second 429 after the Retry-After backoff — dev keys expire
                every 24h and surface as 401 here).
        """
        await self._limiters[host].acquire()
        r = await self._http.get(f"https://{host}.api.riotgames.com{path}", params=params)
        if r.status_code == 429:  # safety net — the limiter should normally prevent this
            await asyncio.sleep(float(r.headers.get("Retry-After", 1)))
            r = await self._http.get(f"https://{host}.api.riotgames.com{path}", params=params)
        r.raise_for_status()
        return r.json()

    async def get_account(self, game_name: str, tag: str, region: str = "europe"):
        """
        @brief Resolve a Riot ID to an account (account-v1).

        @param game_name Riot ID name part (before the "#").
        @param tag       Riot ID tag part (after the "#").
        @param region    Regional routing host.
        @return Account dict including `puuid`, `gameName`, `tagLine`.
        """
        return await self._get(region, f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag}")

    async def get_match_ids(self, puuid: str, region: str = "europe",
                            count: int = 5, start: int = 0):
        """
        @brief List recent match IDs for a player (match-v5).

        @param puuid  Player's PUUID.
        @param region Regional routing host.
        @param count  Max number of match IDs to return.
        @param start  Offset into the player's match history, newest first.
        @return List of match ID strings.
        """
        return await self._get(region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                               count=count, start=start)

    async def get_match(self, match_id: str, region: str = "europe"):
        """
        @brief Fetch full match data (match-v5).

        @param match_id Match ID as returned by `get_match_ids`.
        @param region   Regional routing host.
        @return Raw match JSON (stored verbatim in `matches.raw_json`).
        """
        return await self._get(region, f"/lol/match/v5/matches/{match_id}")

    async def get_timeline(self, match_id: str, region: str = "europe"):
        """
        @brief Fetch the frame-by-frame match timeline (match-v5).

        @param match_id Match ID as returned by `get_match_ids`.
        @param region   Regional routing host.
        @return Raw timeline JSON.
        """
        return await self._get(region, f"/lol/match/v5/matches/{match_id}/timeline")

    async def get_live_game(self, puuid: str, platform: str):
        """
        @brief Look up a player's in-progress game (spectator-v5).

        @param puuid    Player's PUUID.
        @param platform Platform routing host, e.g. "eun1".
        @return Live-game dict, or None if the player isn't currently in a game.
        """
        try:
            return await self._get(platform, f"/lol/spectator/v5/active-games/by-summoner/{puuid}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def get_league_entries(self, puuid: str, platform: str):
        """
        @brief Fetch ranked entries (league-v4) — solo queue and flex.

        @param puuid    Player's PUUID.
        @param platform Platform routing host.
        @return List of league-entry dicts (one per queue the player is ranked in).
        """
        return await self._get(platform, f"/lol/league/v4/entries/by-puuid/{puuid}")

    async def aclose(self):
        """@brief Close the underlying HTTP connection pool."""
        await self._http.aclose()
