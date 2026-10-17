"""Shared ``httpx.AsyncClient`` factory with a middleware stack built from transports.

Stack, outermost first::

    InstrumentedTransport   structured log line + one OTel span per logical request
      CachingTransport      on-disk, content-hashed response cache (hits skip everything below)
        RetryTransport      exponential backoff + jitter on 429/5xx/transport errors; Retry-After
          RateLimitTransport  per-host token bucket + global concurrency cap
            network transport (httpx.AsyncHTTPTransport, or a test double)

``requests`` is never used anywhere in this project.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from opentelemetry.trace import SpanKind, Status, StatusCode

from hydrology_flow.config import Settings, get_settings
from hydrology_flow.telemetry import get_logger, get_meter, get_tracer

log = get_logger("http")

CACHE_EXT_KEY = "hflow_cache"
ATTEMPTS_EXT_KEY = "hflow_attempts"
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR_MIN = 500
HTTP_SERVER_ERROR_MAX = 599
HTTP_NOT_IMPLEMENTED = 501
HTTP_OK = 200
CACHE_KEY_SEPARATOR = b"\x00"
USER_AGENT = "hydrology-flow/0.1 (+https://github.com/halcyondude/hydrology-flow)"
# Headers that describe the stored (already decoded) body and must not be replayed verbatim.
_DROPPED_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


def is_retryable_status(status: int) -> bool:
    return status == HTTP_TOO_MANY_REQUESTS or (
        HTTP_SERVER_ERROR_MIN <= status <= HTTP_SERVER_ERROR_MAX and status != HTTP_NOT_IMPLEMENTED
    )


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


class TokenBucket:
    """Classic token bucket. ``acquire`` sleeps until a token is available."""

    def __init__(
        self,
        rate_per_second: float,
        burst: int,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._rate = rate_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self._rate)


class RateLimitTransport(httpx.AsyncBaseTransport):
    """Per-host token buckets and a global in-flight cap."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        rate_per_second: float,
        burst: int,
        concurrency: int,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._rate = rate_per_second
        self._burst = burst
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self._semaphore = asyncio.Semaphore(concurrency)

    def bucket(self, host: str) -> TokenBucket:
        if host not in self._buckets:
            self._buckets[host] = TokenBucket(
                self._rate, self._burst, clock=self._clock, sleep=self._sleep
            )
        return self._buckets[host]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self.bucket(request.url.host).acquire()
        async with self._semaphore:
            response = await self._inner.handle_async_request(request)
            await response.aread()
            return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class RetryTransport(httpx.AsyncBaseTransport):
    """Retry 429/5xx and transport errors with exponential backoff and full jitter.

    ``Retry-After`` (when present) is honoured as a floor, capped at ``retry_after_max_s``.
    Requests with in-memory bodies are replayable, so POST queries (idempotent ArcGIS
    ``query`` calls) are retried too.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        max_retries: int,
        backoff_base_s: float,
        backoff_max_s: float,
        retry_after_max_s: float,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._inner = inner
        self._max_retries = max_retries
        self._base = backoff_base_s
        self._max = backoff_max_s
        self._retry_after_max = retry_after_max_s
        self._sleep = sleep
        self._rng = rng or random.Random()

    def backoff(self, attempt: int) -> float:
        ceiling = min(self._max, self._base * (2**attempt))
        return self._rng.uniform(0.0, ceiling)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = await self._inner.handle_async_request(request)
                await response.aread()
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise
                delay = self.backoff(attempt)
                reason = type(exc).__name__
            else:
                if not is_retryable_status(response.status_code) or attempt >= self._max_retries:
                    response.extensions[ATTEMPTS_EXT_KEY] = attempt + 1
                    return response
                delay = self.backoff(attempt)
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                if retry_after is not None:
                    delay = max(delay, min(retry_after, self._retry_after_max))
                reason = str(response.status_code)
                await response.aclose()
            log.warning(
                "http retry",
                extra={
                    "http.url": str(request.url),
                    "http.retry.attempt": attempt + 1,
                    "http.retry.delay_s": round(delay, 3),
                    "http.retry.reason": reason,
                },
            )
            await self._sleep(delay)
            attempt += 1

    async def aclose(self) -> None:
        await self._inner.aclose()


def cache_key(request: httpx.Request) -> str:
    digest = hashlib.sha256()
    for part in (request.method.encode(), str(request.url).encode(), request.content):
        digest.update(part)
        digest.update(CACHE_KEY_SEPARATOR)
    return digest.hexdigest()


class CachingTransport(httpx.AsyncBaseTransport):
    """On-disk response cache keyed by sha256(method, url, body). Only 200s are stored."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, cache_dir: Path, *, enabled: bool = True
    ) -> None:
        self._inner = inner
        self._dir = cache_dir
        self._enabled = enabled

    def _paths(self, key: str) -> tuple[Path, Path]:
        base = self._dir / key[:2]
        return base / f"{key}.json", base / f"{key}.body"

    def _read(self, key: str) -> tuple[int, list[tuple[str, str]], bytes] | None:
        meta_path, body_path = self._paths(key)
        try:
            meta = json.loads(meta_path.read_text())
            return int(meta["status"]), [tuple(h) for h in meta["headers"]], body_path.read_bytes()
        except (OSError, ValueError, KeyError):
            return None

    def _write(self, key: str, response: httpx.Response) -> None:
        meta_path, body_path = self._paths(key)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        headers = [
            [k, v] for k, v in response.headers.multi_items() if k.lower() not in _DROPPED_HEADERS
        ]
        # Write body first and the meta file last (via rename) so a reader never sees a torn entry.
        tmp_body = body_path.with_suffix(".body.tmp")
        tmp_meta = meta_path.with_suffix(".json.tmp")
        tmp_body.write_bytes(response.content)
        tmp_meta.write_text(json.dumps({"status": response.status_code, "headers": headers}))
        tmp_body.replace(body_path)
        tmp_meta.replace(meta_path)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._enabled:
            return await self._inner.handle_async_request(request)
        key = cache_key(request)
        hit = await asyncio.to_thread(self._read, key)
        if hit is not None:
            status, headers, body = hit
            return httpx.Response(
                status,
                headers=headers,
                content=body,
                request=request,
                extensions={CACHE_EXT_KEY: "hit"},
            )
        response = await self._inner.handle_async_request(request)
        await response.aread()
        response.extensions[CACHE_EXT_KEY] = "miss"
        if response.status_code == HTTP_OK:
            await asyncio.to_thread(self._write, key, response)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class InstrumentedTransport(httpx.AsyncBaseTransport):
    """One OTel span, one structured log line and request metrics per logical request."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        meter = get_meter()
        self._requests = meter.create_counter("hflow.http.requests", description="HTTP requests")
        self._duration = meter.create_histogram(
            "hflow.http.duration", unit="s", description="HTTP request duration"
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        started = time.perf_counter()
        with get_tracer().start_as_current_span(
            f"HTTP {request.method}", kind=SpanKind.CLIENT
        ) as span:
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("server.address", host)
            span.set_attribute("url.full", str(request.url.copy_with(query=None)))
            try:
                response = await self._inner.handle_async_request(request)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                log.error(
                    "http error",
                    extra={
                        "http.method": request.method,
                        "server.address": host,
                        "error": repr(exc),
                    },
                )
                raise
            elapsed = time.perf_counter() - started
            cache = str(response.extensions.get(CACHE_EXT_KEY, "off"))
            attempts = int(response.extensions.get(ATTEMPTS_EXT_KEY, 1))
            span.set_attribute("http.response.status_code", response.status_code)
            span.set_attribute("hflow.cache", cache)
            span.set_attribute("hflow.attempts", attempts)
            if (
                response.status_code >= HTTP_SERVER_ERROR_MIN
                or response.status_code == HTTP_TOO_MANY_REQUESTS
            ):
                span.set_status(Status(StatusCode.ERROR))
            labels = {"server.address": host, "hflow.cache": cache}
            self._requests.add(1, labels)
            self._duration.record(elapsed, labels)
            log.info(
                "http %s %s -> %s",
                request.method,
                host,
                response.status_code,
                extra={
                    "http.method": request.method,
                    "server.address": host,
                    "http.status": response.status_code,
                    "hflow.cache": cache,
                    "hflow.attempts": attempts,
                    "duration_s": round(elapsed, 3),
                },
            )
            return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_transport(
    settings: Settings | None = None,
    *,
    inner: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
    rng: random.Random | None = None,
) -> httpx.AsyncBaseTransport:
    """Compose the middleware stack around ``inner`` (default: a real network transport)."""
    settings = settings or get_settings()
    network = inner or httpx.AsyncHTTPTransport(retries=0)
    limited = RateLimitTransport(
        network,
        rate_per_second=settings.http_rate_per_second,
        burst=settings.http_burst,
        concurrency=settings.http_concurrency,
        clock=clock,
        sleep=sleep,
    )
    retrying = RetryTransport(
        limited,
        max_retries=settings.http_max_retries,
        backoff_base_s=settings.http_backoff_base_s,
        backoff_max_s=settings.http_backoff_max_s,
        retry_after_max_s=settings.http_retry_after_max_s,
        sleep=sleep,
        rng=rng,
    )
    cached = CachingTransport(
        retrying, settings.http_cache_dir, enabled=settings.http_cache_enabled
    )
    return InstrumentedTransport(cached)


def create_client(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Build the project's shared async client. Always use this instead of ``httpx.AsyncClient()``."""
    settings = settings or get_settings()
    return httpx.AsyncClient(
        transport=build_transport(settings, inner=transport),
        timeout=settings.http_timeout_s,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
