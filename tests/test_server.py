"""The per-call token-resolution boundary in Server.resolve_client.

The security-critical invariant: the default (session) token is bound to the
default base URL ONLY and must never be sent to a caller-supplied base URL.
When no token can be resolved for the requested base URL, the tool returns an
error result *before* making any HTTP request, so these tests are hermetic.
"""

from __future__ import annotations

import json
import re

import pytest
from mcp.shared.exceptions import MCPError

from argocd_mcp.server.server import Server, create_server
from argocd_mcp.server.token_registry import TokenRegistry

DEFAULT_BASE_URL = "https://argocd.internal.example.com"
DEFAULT_TOKEN = "default-secret-token"
EVIL_BASE_URL = "https://evil.example.com"
# A legitimately registered second instance (distinct from the default), used to
# prove that the presence of an unrelated registry entry doesn't change how the
# default base URL resolves its token.
OTHER_BASE_URL = "https://argocd.other.example.com"

READ_TOOLS = {
    "list_applications",
    "list_clusters",
    "get_application",
    "get_appproject",
    "get_application_resource_tree",
    "get_application_managed_resources",
    "get_application_workload_logs",
    "get_application_events",
    "get_resource_events",
    "get_resources",
    "get_resource_actions",
}
WRITE_TOOLS = {
    "create_application",
    "update_application",
    "delete_application",
    "sync_application",
    "run_resource_action",
}


def text_of(result) -> str:
    return "\n".join(item.text for item in result.content)


async def test_overridden_base_url_with_no_registry_entry_does_not_receive_the_default_token() -> (
    None
):
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())

    result = await server.call_tool("list_applications", {"argocdBaseUrl": EVIL_BASE_URL})

    # The call must fail to resolve a token (so nothing is ever sent to the
    # attacker host) rather than silently pairing the default token with it.
    assert result.is_error is True
    assert "Missing required ArgoCD API token" in text_of(result)
    assert EVIL_BASE_URL in text_of(result)


def test_overridden_base_url_with_a_registry_entry_uses_the_registry_token_not_the_default() -> (
    None
):
    registry = TokenRegistry([{"baseUrl": EVIL_BASE_URL, "token": "registered-token"}])
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, registry)

    client = server.resolve_client(EVIL_BASE_URL)

    assert client.client.api_token == "registered-token"
    assert client.client.api_token != DEFAULT_TOKEN
    assert client.base_url == EVIL_BASE_URL


def test_default_base_url_uses_the_default_token() -> None:
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())
    client = server.resolve_client(DEFAULT_BASE_URL)
    assert client.client.api_token == DEFAULT_TOKEN
    # And it is the session client itself, not a fresh copy.
    assert client is server.argocd_client


def test_no_override_uses_the_session_client() -> None:
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())
    assert server.resolve_client(None) is server.argocd_client


def test_default_base_url_match_is_normalized_trailing_slash_and_case() -> None:
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())
    # Same host, different formatting: still the default URL, still the default token.
    client = server.resolve_client(f"{DEFAULT_BASE_URL.upper()}/")
    assert client.client.api_token == DEFAULT_TOKEN


def test_overriding_to_the_default_instance_reuses_the_session_token_with_a_registry_present() -> (
    None
):
    registry = TokenRegistry([{"baseUrl": OTHER_BASE_URL, "token": "registered-token"}])
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, registry)
    client = server.resolve_client(f"{DEFAULT_BASE_URL.upper()}/")
    assert client.client.api_token == DEFAULT_TOKEN


async def test_overriding_to_a_different_instance_with_no_registry_entry_sends_no_request() -> None:
    # Even though a valid default token exists for the default host, it must
    # never be sent to the other host.
    registry = TokenRegistry([{"baseUrl": DEFAULT_BASE_URL, "token": "registered-default"}])
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, registry)

    result = await server.call_tool("list_applications", {"argocdBaseUrl": EVIL_BASE_URL})

    assert result.is_error is True
    assert "Missing required ArgoCD API token" in text_of(result)
    assert EVIL_BASE_URL in text_of(result)


async def test_with_no_default_token_an_overridden_url_still_resolves_only_from_the_registry() -> (
    None
):
    # Tokenless session (allowed when a registry is configured). Even the default
    # URL has no token, and an unregistered override must fail rather than borrow.
    registry = TokenRegistry([{"baseUrl": DEFAULT_BASE_URL, "token": "registered-default"}])
    server = create_server("", "", registry)

    result = await server.call_tool("list_applications", {"argocdBaseUrl": EVIL_BASE_URL})
    assert result.is_error is True
    assert "Missing required ArgoCD API token" in text_of(result)


def test_a_tokenless_session_falls_back_to_the_registry_for_the_default_url() -> None:
    registry = TokenRegistry([{"baseUrl": DEFAULT_BASE_URL, "token": "registered-default"}])
    server = create_server(DEFAULT_BASE_URL, "", registry)
    assert server.resolve_client(DEFAULT_BASE_URL).client.api_token == "registered-default"


async def test_a_call_with_no_base_url_anywhere_is_an_error_result() -> None:
    server = create_server("", "", TokenRegistry())
    result = await server.call_tool("list_applications", {})
    assert result.is_error is True
    assert "Missing required ArgoCD base URL" in text_of(result)


def test_clients_are_cached_per_base_url_and_token() -> None:
    registry = TokenRegistry([{"baseUrl": OTHER_BASE_URL, "token": "registered-token"}])
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, registry)
    first = server.resolve_client(OTHER_BASE_URL)
    assert server.resolve_client(OTHER_BASE_URL) is first
    assert server.resolve_client(f"{OTHER_BASE_URL}/") is not None


# --- Tool registry ----------------------------------------------------------


def test_all_tools_are_registered_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_READ_ONLY", raising=False)
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())
    assert set(server.tools) == READ_TOOLS | WRITE_TOOLS


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_read_only_mode_unregisters_the_write_tools(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MCP_READ_ONLY", value)
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())
    assert set(server.tools) == READ_TOOLS


@pytest.mark.parametrize("value", ["false", "", "yes", "1"])
def test_read_only_mode_requires_the_literal_true(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MCP_READ_ONLY", value)
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())
    assert set(server.tools) >= WRITE_TOOLS


async def test_every_tool_advertises_the_argocd_base_url_argument_and_a_clean_schema() -> None:
    server: Server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())
    listed = await server._on_list_tools(None, None)
    assert {tool.name for tool in listed.tools} == READ_TOOLS | WRITE_TOOLS
    for tool in listed.tools:
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert "argocdBaseUrl" in schema["properties"]
        assert "argocdBaseUrl" not in schema.get("required", [])
        # Optional fields are plain types, not `anyOf: [X, null]` unions.
        assert "anyOf" not in json.dumps(schema)
        assert '"title"' not in json.dumps(schema)
        assert tool.description


async def test_tool_schema_uses_camel_case_wire_names() -> None:
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())
    listed = {tool.name: tool for tool in (await server._on_list_tools(None, None)).tools}
    events = listed["get_resource_events"].input_schema
    assert set(events["required"]) == {
        "applicationName",
        "applicationNamespace",
        "resourceUID",
        "resourceNamespace",
        "resourceName",
    }
    applications = listed["list_applications"].input_schema["properties"]
    assert applications["limit"] == {
        "type": "integer",
        "exclusiveMinimum": 0,
        "description": applications["limit"]["description"],
    }
    assert applications["offset"]["minimum"] == 0
    create = listed["create_application"].input_schema
    assert create["required"] == ["application"]
    source = create["$defs"]["ApplicationSource"]
    assert set(source["required"]) == {"repoURL", "path", "targetRevision"}


async def test_an_unknown_tool_is_a_protocol_error() -> None:
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())
    with pytest.raises(MCPError, match="Tool nope not found"):
        await server.call_tool("nope", {})


async def test_invalid_arguments_are_a_protocol_error_naming_the_field() -> None:
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())
    with pytest.raises(
        MCPError, match=re.compile(r"Input validation error.*applicationName", re.S)
    ):
        await server.call_tool("get_application", {})
    with pytest.raises(MCPError, match="Input validation error"):
        await server.call_tool("list_applications", {"limit": 0})


async def test_application_destination_needs_exactly_one_of_server_or_name() -> None:
    server = create_server(DEFAULT_BASE_URL, DEFAULT_TOKEN, TokenRegistry())
    application = {
        "metadata": {"name": "guestbook", "namespace": "argocd"},
        "spec": {
            "project": "default",
            "source": {
                "repoURL": "https://example.com/repo.git",
                "path": ".",
                "targetRevision": "HEAD",
            },
            "syncPolicy": {
                "syncOptions": [],
                "retry": {
                    "limit": 3,
                    "backoff": {"duration": "5s", "maxDuration": "1m", "factor": 2},
                },
            },
            "destination": {"server": "https://kubernetes.default.svc", "name": "in-cluster"},
        },
    }
    with pytest.raises(MCPError, match="Only one of server or name must be specified"):
        await server.call_tool("create_application", {"application": application})
