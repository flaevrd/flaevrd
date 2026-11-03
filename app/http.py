"""Shared outbound HTTP: per-host rate limiting, response caching, retries, robots.txt.

All providers go through `Http`. Credentials are passed per request by the provider adapters and
are never part of the cache key or logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import settings
from .db import DB, now

# Minimum seconds between requests to the same host (documented provider limits, with margin).
HOST_INTERVALS = {
    "musicbrainz.org": 1.1,          # 1 req/s per IP, User-Agent required
    "api.discogs.com": 1.05,         # 60/min authenticated
    "api.genius.com": 0.35,          # undocumented; stay polite
    "www.wikidata.org": 0.25,
    "query.wikidata.org": 0.5,
    "en.wikipedia.org": 0.25,
    "ws.audioscrobbler.com": 0.25,   # Last.fm: 5 req/s averaged
    "api.spotify.com": 0.2,
    "www.googleapis.com": 0.1,
}
DEFAULT_INTERVAL = 1.0               # arbitrary websites (contact discovery)


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body[:500]


@dataclass
class Response:
    status: int
    text: str
    url: str
    from_cache: bool

    def json(self) -> Any:
        return json.loads(self.text)


class Http:
    def __init__(self, db: DB):
        self.db = db
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        )
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    async def close(self) -> None:
        await self.client.aclose()

    # ---------------------------------------------------------------------------------
    async def _throttle(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            interval = HOST_INTERVALS.get(host, DEFAULT_INTERVAL)
            wait = self._host_last.get(host, 0) + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    def _cache_key(self, url: str, params: dict | None) -> str:
        raw = url + "?" + json.dumps(params or {}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _cached(self, key: str, max_age_hours: float) -> Response | None:
        row = self.db.one("SELECT * FROM http_cache WHERE key=?", (key,))
        if not row:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        if datetime.now(timezone.utc) - fetched > timedelta(hours=max_age_hours):
            return None
        return Response(row["status"], row["body"], row["url"], True)

    async def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        cache_hours: float | None = None,
        accept: str | None = None,
    ) -> Response:
        """GET with throttling and caching. Raises HttpError for non-2xx (404 included)."""
        cache_hours = settings.cache_hours if cache_hours is None else cache_hours
        key = self._cache_key(url, params)
        if cache_hours > 0:
            hit = self._cached(key, cache_hours)
            if hit:
                if hit.status >= 400:
                    raise HttpError(hit.status, url, hit.text)
                return hit

        host = urlsplit(url).hostname or ""
        h = dict(headers or {})
        if accept:
            h["Accept"] = accept
        last_exc: Exception | None = None
        for attempt in range(3):
            await self._throttle(host)
            try:
                r = await self.client.get(url, params=params, headers=h)
            except httpx.HTTPError as e:  # network level: retry once, then give up
                last_exc = e
                if attempt >= 1 or isinstance(e, httpx.ProxyError):
                    break
                await asyncio.sleep(1.5)
                continue
            if r.status_code in (429, 502, 503, 504):
                retry_after = r.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else 2.0 * (attempt + 1)
                await asyncio.sleep(min(delay, 30))
                last_exc = HttpError(r.status_code, url, r.text)
                continue
            text = r.text
            if cache_hours > 0 and (r.status_code < 400 or r.status_code == 404):
                self.db.x(
                    "INSERT OR REPLACE INTO http_cache(key,url,status,body,fetched_at) VALUES(?,?,?,?,?)",
                    (key, str(r.url), r.status_code, text, now()),
                )
            if r.status_code >= 400:
                raise HttpError(r.status_code, url, text)
            return Response(r.status_code, text, str(r.url), False)
        if isinstance(last_exc, HttpError):
            raise last_exc
        raise HttpError(0, url, str(last_exc))

    async def post_form(self, url: str, data: dict, auth: tuple[str, str]) -> Any:
        host = urlsplit(url).hostname or ""
        await self._throttle(host)
        r = await self.client.post(url, data=data, auth=auth)
        if r.status_code >= 400:
            raise HttpError(r.status_code, url, r.text)
        return r.json()

    # ---------------------------------------------------------------------------------
    async def robots_allows(self, url: str) -> bool:
        """robots.txt check for non-API pages. Unreachable robots.txt => allowed (RFC 9309 §2.3.1.3
        treats 4xx as 'no restrictions'); 5xx/network failure => disallow to be safe."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                await self._throttle(parts.hostname or "")
                r = await self.client.get(origin + "/robots.txt", headers={"Accept": "text/plain"})
                if r.status_code >= 500:
                    self._robots[origin] = None
                elif r.status_code >= 400:
                    rp.parse([])
                    self._robots[origin] = rp
                else:
                    rp.parse(r.text.splitlines())
                    self._robots[origin] = rp
            except httpx.HTTPError:
                self._robots[origin] = None
        rp = self._robots[origin]
        if rp is None:
            return False
        return rp.can_fetch(settings.user_agent, url) and rp.can_fetch("*", url)
