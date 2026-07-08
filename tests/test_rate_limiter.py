import asyncio
import time

from lol.riot import RateLimiter


def test_limiter_blocks_after_burst():
    async def run():
        limiter = RateLimiter(limits=((3, 0.3),))
        start = time.monotonic()
        for _ in range(4):
            await limiter.acquire()
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    # první 3 projdou hned, čtvrtý musí počkat na okno
    assert elapsed >= 0.25


def test_limiter_no_wait_under_limit():
    async def run():
        limiter = RateLimiter(limits=((10, 1.0),))
        start = time.monotonic()
        for _ in range(5):
            await limiter.acquire()
        return time.monotonic() - start

    assert asyncio.run(run()) < 0.1
