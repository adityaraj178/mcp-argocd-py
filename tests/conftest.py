"""Shared fixtures: a real uvicorn listener on a free loopback port and a raw HTTP client.

Requests are issued over a raw socket rather than an HTTP library so a test can
set an arbitrary Host header, which is exactly what a DNS-rebinding victim's
browser would send.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field

import pytest
from starlette.types import ASGIApp

from argocd_mcp.server.transport import make_uvicorn_server

INBOUND_TOKEN = "inbound-secret"


def free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@dataclass
class RawResponse:
    status: int
    headers: dict[str, str]
    body: str
    status_line: str = ""


@dataclass
class Listener:
    port: int
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    async def request(
        self,
        method: str = "POST",
        path: str = "/mcp",
        headers: Mapping[str, str] | None = None,
        body: str | None = "{}",
        *,
        send_host: bool = True,
    ) -> RawResponse:
        """One HTTP/1.0 exchange with exactly the headers given (Host included only if supplied)."""
        headers = dict(headers or {})
        if send_host and not any(k.lower() == "host" for k in headers):
            headers["host"] = f"localhost:{self.port}"
        payload = body.encode() if body is not None else b""
        if payload and not any(k.lower() == "content-length" for k in headers):
            headers["content-length"] = str(len(payload))
        lines = [f"{method} {path} HTTP/1.0"] + [f"{k}: {v}" for k, v in headers.items()]
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + payload
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(raw)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=10)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        head, _, rest = data.partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        status = int(status_line.split(" ", 2)[1])
        parsed = {}
        for line in header_lines:
            name, _, value = line.partition(":")
            parsed[name.strip().lower()] = value.strip()
        return RawResponse(status, parsed, rest.decode("utf-8", "replace"), status_line)


@contextlib.asynccontextmanager
async def serve_app(app: ASGIApp, port: int) -> AsyncIterator[Listener]:
    server = make_uvicorn_server(app, "127.0.0.1", port)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("listener did not start")
    try:
        yield Listener(port)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@pytest.fixture
def start_app() -> Callable[
    [Callable[[int], ASGIApp]], contextlib.AbstractAsyncContextManager[Listener]
]:
    """Start an ASGI app built for a free port; yields a Listener that talks to it."""

    @contextlib.asynccontextmanager
    async def _start(build: Callable[[int], ASGIApp]) -> AsyncIterator[Listener]:
        port = free_port()
        async with serve_app(build(port), port) as listener:
            yield listener

    return _start
