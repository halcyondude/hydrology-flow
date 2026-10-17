from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from hydrology_flow.config import Settings
from hydrology_flow.http import (
    TokenBucket,
    build_transport,
    cache_key,
    create_client,
    is_retryable_status,
    parse_retry_after,
)

URL = "https://example.test/data"


class Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def client_for(settings: Settings, sleeper: Sleeper) -> httpx.AsyncClient:
    transport = build_transport(settings, sleep=sleeper, rng=random.Random(1))
    return httpx.AsyncClient(transport=transport)


def test_retryable_statuses() -> None:
    assert is_retryable_status(429)
    assert is_retryable_status(503)
    assert not is_retryable_status(501)
    assert not is_retryable_status(404)
    assert not is_retryable_status(200)


def test_parse_retry_after() -> None:
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    now = datetime(2030, 1, 1, tzinfo=UTC)
    when = (now + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(when, now=now) == 30.0


@respx.mock
async def test_retries_5xx_then_succeeds(settings: Settings) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": 1})]
    )
    sleeper = Sleeper()
    async with client_for(settings, sleeper) as client:
        resp = await client.get(URL)
    assert resp.json() == {"ok": 1}
    assert route.call_count == 3
    assert len(sleeper.delays) == 2


@respx.mock
async def test_honors_retry_after(settings: Settings) -> None:
    respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    sleeper = Sleeper()
    async with client_for(settings, sleeper) as client:
        resp = await client.get(URL)
    assert resp.status_code == 200
    assert sleeper.delays[0] >= 7.0


@respx.mock
async def test_retry_after_is_capped(settings: Settings) -> None:
    capped = settings.model_copy(update={"http_retry_after_max_s": 5.0})
    respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "9999"}), httpx.Response(200)]
    )
    sleeper = Sleeper()
    async with client_for(capped, sleeper) as client:
        await client.get(URL)
    assert sleeper.delays[0] <= 5.0


@respx.mock
async def test_retries_timeouts_then_gives_up(settings: Settings) -> None:
    limited = settings.model_copy(update={"http_max_retries": 2})
    route = respx.get(URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    sleeper = Sleeper()
    async with client_for(limited, sleeper) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get(URL)
    assert route.call_count == 3
    assert len(sleeper.delays) == 2


@respx.mock
async def test_exhausted_retries_return_last_response(settings: Settings) -> None:
    limited = settings.model_copy(update={"http_max_retries": 1})
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    async with client_for(limited, Sleeper()) as client:
        resp = await client.get(URL)
    assert resp.status_code == 500
    assert route.call_count == 2


@respx.mock
async def test_client_errors_are_not_retried(settings: Settings) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    sleeper = Sleeper()
    async with client_for(settings, sleeper) as client:
        resp = await client.get(URL)
    assert resp.status_code == 404
    assert route.call_count == 1
    assert sleeper.delays == []


@respx.mock
async def test_backoff_grows_exponentially_with_jitter(settings: Settings) -> None:
    limited = settings.model_copy(update={"http_max_retries": 4, "http_backoff_max_s": 1000.0})
    respx.get(URL).mock(return_value=httpx.Response(503))
    sleeper = Sleeper()
    async with client_for(limited, sleeper) as client:
        await client.get(URL)
    base = limited.http_backoff_base_s
    for attempt, delay in enumerate(sleeper.delays):
        assert 0.0 <= delay <= base * 2**attempt


@respx.mock
async def test_cache_hit_skips_network(settings: Settings) -> None:
    route = respx.get(URL).mock(
        return_value=httpx.Response(
            200, json={"v": 1}, headers={"content-type": "application/json"}
        )
    )
    async with client_for(settings, Sleeper()) as client:
        first = await client.get(URL)
        second = await client.get(URL)
    assert route.call_count == 1
    assert first.json() == second.json() == {"v": 1}
    assert second.headers["content-type"] == "application/json"
    assert second.extensions["hflow_cache"] == "hit"


@respx.mock
async def test_cache_persists_across_clients(settings: Settings) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="hello"))
    for _ in range(2):
        async with client_for(settings, Sleeper()) as client:
            assert (await client.get(URL)).text == "hello"
    assert route.call_count == 1


@respx.mock
async def test_cache_keyed_by_body(settings: Settings) -> None:
    route = respx.post(URL).mock(side_effect=lambda req: httpx.Response(200, content=req.content))
    async with client_for(settings, Sleeper()) as client:
        a = await client.post(URL, data={"q": "a"})
        b = await client.post(URL, data={"q": "b"})
        a2 = await client.post(URL, data={"q": "a"})
    assert route.call_count == 2
    assert a.content != b.content
    assert a.content == a2.content


@respx.mock
async def test_errors_are_not_cached(settings: Settings) -> None:
    limited = settings.model_copy(update={"http_max_retries": 0})
    route = respx.get(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    async with client_for(limited, Sleeper()) as client:
        assert (await client.get(URL)).status_code == 500
        assert (await client.get(URL)).status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_cache_can_be_disabled(settings: Settings) -> None:
    off = settings.model_copy(update={"http_cache_enabled": False})
    route = respx.get(URL).mock(return_value=httpx.Response(200))
    async with client_for(off, Sleeper()) as client:
        await client.get(URL)
        await client.get(URL)
    assert route.call_count == 2


@respx.mock
async def test_gzip_body_round_trips_through_cache(settings: Settings) -> None:
    import gzip

    payload = b"x" * 1000
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, content=gzip.compress(payload), headers={"content-encoding": "gzip"}
        )
    )
    async with client_for(settings, Sleeper()) as client:
        assert (await client.get(URL)).content == payload
        assert (await client.get(URL)).content == payload


def test_cache_key_distinguishes_method_url_body() -> None:
    a = cache_key(httpx.Request("GET", URL))
    assert a == cache_key(httpx.Request("GET", URL))
    assert a != cache_key(httpx.Request("POST", URL))
    assert a != cache_key(httpx.Request("GET", URL + "?x=1"))
    assert cache_key(httpx.Request("POST", URL, content=b"a")) != cache_key(
        httpx.Request("POST", URL, content=b"b")
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


async def test_token_bucket_throttles_after_burst() -> None:
    clock = FakeClock()
    bucket = TokenBucket(2.0, 2, clock=clock, sleep=clock.sleep)
    for _ in range(2):
        await bucket.acquire()
    assert clock.sleeps == []
    await bucket.acquire()
    assert clock.sleeps == [pytest.approx(0.5)]


@respx.mock
async def test_rate_limit_is_per_host(settings: Settings) -> None:
    slow = settings.model_copy(update={"http_rate_per_second": 1.0, "http_burst": 1})
    respx.get("https://a.test/x").mock(return_value=httpx.Response(200))
    respx.get("https://b.test/x").mock(return_value=httpx.Response(200))
    clock = FakeClock()
    transport = build_transport(
        slow.model_copy(update={"http_cache_enabled": False}), sleep=clock.sleep, clock=clock
    )
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://a.test/x")
        await client.get("https://b.test/x")  # different host: own bucket, no wait
        assert clock.sleeps == []
        await client.get("https://a.test/x")  # same host: must wait for a token
    assert clock.sleeps == [pytest.approx(1.0)]


@respx.mock
async def test_create_client_sets_user_agent(settings: Settings) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200))
    async with create_client(settings) as client:
        await client.get(URL)
    assert "hydrology-flow" in route.calls.last.request.headers["user-agent"]
