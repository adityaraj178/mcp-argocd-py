"""The real transports, booted end to end.

The security tests pin what apply_listener_security does; these prove each
transport actually sits behind it, that /healthz is exempt, and that the MCP
handshake works over the wire in both HTTP modes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from argocd_mcp.server.transport import (
    HttpTransportOptions,
    TransportOptions,
    build_http_app,
    build_sse_app,
)
from tests.conftest import INBOUND_TOKEN, Listener

INITIALIZE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "transport-test", "version": "1.0.0"},
        },
    }
)


@pytest.fixture(autouse=True)
def _no_argocd_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the ArgoCD credentials absent, a request that gets past the security
    # layer stops at the credential check rather than reaching out to a cluster.
    monkeypatch.delenv("ARGOCD_API_TOKEN", raising=False)
    monkeypatch.delenv("ARGOCD_BASE_URL", raising=False)
    monkeypatch.delenv("ARGOCD_TOKEN_REGISTRY_PATH", raising=False)


def http_app(port: int, **extra: Any):
    return build_http_app(HttpTransportOptions(port=port, auth_token=INBOUND_TOKEN, **extra))[0]


def sse_app(port: int):
    return build_sse_app(TransportOptions(port=port, auth_token=INBOUND_TOKEN))[0]


def authorized(port: int) -> dict[str, str]:
    return {
        "host": f"localhost:{port}",
        "authorization": f"Bearer {INBOUND_TOKEN}",
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }


def _payloads(body: str) -> list[dict[str, Any]]:
    """Decode a JSON or SSE-framed MCP response body into its JSON-RPC messages."""
    if body.lstrip().startswith("{"):
        return [json.loads(body)]
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]


async def test_the_http_transport_requires_the_inbound_token_on_mcp(start_app) -> None:
    async with start_app(http_app) as listener:
        for method in ["POST", "GET", "DELETE"]:
            res = await listener.request(
                method=method,
                headers={"host": f"localhost:{listener.port}", "content-type": "application/json"},
                body=INITIALIZE if method == "POST" else None,
            )
            assert res.status == 401, f"{method} /mcp without a token is rejected"


async def test_the_http_transport_rejects_a_forged_host_and_a_cross_origin_request(
    start_app,
) -> None:
    async with start_app(http_app) as listener:
        forged = await listener.request(
            headers={**authorized(listener.port), "host": "evil.example"}, body=INITIALIZE
        )
        assert forged.status == 403
        assert "Invalid Host" in forged.body

        cross = await listener.request(
            headers={**authorized(listener.port), "origin": "https://evil.example"}, body=INITIALIZE
        )
        assert cross.status == 403
        assert "Invalid Origin" in cross.body


async def test_an_authorized_request_reaches_the_http_transport_itself(start_app) -> None:
    async with start_app(http_app) as listener:
        # Stopping at the ArgoCD credential check is how we know the request cleared
        # every listener protection and entered the transport's own handler.
        res = await listener.request(headers=authorized(listener.port), body=INITIALIZE)
        assert res.status == 400
        assert "x-argocd-api-token" in res.body


async def test_a_non_initialize_request_without_a_session_is_a_400_json_rpc_error(
    start_app,
) -> None:
    async with start_app(http_app) as listener:
        res = await listener.request(
            headers={**authorized(listener.port), "x-argocd-api-token": "t"},
            body=json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}),
        )
        assert res.status == 400
        error = json.loads(res.body)
        assert error["id"] == 7
        assert "Not an initialization request" in error["error"]["message"]

        stale = await listener.request(
            headers={
                **authorized(listener.port),
                "x-argocd-api-token": "t",
                "mcp-session-id": "gone",
            },
            body=json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/list"}),
        )
        assert stale.status == 400
        assert "Invalid or expired session ID: gone" in stale.body


async def _handshake(
    listener: Listener, headers: dict[str, str]
) -> tuple[str | None, dict[str, str]]:
    res = await listener.request(headers=headers, body=INITIALIZE)
    assert res.status == 200, res.body
    result = _payloads(res.body)[0]["result"]
    assert result["serverInfo"]["name"] == "argocd-mcp"
    session_id = res.headers.get("mcp-session-id")
    session = {**headers, **({"mcp-session-id": session_id} if session_id else {})}
    await listener.request(
        headers=session, body=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    )
    return session_id, session


async def test_a_stateful_session_completes_the_handshake_and_serves_tool_calls(start_app) -> None:
    async with start_app(http_app) as listener:
        credentials = {
            **authorized(listener.port),
            "x-argocd-base-url": "https://argocd.example.com",
            "x-argocd-api-token": "outbound-token",
        }
        session_id, session = await _handshake(listener, credentials)
        assert session_id, "a stateful session is assigned an Mcp-Session-Id"

        listed = await listener.request(
            headers=session, body=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        )
        tools = _payloads(listed.body)[0]["result"]["tools"]
        assert len(tools) == 16
        assert all("argocdBaseUrl" in tool["inputSchema"]["properties"] for tool in tools)

        # The default token is bound to the default base URL: an override to an
        # unregistered host fails before any request leaves the server.
        called = await listener.request(
            headers=session,
            body=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "list_applications",
                        "arguments": {"argocdBaseUrl": "https://evil.example.com"},
                    },
                }
            ),
        )
        result = _payloads(called.body)[0]["result"]
        assert result["isError"] is True
        assert "Missing required ArgoCD API token" in result["content"][0]["text"]

        ended = await listener.request(method="DELETE", headers=session, body=None)
        assert ended.status == 200

        after = await listener.request(
            headers=session, body=json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        )
        assert after.status == 400
        assert "Invalid or expired session ID" in after.body


async def test_stateless_mode_serves_every_request_without_a_session(start_app) -> None:
    def build(port: int):
        return http_app(port, stateless=True)

    async with start_app(build) as listener:
        credentials = {
            **authorized(listener.port),
            "x-argocd-base-url": "https://argocd.example.com",
            "x-argocd-api-token": "outbound-token",
        }
        session_id, _ = await _handshake(listener, credentials)
        assert session_id is None, "no Mcp-Session-Id is returned in stateless mode"

        # A bare tools/list with no prior initialize on this connection still works.
        listed = await listener.request(
            headers=credentials,
            body=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        )
        assert listed.status == 200
        assert len(_payloads(listed.body)[0]["result"]["tools"]) == 16

        # Credentials are required on every request.
        bare = await listener.request(
            headers=authorized(listener.port),
            body=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
        )
        assert bare.status == 400
        assert "x-argocd-api-token" in bare.body

        for method in ["GET", "DELETE"]:
            res = await listener.request(method=method, headers=credentials, body=None)
            assert res.status == 405
            assert "stateless" in res.body


async def test_the_sse_transport_requires_the_inbound_token_on_sse_and_messages(start_app) -> None:
    async with start_app(sse_app) as listener:
        sse = await listener.request(
            method="GET", path="/sse", headers={"host": f"localhost:{listener.port}"}, body=None
        )
        assert sse.status == 401

        messages = await listener.request(
            path="/messages?session_id=made-up",
            headers={"host": f"localhost:{listener.port}", "content-type": "application/json"},
            body="{}",
        )
        assert messages.status == 401


async def test_the_sse_transport_requires_argocd_credentials_before_allocating_a_server(
    start_app,
) -> None:
    async with start_app(sse_app) as listener:
        res = await listener.request(
            method="GET", path="/sse", headers=authorized(listener.port), body=None
        )
        assert res.status == 400
        assert "x-argocd-api-token" in res.body


async def test_healthz_stays_reachable_without_a_token_on_both_transports(start_app) -> None:
    for build in (http_app, sse_app):
        async with start_app(build) as listener:
            # A kubelet probe addresses the pod IP, so it fails Host validation and
            # carries no bearer token. The exemption is held only by routing order,
            # which is exactly why it needs a test.
            res = await listener.request(
                method="GET", path="/healthz", headers={"host": "10.1.2.3:8080"}, body=None
            )
            assert res.status == 200
            assert json.loads(res.body) == {"status": "ok"}


async def test_an_absent_or_empty_host_header_is_rejected_not_treated_as_an_allowed_name(
    start_app,
) -> None:
    async with start_app(http_app) as listener:
        # A valid token, so what is being measured is the Host check rather than the
        # credential in front of it. HTTP/1.0 so the request is legal without Host.
        authorization = {"authorization": f"Bearer {INBOUND_TOKEN}"}
        absent = await listener.request(
            method="GET", headers=authorization, body=None, send_host=False
        )
        assert absent.status == 403

        empty = await listener.request(
            method="GET", headers={"host": "", **authorization}, body=None
        )
        assert empty.status == 403
