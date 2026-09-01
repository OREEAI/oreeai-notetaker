import json
import logging
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from oreeai_nt.core.config import settings

logger = logging.getLogger(__name__)


class CacheService:
    def __init__(self, redis: Redis | None) -> None:
        self._redis = redis

    @property
    def redis(self) -> Redis | None:
        return self._redis

    def key(self, *parts: Any) -> str:
        return f"{settings.cache_prefix}:" + ":".join(str(p) for p in parts)

    async def get_json(self, key: str) -> Any | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
        except RedisError:
            logger.warning("cache get failed for key=%s", key, exc_info=True)
            return None
        if raw is None:
            return None
        return json.loads(raw)

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        if self._redis is None:
            return
        try:
            ttl = ttl or settings.cache_ttl_seconds
            await self._redis.set(key, json.dumps(value, default=str), ex=ttl)
        except RedisError:
            logger.warning("cache set failed for key=%s", key, exc_info=True)

    async def delete(self, *keys: str) -> None:
        if self._redis is None or not keys:
            return
        try:
            await self._redis.delete(*keys)
        except RedisError:
            logger.warning("cache delete failed for keys=%s", keys, exc_info=True)

    async def ping(self) -> bool:
        if self._redis is None:
            return False
        try:
            return bool(await self._redis.ping())
        except RedisError:
            return False

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
