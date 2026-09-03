"""Shared SEC HTTP client.

The SEC requires a descriptive User-Agent that includes a contact email, and
asks that automated clients stay under 10 requests/second. This module gives
every request in the Actor one rate-limited, retrying client so the whole run
stays inside that budget no matter which pipeline is calling.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from apify import Actor

# SEC's published ceiling is 10 req/s. We run under it on purpose.
MAX_REQUESTS_PER_SECOND = 8.0
_MIN_INTERVAL = 1.0 / MAX_REQUESTS_PER_SECOND


class SecClient:
    def __init__(self, user_agent: str, timeout: float = 60.0) -> None:
        if not user_agent or '@' not in user_agent:
            raise ValueError(
                'SEC requires a User-Agent containing a contact email, '
                'e.g. "Patriot Holdings jer@patriotholdings.com"'
            )
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                'User-Agent': user_agent,
                'Accept-Encoding': 'gzip, deflate',
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = _MIN_INTERVAL - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        attempts: int = 4,
        allow_404: bool = False,
    ) -> httpx.Response | None:
        """GET with throttling and exponential backoff.

        Returns None when the resource is legitimately missing (404 on a
        weekend daily-index file, for example) and allow_404 is set.
        """
        delay = 2.0
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            await self._throttle()
            try:
                response = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:  # network-level failure
                last_error = exc
                Actor.log.warning('GET %s failed (%s), attempt %s/%s', url, exc, attempt, attempts)
            else:
                if response.status_code == 404 and allow_404:
                    return None
                if response.status_code == 429 or response.status_code >= 500:
                    Actor.log.warning(
                        'GET %s returned %s, backing off %.0fs (attempt %s/%s)',
                        url, response.status_code, delay, attempt, attempts,
                    )
                    last_error = httpx.HTTPStatusError(
                        f'{response.status_code}', request=response.request, response=response
                    )
                else:
                    response.raise_for_status()
                    return response
            if attempt < attempts:
                await asyncio.sleep(delay)
                delay *= 2
        if allow_404:
            Actor.log.warning('Giving up on %s: %s', url, last_error)
            return None
        raise RuntimeError(f'GET {url} failed after {attempts} attempts: {last_error}')

    async def get_json(self, url: str, *, params: dict | None = None, allow_404: bool = False):
        response = await self.get(url, params=params, allow_404=allow_404)
        return response.json() if response is not None else None

    async def get_text(self, url: str, *, allow_404: bool = False) -> str | None:
        response = await self.get(url, allow_404=allow_404)
        return response.text if response is not None else None

    async def get_bytes(self, url: str, *, allow_404: bool = False) -> bytes | None:
        response = await self.get(url, allow_404=allow_404)
        return response.content if response is not None else None
