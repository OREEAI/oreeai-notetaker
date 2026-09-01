from oreeai_nt.core.cache import CacheService


async def test_cache_disabled_returns_none() -> None:
    cache = CacheService(None)
    assert await cache.get_json("k") is None
    assert await cache.ping() is False
    await cache.set_json("k", {"a": 1})
    await cache.delete("k")


async def test_cache_roundtrip(fake_cache) -> None:
    key = fake_cache.key("meeting", "123")
    await fake_cache.set_json(key, {"title": "Sync"})
    assert await fake_cache.get_json(key) == {"title": "Sync"}
    await fake_cache.delete(key)
    assert await fake_cache.get_json(key) is None
