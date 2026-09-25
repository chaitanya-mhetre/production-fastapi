"""Fixed-window rate limiting in Redis.

Each (subject, minute) pair is one counter: INCR + EXPIRE in a single pipeline round-trip.
Trade-off: a client can send up to 2x the limit across a window boundary (end of one minute +
start of the next). A sliding-window log or token bucket fixes that at the cost of more Redis
work per request; for per-tenant API limits the fixed window is the usual pragmatic choice.
"""

import time
from dataclasses import dataclass

from redis.asyncio import Redis

from slotwise.errors import RateLimited

WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class RateLimitState:
    limit: int
    remaining: int
    reset_in: int


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def hit(self, subject: str, *, limit: int, now: float | None = None) -> RateLimitState:
        now = time.time() if now is None else now
        window = int(now // WINDOW_SECONDS)
        key = f"rl:{subject}:{window}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, WINDOW_SECONDS * 2)
            count, _ = await pipe.execute()
        reset_in = WINDOW_SECONDS - int(now % WINDOW_SECONDS)
        if int(count) > limit:
            raise RateLimited(
                f"rate limit of {limit}/min exceeded", retry_after=reset_in, limit=limit
            )
        return RateLimitState(limit=limit, remaining=limit - int(count), reset_in=reset_in)
