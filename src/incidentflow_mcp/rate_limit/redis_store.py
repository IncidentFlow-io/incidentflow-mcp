"""Redis-backed primitives for token-bucket rate limiting and concurrency guards."""

from __future__ import annotations

import time
from dataclasses import dataclass

try:
    from redis.asyncio import Redis, from_url
except ImportError:  # pragma: no cover - exercised in dependency-missing envs
    Redis = None  # type: ignore[assignment,misc]
    from_url = None  # type: ignore[assignment,misc]


@dataclass(frozen=True)
class TokenBucketResult:
    allowed: bool
    limit: int
    remaining: int
    reset_after_ms: int


_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill_per_sec = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl_ms = tonumber(ARGV[5])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if not tokens then tokens = capacity end
if not ts then ts = now_ms end

local elapsed_ms = math.max(0, now_ms - ts)
local refill_tokens = (elapsed_ms / 1000.0) * refill_per_sec
tokens = math.min(capacity, tokens + refill_tokens)

local allowed = 0
if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
end

local deficit = math.max(0, cost - tokens)
local reset_after_ms = 0
if refill_per_sec > 0 then
  reset_after_ms = math.ceil((deficit / refill_per_sec) * 1000)
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', key, ttl_ms)

return {allowed, math.floor(tokens), reset_after_ms}
"""

_ACQUIRE_CONCURRENCY_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl_ms = tonumber(ARGV[2])

local current = tonumber(redis.call('GET', key) or '0')
if current >= limit then
  return {0, current}
end

local next = redis.call('INCR', key)
redis.call('PEXPIRE', key, ttl_ms)
return {1, next}
"""

_RELEASE_CONCURRENCY_LUA = """
local key = KEYS[1]
local current = tonumber(redis.call('GET', key) or '0')
if current <= 1 then
  redis.call('DEL', key)
  return 0
end
return redis.call('DECR', key)
"""


class RedisRateLimitStore:
    """
    Distributed store for rate-limit and concurrency state.

    Token bucket is implemented in Lua for atomicity across replicas/processes.
    This prevents races that would occur with read-modify-write logic in Python.
    """

    def __init__(self, redis_url: str, key_prefix: str = "incidentflow:mcp") -> None:
        if from_url is None:
            raise RuntimeError(
                "redis package is required for distributed rate limiting. "
                "Install dependency: redis>=5.2.1"
            )
        self._client = from_url(redis_url, encoding="utf-8", decode_responses=True)
        self._prefix = key_prefix.rstrip(":")

    async def close(self) -> None:
        await self._client.aclose()

    async def take_token(
        self,
        *,
        scope: str,
        identity_key: str,
        limit_per_min: int,
        cost: int = 1,
    ) -> TokenBucketResult:
        now_ms = int(time.time() * 1000)
        ttl_ms = 120_000
        refill_per_sec = limit_per_min / 60.0

        key = self._key("bucket", scope, identity_key)
        allowed, remaining, reset_after_ms = await self._client.eval(  # type: ignore[assignment]
            _TOKEN_BUCKET_LUA,
            1,
            key,
            now_ms,
            limit_per_min,
            refill_per_sec,
            cost,
            ttl_ms,
        )

        return TokenBucketResult(
            allowed=bool(int(allowed)),
            limit=limit_per_min,
            remaining=max(0, int(remaining)),
            reset_after_ms=max(0, int(reset_after_ms)),
        )

    async def acquire_concurrency(
        self,
        *,
        scope: str,
        identity_key: str,
        limit: int,
        ttl_ms: int,
    ) -> bool:
        key = self._key("concurrency", scope, identity_key)
        acquired, _ = await self._client.eval(  # type: ignore[assignment]
            _ACQUIRE_CONCURRENCY_LUA,
            1,
            key,
            limit,
            ttl_ms,
        )
        return bool(int(acquired))

    async def release_concurrency(self, *, scope: str, identity_key: str) -> None:
        key = self._key("concurrency", scope, identity_key)
        await self._client.eval(_RELEASE_CONCURRENCY_LUA, 1, key)

    def _key(self, kind: str, scope: str, identity_key: str) -> str:
        return f"{self._prefix}:{kind}:{scope}:{identity_key}"
