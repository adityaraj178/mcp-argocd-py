"""Thin async HTTP client for the ArgoCD REST API."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

SearchParams = Mapping[str, str | int | float | bool | None] | None

# The Node original disables TLS verification through NODE_TLS_REJECT_UNAUTHORIZED=0;
# this is the Python equivalent for self-signed or private-CA ArgoCD instances.
INSECURE_SKIP_VERIFY_ENV = "ARGOCD_INSECURE_SKIP_VERIFY"

_DEFAULT_TIMEOUT = httpx.Timeout(60.0)


def _tls_verify() -> bool:
    return os.environ.get(INSECURE_SKIP_VERIFY_ENV, "").strip().lower() not in ("1", "true", "yes")


def _stringify(value: str | int | float | bool | None) -> str:
    # Match JavaScript's `value?.toString() || ''` so ArgoCD sees identical query strings.
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass
class HttpResponse:
    status: int
    headers: httpx.Headers
    body: Any


class HttpClient:
    def __init__(self, base_url: str, api_token: str) -> None:
        self.base_url = base_url
        self.api_token = api_token
        self.headers: dict[str, str] = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        self._client: httpx.AsyncClient | None = None

    def _session(self) -> httpx.AsyncClient:
        # Created lazily so the client is bound to the event loop that first uses it.
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self.headers, timeout=_DEFAULT_TIMEOUT, verify=_tls_verify()
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def abs_url(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url
        return urljoin(self.base_url, url)

    @staticmethod
    def _query(params: SearchParams) -> dict[str, str] | None:
        if not params:
            return None
        return {key: _stringify(value) for key, value in params.items()}

    async def _request(
        self,
        method: str,
        url: str,
        params: SearchParams = None,
        body: Any = None,
    ) -> HttpResponse:
        response = await self._session().request(
            method,
            self.abs_url(url),
            params=self._query(params),
            content=json.dumps(body) if body is not None else None,
        )
        try:
            parsed = response.json()
        except ValueError as error:
            snippet = response.text[:512]
            raise ValueError(
                f"ArgoCD returned a non-JSON response (HTTP {response.status_code}) "
                f"from {method} {url}: {snippet}"
            ) from error
        return HttpResponse(status=response.status_code, headers=response.headers, body=parsed)

    async def get(self, url: str, params: SearchParams = None) -> HttpResponse:
        return await self._request("GET", url, params)

    async def post(self, url: str, params: SearchParams = None, body: Any = None) -> HttpResponse:
        return await self._request("POST", url, params, body)

    async def put(self, url: str, params: SearchParams = None, body: Any = None) -> HttpResponse:
        return await self._request("PUT", url, params, body)

    async def delete(self, url: str, params: SearchParams = None) -> HttpResponse:
        return await self._request("DELETE", url, params)

    async def get_stream(
        self,
        url: str,
        params: SearchParams = None,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """Read an NDJSON stream, handing each line's `result` field to `callback`."""
        async with self._session().stream(
            "GET", self.abs_url(url), params=self._query(params)
        ) as response:
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if callback is not None:
                    callback(chunk.get("result"))
