"""stdio, SSE, and Streamable HTTP transports for the ArgoCD MCP server.

Each connection (or, in stateless mode, each request) gets its own `Server`
bound to the ArgoCD credentials resolved for it, exactly as the TypeScript
original does. The network transports are ASGI apps served by uvicorn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import anyio
import mcp_types as types
import uvicorn
from anyio.abc import TaskGroup, TaskStatus
from mcp.server.connection import Connection
from mcp.server.runner import serve_connection
from mcp.server.sse import SseServerTransport
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER, StreamableHTTPServerTransport
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.transport_context import TransportContext
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from argocd_mcp.log import adopt, logger
from argocd_mcp.server.security import (
    ListenerSecurity,
    ListenerSecurityOptions,
    apply_listener_security,
    resolve_listener_security,
)
from argocd_mcp.server.server import Server, create_server
from argocd_mcp.server.token_registry import TokenRegistry, token_registry_from_env

TransportOptions = ListenerSecurityOptions


@dataclass(frozen=True)
class HttpTransportOptions(ListenerSecurityOptions):
    stateless: bool = False


# --- stdio ---------------------------------------------------------------------


def _env_credentials() -> tuple[str, str]:
    return os.environ.get("ARGOCD_BASE_URL", ""), os.environ.get("ARGOCD_API_TOKEN", "")


async def run_stdio_transport() -> None:
    # Load the base-URL -> token registry once at startup from the JSON file at
    # ARGOCD_TOKEN_REGISTRY_PATH. Read-only after construction.
    token_registry = token_registry_from_env()
    base_url, api_token = _env_credentials()
    server = create_server(base_url, api_token, token_registry)
    logger.info("Connecting to stdio transport")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


def connect_stdio_transport() -> None:
    anyio.run(run_stdio_transport)


# --- shared HTTP plumbing -----------------------------------------------------


class _Asgi:
    """Wrap a coroutine function so Starlette routes it as a raw ASGI app."""

    def __init__(self, handler: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self._handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._handler(scope, receive, send)


# Liveness probe, mounted beside (not behind) the security middleware: a kubelet
# probe addresses the pod IP and carries no bearer token. Reports only that we
# are up.
async def _healthz(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


def _compose(
    guarded_routes: list[Route | Mount],
    security: ListenerSecurity,
    lifespan: Callable[[Starlette], contextlib.AbstractAsyncContextManager[None]] | None = None,
) -> Starlette:
    guarded: ASGIApp = Starlette(routes=guarded_routes)
    return Starlette(
        routes=[
            Route("/healthz", _healthz, methods=["GET"]),
            Mount("/", app=apply_listener_security(guarded, security)),
        ],
        lifespan=lifespan,
    )


# Resolve the session-level ArgoCD credentials from headers or env.
#
# The API token is only ever accepted here (x-argocd-api-token header or
# ARGOCD_API_TOKEN env var), never as a tool-call argument, so the secret stays
# in the transport layer and out of prompts/model context.
#
# The token is normally MANDATORY and the connection is rejected when it is
# missing. The exception is when a token registry (ARGOCD_TOKEN_REGISTRY_PATH)
# is configured: the per-call base URL can then resolve its token from the
# registry, so a tokenless connection is allowed.
#
# These are outbound credentials only. Inbound authorization is the listener's
# job (MCP_AUTH_TOKEN, see security.py).
def _resolve_credentials(request: Request, token_registry: TokenRegistry) -> tuple[str, str] | None:
    env_base_url, env_api_token = _env_credentials()
    base_url = request.headers.get("x-argocd-base-url") or env_base_url
    api_token = request.headers.get("x-argocd-api-token") or env_api_token
    if not api_token and token_registry.get_size() == 0:
        return None
    return base_url, api_token


_MISSING_CREDENTIALS = (
    "x-argocd-api-token must be provided in the request header (or the ARGOCD_API_TOKEN env "
    "var), or a token registry must be configured via ARGOCD_TOKEN_REGISTRY_PATH."
)


async def _buffer_body(receive: Receive) -> tuple[bytes, Receive]:
    """Read the request body, returning it and a `receive` that replays it."""
    chunks = bytearray()
    trailing: Message | None = None
    while True:
        message = await receive()
        if message["type"] != "http.request":
            trailing = message
            break
        chunks.extend(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = bytes(chunks)
    pending: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]
    if trailing is not None:
        pending.append(trailing)

    async def replay() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return body, replay


async def _watch_status(app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> int | None:
    """Run `app` for one request and report the HTTP status it answered with."""
    status: int | None = None

    async def watching(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        await send(message)

    await app(scope, receive, watching)
    return status


def _json_rpc_error(status: int, message: str, request_id: Any = None) -> Response:
    return JSONResponse(
        {"jsonrpc": "2.0", "error": {"code": -32000, "message": message}, "id": request_id},
        status_code=status,
    )


# --- Streamable HTTP ------------------------------------------------------------


class _HttpSessionManager:
    """Per-session Streamable HTTP transports, each driving its own `Server`."""

    def __init__(self, stateless: bool, token_registry: TokenRegistry) -> None:
        self.stateless = stateless
        self.token_registry = token_registry
        self._transports: dict[str, StreamableHTTPServerTransport] = {}
        self._task_group: TaskGroup | None = None

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        async with anyio.create_task_group() as tg:
            self._task_group = tg
            try:
                yield
            finally:
                tg.cancel_scope.cancel()
                self._task_group = None
                self._transports.clear()

    def _tg(self) -> TaskGroup:
        if self._task_group is None:
            raise RuntimeError("Session manager is not running; is the app lifespan enabled?")
        return self._task_group

    async def handle_post(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        if not self.stateless and session_id and session_id in self._transports:
            await self._transports[session_id].handle_request(scope, receive, send)
            return

        body, receive = await _buffer_body(receive)
        request = Request(scope, receive)
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            response = JSONResponse(
                {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None},
                status_code=400,
            )
            await response(scope, receive, send)
            return
        is_initialize = isinstance(payload, dict) and payload.get("method") == "initialize"

        if self.stateless or (not session_id and is_initialize):
            credentials = _resolve_credentials(request, self.token_registry)
            if credentials is None:
                await PlainTextResponse(_MISSING_CREDENTIALS, status_code=400)(scope, receive, send)
                return
            server = create_server(*credentials, self.token_registry)
            if self.stateless:
                await self._serve_stateless(server, scope, receive, send)
            else:
                await self._open_session(server, scope, receive, send)
            return

        message = (
            f"Invalid or expired session ID: {session_id}"
            if session_id
            else "Bad Request: Not an initialization request and no valid session ID provided."
        )
        request_id = payload.get("id") if isinstance(payload, dict) else None
        await _json_rpc_error(400, message, request_id)(scope, receive, send)

    async def _open_session(
        self, server: Server, scope: Scope, receive: Receive, send: Send
    ) -> None:
        transport = StreamableHTTPServerTransport(mcp_session_id=uuid4().hex)
        session_id = transport.mcp_session_id
        assert session_id is not None
        self._transports[session_id] = transport

        async def run_session(*, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
            async with transport.connect() as (read_stream, write_stream):
                task_status.started()
                try:
                    await server.run(read_stream, write_stream)
                except Exception:  # noqa: BLE001 - a crashed session must not take the listener down
                    logger.exception(f"Session {session_id} crashed")
                finally:
                    await self._discard(session_id, transport)

        established = False
        try:
            await self._tg().start(run_session)
            status = await _watch_status(transport.handle_request, scope, receive, send)
            established = status is not None and status < 400
        finally:
            if not established:
                await self._discard(session_id, transport)

    async def _serve_stateless(
        self, server: Server, scope: Scope, receive: Receive, send: Send
    ) -> None:
        transport = StreamableHTTPServerTransport(mcp_session_id=None)

        async def run_request(*, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
            async with transport.connect() as (read_stream, write_stream):
                task_status.started()
                dispatcher: JSONRPCDispatcher[TransportContext] = JSONRPCDispatcher(
                    read_stream,
                    write_stream,
                    inline_methods=frozenset({"initialize"}),
                    # Without a session a server-to-client request has nowhere to land.
                    transport_builder=lambda _md: TransportContext(
                        kind="streamable-http", can_send_request=False
                    ),
                )
                # Born-ready: a stateless request may arrive with no prior initialize.
                connection = Connection.from_envelope(types.DEFAULT_NEGOTIATED_VERSION, None, None)
                try:
                    async with server.mcp.lifespan(server.mcp) as lifespan_state:
                        await serve_connection(
                            server.mcp,
                            dispatcher,
                            connection=connection,
                            lifespan_state=lifespan_state,
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("Stateless request crashed")

        try:
            await self._tg().start(run_request)
            await transport.handle_request(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await transport.terminate()

    async def handle_session_request(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.stateless:
            await PlainTextResponse("Method Not Allowed in stateless mode", status_code=405)(
                scope, receive, send
            )
            return
        session_id = Request(scope, receive).headers.get(MCP_SESSION_ID_HEADER)
        transport = self._transports.get(session_id) if session_id else None
        if transport is None:
            await PlainTextResponse("Invalid or missing session ID", status_code=400)(
                scope, receive, send
            )
            return
        await transport.handle_request(scope, receive, send)

    async def handle_mcp(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] == "POST":
            await self.handle_post(scope, receive, send)
        else:
            await self.handle_session_request(scope, receive, send)

    async def _discard(self, session_id: str, transport: StreamableHTTPServerTransport) -> None:
        self._transports.pop(session_id, None)
        if not transport.is_terminated:
            with anyio.CancelScope(shield=True):
                await transport.terminate()


def build_http_app(options: HttpTransportOptions) -> tuple[Starlette, ListenerSecurity]:
    """Build the Streamable HTTP ASGI app; raises on a listener configuration it cannot honour."""
    security = resolve_listener_security(options)
    token_registry = token_registry_from_env()
    manager = _HttpSessionManager(options.stateless, token_registry)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = _compose(
        [Route("/mcp", _Asgi(manager.handle_mcp), methods=["POST", "GET", "DELETE"])],
        security,
        lifespan,
    )
    return app, security


def http_transport_label(stateless: bool) -> str:
    return f"Http Stream transport{' (stateless mode)' if stateless else ''}"


def connect_http_transport(options: HttpTransportOptions) -> None:
    app, security = build_http_app(options)
    serve(app, security.bind_address, options.port, http_transport_label(options.stateless))


# --- SSE ------------------------------------------------------------------------


def build_sse_app(options: TransportOptions) -> tuple[Starlette, ListenerSecurity]:
    security = resolve_listener_security(options)
    token_registry = token_registry_from_env()
    sse = SseServerTransport("/messages")

    async def handle_sse(scope: Scope, receive: Receive, send: Send) -> None:
        # Same credential requirement as the http transport. Without it the handler
        # would build a Server with empty credentials and hold it until the socket
        # closed, one per connection.
        credentials = _resolve_credentials(Request(scope, receive), token_registry)
        if credentials is None:
            await PlainTextResponse(_MISSING_CREDENTIALS, status_code=400)(scope, receive, send)
            return
        server = create_server(*credentials, token_registry)
        async with sse.connect_sse(scope, receive, send) as (read_stream, write_stream):
            await server.run(read_stream, write_stream)

    app = _compose(
        [
            Route("/sse", _Asgi(handle_sse), methods=["GET"]),
            Route("/messages", _Asgi(sse.handle_post_message), methods=["POST"]),
        ],
        security,
    )
    return app, security


def connect_sse_transport(options: TransportOptions) -> None:
    app, security = build_sse_app(options)
    serve(app, security.bind_address, options.port, "SSE transport")


# --- uvicorn ----------------------------------------------------------------------


def make_uvicorn_server(app: ASGIApp, bind_address: str, port: int) -> uvicorn.Server:
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        adopt(name)
    config = uvicorn.Config(
        app,
        host=bind_address,
        port=port,
        log_config=None,
        access_log=False,
        # Do not advertise the framework.
        server_header=False,
        date_header=False,
        lifespan="on",
    )
    return uvicorn.Server(config)


async def serve_async(app: ASGIApp, bind_address: str, port: int, label: str) -> None:
    """Bind first, so a failure is reported as such rather than as a silent exit."""
    server = make_uvicorn_server(app, bind_address, port)
    try:
        sock = server.config.bind_socket()
    except (OSError, SystemExit) as error:
        raise ValueError(f"{label} could not bind {bind_address}:{port}: {error}") from error
    bound_host, bound_port = sock.getsockname()[:2]
    logger.info(f"{label} listening on {bound_host}:{bound_port}")
    await server.serve(sockets=[sock])


def serve(app: ASGIApp, bind_address: str, port: int, label: str) -> None:
    asyncio.run(serve_async(app, bind_address, port, label))
