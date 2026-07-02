"""Tests for response cache."""

import pytest
from qb2api.cache import ResponseCache, _make_key


class TestCache:
    def test_cache_hit(self):
        c = ResponseCache(max_size=10, ttl=60)
        body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
        c.set(body, {"choices": [{"message": {"content": "hello"}}]})

        hit = c.get(body)
        assert hit is not None
        assert hit["choices"][0]["message"]["content"] == "hello"

    def test_cache_miss(self):
        c = ResponseCache(max_size=10, ttl=60)
        assert c.get({"model": "x", "messages": []}) is None

    def test_cache_expiry(self):
        c = ResponseCache(max_size=10, ttl=0)  # TTL=0 means instant expiry
        body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
        c.set(body, {"data": 1})
        assert c.get(body) is None

    def test_cache_eviction(self):
        c = ResponseCache(max_size=3, ttl=60)
        for i in range(5):
            c.set({"model": str(i), "messages": []}, {"i": i})
        assert c.size == 3  # oldest two evicted

    def test_cache_disabled(self):
        c = ResponseCache(max_size=0, ttl=60)
        body = {"model": "x", "messages": []}
        c.set(body, {"data": 1})
        assert c.get(body) is None

    def test_key_deterministic(self):
        a = {"model": "x", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.5}
        b = {"temperature": 0.5, "messages": [{"role": "user", "content": "hi"}], "model": "x"}
        assert _make_key(a) == _make_key(b)

    def test_key_differs_on_content(self):
        a = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
        b = {"model": "x", "messages": [{"role": "user", "content": "bye"}]}
        assert _make_key(a) != _make_key(b)

    def test_clear(self):
        c = ResponseCache(max_size=10, ttl=60)
        c.set({"model": "x", "messages": []}, {"data": 1})
        c.clear()
        assert c.size == 0
