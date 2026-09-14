"""The listener's exposure boundary.

What it binds, which Host and Origin values it accepts, whether it demands an
inbound token, and that a configuration it cannot honour fails at startup.
Each test name states the invariant.
"""

from __future__ import annotations

import json
import re

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from argocd_mcp.server.security import (
    ListenerSecurity,
    ListenerSecurityOptions,
    apply_listener_security,
    canonicalize_bind_address,
    is_loopback_address,
    normalize_bind_address,
    resolve_allowed_hostnames,
    resolve_allowed_origins,
    resolve_listener_security,
)

PORT = 3000


def resolve(**kwargs) -> ListenerSecurity:
    return resolve_listener_security(ListenerSecurityOptions(**{"port": PORT, **kwargs}))


# --- resolve_listener_security ---------------------------------------------------


def test_the_default_bind_address_is_loopback_and_needs_no_inbound_credential() -> None:
    security = resolve()
    assert security.bind_address == "127.0.0.1"
    assert security.auth_token is None
    assert security.validate_host_header is True
    assert sorted(security.allowed_hostnames) == ["127.0.0.1", "[::1]", "localhost"]


def test_binding_past_loopback_without_inbound_auth_is_refused() -> None:
    with pytest.raises(ValueError, match=r"Refusing to bind to 0\.0\.0\.0"):
        resolve(bind_address="0.0.0.0")
    with pytest.raises(ValueError, match="MCP_AUTH_TOKEN"):
        resolve(bind_address="192.168.1.10")


def test_binding_past_loopback_is_allowed_with_an_inbound_token_or_an_explicit_ack() -> None:
    with_token = resolve(bind_address="0.0.0.0", auth_token="inbound-secret")
    assert with_token.bind_address == "0.0.0.0"
    assert with_token.auth_token == "inbound-secret"

    acked = resolve(bind_address="0.0.0.0", allow_unauthenticated=True)
    assert acked.auth_token is None


@pytest.mark.parametrize("auth_token", ["", "   "])
def test_a_set_but_empty_mcp_auth_token_is_a_startup_error_not_no_auth(auth_token: str) -> None:
    # An unexpanded '${VAR}' in compose, or a k8s secret key that resolved to
    # nothing. Treating it as unset would disable the bearer check while the
    # operator believes it is enforced.
    with pytest.raises(ValueError, match="MCP_AUTH_TOKEN is set but empty"):
        resolve(auth_token=auth_token)


def test_surrounding_whitespace_in_mcp_auth_token_is_stripped_inner_whitespace_is_fatal() -> None:
    assert resolve(auth_token="inbound-secret\n").auth_token == "inbound-secret"
    for auth_token in ["inbound secret", "inbound\tsecret", "a\nb"]:
        with pytest.raises(ValueError, match="MCP_AUTH_TOKEN contains whitespace"):
            resolve(auth_token=auth_token)


def test_a_bind_address_the_resolver_and_the_url_parser_could_read_differently_is_refused() -> None:
    # An empty --bind-address/MCP_BIND_ADDRESS is absent rather than invalid: it
    # falls back to the loopback default, which is the safe direction.
    assert resolve(bind_address="").bind_address == "127.0.0.1"
    for bind_address in [
        "evil.example@127.0.0.1",
        "0177.0.0.1",
        "127.1",
        "*",
        "${MCP_BIND_ADDRESS}",
        " ",
    ]:
        with pytest.raises(ValueError, match="Invalid --bind-address value"):
            resolve(bind_address=bind_address)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("LocalHost", "localhost"),
        ("  0.0.0.0  ", "0.0.0.0"),
        ("[::1]", "[::1]"),
        ("::", "::"),
        ("MCP.Internal.Example.COM", "mcp.internal.example.com"),
    ],
)
def test_canonicalize_bind_address_accepts_every_real_spelling_and_lower_cases_it(
    host: str, expected: str
) -> None:
    assert canonicalize_bind_address(host) == expected


def test_argocd_api_token_never_satisfies_the_inbound_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guards against an env fallback creeping in: the outbound ArgoCD credential
    # must not make an exposed bind look authorized.
    monkeypatch.setenv("ARGOCD_API_TOKEN", "outbound-only")
    with pytest.raises(ValueError, match="Refusing to bind"):
        resolve(bind_address="0.0.0.0")


def test_host_validation_is_off_on_an_exposed_bind_with_no_allow_list_and_on_with_one() -> None:
    no_allow_list = resolve(bind_address="0.0.0.0", auth_token="inbound-secret")
    assert no_allow_list.validate_host_header is False

    with_allow_list = resolve(
        bind_address="0.0.0.0", auth_token="inbound-secret", allowed_host_headers=["mcp.internal"]
    )
    assert with_allow_list.validate_host_header is True
    assert "mcp.internal" in with_allow_list.allowed_hostnames


# --- allow lists -----------------------------------------------------------------------


def test_the_host_allow_list_covers_loopback_the_bind_host_and_operator_additions() -> None:
    names = resolve_allowed_hostnames("192.168.1.10", ["mcp.internal", "https://ide.example:8443"])
    for expected in ["127.0.0.1", "localhost", "[::1]", "192.168.1.10", "mcp.internal"]:
        assert expected in names
    # A full origin is reduced to its hostname, matching the port-agnostic Host check.
    assert "ide.example" in names


def test_an_ipv6_hostname_survives_the_allow_list_unchanged() -> None:
    names = resolve_allowed_hostnames("[::1]", ["[2001:db8::5]"])
    assert "[::1]" in names
    assert "[2001:db8::5]" in names
    assert not any(name.startswith("[[") for name in names)


def test_an_unparseable_allowed_host_header_is_a_startup_error_not_a_silent_drop() -> None:
    with pytest.raises(ValueError, match=r"Invalid --allowed-host-header value: 2001:db8::5"):
        resolve_allowed_hostnames("0.0.0.0", ["2001:db8::5"])


@pytest.mark.parametrize(
    "value", ["*", "a.example,b.example", "${HOST}", "mcp.internal.", "has space"]
)
def test_an_allowed_host_header_that_parses_but_can_never_match_is_a_startup_error(
    value: str,
) -> None:
    with pytest.raises(ValueError, match="Invalid --allowed-host-header value"):
        resolve_allowed_hostnames("0.0.0.0", [value])


@pytest.mark.parametrize(
    "value", ["evil.example@127.0.0.1", "0177.0.0.1", "127.1", "a%2eexample.com"]
)
def test_an_allowed_host_header_the_resolver_and_url_parser_could_read_differently_is_refused(
    value: str,
) -> None:
    with pytest.raises(ValueError, match="Invalid --allowed-host-header value"):
        resolve_allowed_hostnames("0.0.0.0", [value])


@pytest.mark.parametrize(
    "value",
    [
        "0177.0.0.1/127.0.0.1",
        "http://0177.0.0.1/127.0.0.1",
        "127.0.0.1/evil",
        "mcp.internal?x=1",
        "mcp.internal#f",
    ],
)
def test_an_allowed_host_header_carrying_a_path_query_or_fragment_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid --allowed-host-header value"):
        resolve_allowed_hostnames("0.0.0.0", [value])


@pytest.mark.parametrize(
    "value", ["https://a.example,https://b.example", "https://*", "null", "file:///tmp"]
)
def test_an_allowed_origin_that_parses_but_can_never_match_is_a_startup_error(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid --allowed-origin value"):
        resolve_allowed_origins(PORT, [value])


def test_an_origin_with_a_non_http_scheme_can_be_allow_listed() -> None:
    origins = resolve_allowed_origins(
        PORT, ["vscode-webview://abc123", "chrome-extension://kkljfdmelbpgcnkhfnnbdgjbjmhllbcm"]
    )
    assert "vscode-webview://abc123" in origins
    assert "chrome-extension://kkljfdmelbpgcnkhfnnbdgjbjmhllbcm" in origins


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::", "[::]"])
def test_a_wildcard_bind_contributes_no_hostname_to_the_allow_list(wildcard: str) -> None:
    assert sorted(resolve_allowed_hostnames(wildcard)) == ["127.0.0.1", "[::1]", "localhost"]


@pytest.mark.parametrize(
    "host", ["localhost", "LOCALHOST", "127.0.0.1", "127.0.0.2", "127.255.0.1", "::1", "[::1]"]
)
def test_is_loopback_address_recognises_every_loopback_spelling(host: str) -> None:
    assert is_loopback_address(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "[::]",
        "192.168.1.10",
        "128.0.0.1",
        "1270.0.0.1",
        "127.0.0.1.evil.example",
        "mcp.internal",
    ],
)
def test_is_loopback_address_rejects_everything_else(host: str) -> None:
    assert is_loopback_address(host) is False


def test_a_non_default_127_8_bind_is_loopback_no_inbound_credential_required() -> None:
    security = resolve(bind_address="127.0.0.2")
    assert security.bind_address == "127.0.0.2"
    assert security.validate_host_header is True
    assert "127.0.0.2" in security.allowed_hostnames


def test_a_bracketed_bind_address_is_unwrapped_for_the_socket() -> None:
    assert normalize_bind_address("[::1]") == "::1"
    assert normalize_bind_address("::1") == "::1"
    assert normalize_bind_address("127.0.0.1") == "127.0.0.1"
    assert resolve(bind_address="[::1]").bind_address == "::1"


def test_the_origin_allow_list_is_port_and_scheme_specific() -> None:
    origins = resolve_allowed_origins(3000, ["https://ide.example"])
    assert "http://localhost:3000" in origins
    assert "https://localhost:3000" in origins
    assert "http://[::1]:3000" in origins
    assert "https://ide.example" in origins
    assert "http://localhost:9999" not in origins


def test_an_unparseable_allowed_origin_is_a_startup_error() -> None:
    with pytest.raises(ValueError, match="Invalid --allowed-origin"):
        resolve_allowed_origins(PORT, ["http://"])


# --- live listener ---------------------------------------------------------------------
#
# A protected app on a loopback port. The single guarded route stands in for /mcp:
# reaching it means every protection let the request through.


async def _reached(_request: Request) -> JSONResponse:
    return JSONResponse({"reached": True})


def guarded_app(security: ListenerSecurity) -> Starlette:
    inner = Starlette(routes=[Route("/mcp", _reached, methods=["POST"])])
    return apply_listener_security(inner, security)  # type: ignore[return-value]


def loopback_defaults(port: int) -> Starlette:
    return guarded_app(resolve_listener_security(ListenerSecurityOptions(port=port)))


JSON = {"content-type": "application/json"}


async def test_an_allowed_host_with_no_origin_reaches_the_route(start_app) -> None:
    async with start_app(loopback_defaults) as listener:
        res = await listener.request(headers={"host": f"localhost:{listener.port}", **JSON})
        assert res.status == 200
        assert "x-powered-by" not in res.headers
        assert "server" not in res.headers, "framework is not advertised"


async def test_a_host_outside_the_allow_list_is_rejected(start_app) -> None:
    async with start_app(loopback_defaults) as listener:
        # What an attacker on the same LAN sends when addressing the machine's own IP.
        res = await listener.request(headers={"host": "192.168.1.10:3000", **JSON})
        assert res.status == 403
        assert "Invalid Host" in res.body


async def test_a_cross_origin_request_is_rejected_even_when_the_host_is_forged_to_localhost(
    start_app,
) -> None:
    async with start_app(loopback_defaults) as listener:
        res = await listener.request(
            headers={"host": f"localhost:{listener.port}", "origin": "https://evil.example", **JSON}
        )
        assert res.status == 403
        assert "Invalid Origin" in res.body


async def test_a_co_resident_page_on_another_loopback_port_is_a_different_origin(start_app) -> None:
    async with start_app(loopback_defaults) as listener:
        res = await listener.request(
            headers={
                "host": f"localhost:{listener.port}",
                "origin": f"http://localhost:{listener.port + 1}",
                **JSON,
            }
        )
        assert res.status == 403
        assert "Invalid Origin" in res.body


async def test_an_opaque_origin_is_rejected(start_app) -> None:
    async with start_app(loopback_defaults) as listener:
        # Sent by a sandboxed iframe or a file:// page.
        res = await listener.request(
            headers={"host": f"localhost:{listener.port}", "origin": "null", **JSON}
        )
        assert res.status == 403


async def test_a_same_origin_browser_request_reaches_the_route(start_app) -> None:
    async with start_app(loopback_defaults) as listener:
        res = await listener.request(
            headers={
                "host": f"localhost:{listener.port}",
                "origin": f"http://localhost:{listener.port}",
                **JSON,
            }
        )
        assert res.status == 200


async def test_an_explicitly_allowed_origin_reaches_the_route(start_app) -> None:
    def build(port: int) -> Starlette:
        return guarded_app(
            resolve_listener_security(
                ListenerSecurityOptions(port=port, allowed_origins=["https://ide.example"])
            )
        )

    async with start_app(build) as listener:
        res = await listener.request(
            headers={"host": f"localhost:{listener.port}", "origin": "https://ide.example", **JSON}
        )
        assert res.status == 200


async def test_an_exposed_listener_still_rejects_cross_origin_requests(start_app) -> None:
    def build(port: int) -> Starlette:
        return guarded_app(
            resolve_listener_security(
                ListenerSecurityOptions(
                    bind_address="0.0.0.0", port=port, allow_unauthenticated=True
                )
            )
        )

    async with start_app(build) as listener:
        # Host validation is off here, so this proves Origin validation alone keeps a
        # malicious web page out of a deliberately exposed listener.
        evil = await listener.request(
            headers={"host": "mcp.internal:3000", "origin": "https://evil.example", **JSON}
        )
        assert evil.status == 403
        assert "Invalid Origin" in evil.body

        no_origin = await listener.request(headers={"host": "mcp.internal:3000", **JSON})
        assert no_origin.status == 200


async def test_an_exposed_listener_with_an_allow_list_enforces_host_validation(start_app) -> None:
    def build(port: int) -> Starlette:
        return guarded_app(
            resolve_listener_security(
                ListenerSecurityOptions(
                    bind_address="0.0.0.0",
                    port=port,
                    allow_unauthenticated=True,
                    allowed_host_headers=["mcp.internal"],
                )
            )
        )

    async with start_app(build) as listener:
        allowed = await listener.request(headers={"host": "mcp.internal:3000", **JSON})
        assert allowed.status == 200

        other = await listener.request(headers={"host": "other.internal:3000", **JSON})
        assert other.status == 403
        assert "Invalid Host" in other.body


async def test_a_configured_inbound_token_is_required_on_every_request(start_app) -> None:
    def build(port: int) -> Starlette:
        return guarded_app(
            resolve_listener_security(
                ListenerSecurityOptions(
                    bind_address="0.0.0.0", port=port, auth_token="inbound-secret"
                )
            )
        )

    async with start_app(build) as listener:
        base = {"host": f"localhost:{listener.port}", **JSON}

        missing = await listener.request(headers=base)
        assert missing.status == 401
        assert "Missing or invalid inbound bearer token" in missing.body
        assert missing.headers.get("www-authenticate") == "Bearer"

        wrong = await listener.request(headers={**base, "authorization": "Bearer wrong-secret"})
        assert wrong.status == 401

        # A same-length token rules out the comparison short-circuiting on length alone.
        same_length = await listener.request(
            headers={**base, "authorization": "Bearer inbound-secrXt"}
        )
        assert same_length.status == 401

        # Not a bearer credential at all.
        basic = await listener.request(headers={**base, "authorization": "Basic aW5ib3VuZA=="})
        assert basic.status == 401

        correct = await listener.request(headers={**base, "authorization": "Bearer inbound-secret"})
        assert correct.status == 200

        # RFC 9110 makes the scheme case-insensitive and tolerates extra whitespace.
        for header in ["bearer inbound-secret", "BEARER  inbound-secret"]:
            res = await listener.request(headers={**base, "authorization": header})
            assert res.status == 200, f'"{header}" is accepted'


async def test_a_trimmed_mcp_auth_token_is_the_value_the_listener_actually_accepts(
    start_app,
) -> None:
    def build(port: int) -> Starlette:
        return guarded_app(
            resolve_listener_security(
                ListenerSecurityOptions(port=port, auth_token="  inbound-secret\n")
            )
        )

    async with start_app(build) as listener:
        res = await listener.request(
            headers={
                "host": f"localhost:{listener.port}",
                "authorization": "Bearer inbound-secret",
                **JSON,
            }
        )
        assert res.status == 200


async def test_the_allow_lists_are_not_an_enumeration_oracle_for_an_unauthenticated_caller(
    start_app,
) -> None:
    # With the credential checked last, a caller with no token would get 403
    # (echoing the value) for a Host or Origin outside the allow list and 401 for
    # one inside it, enumerating both lists before ever presenting a token.
    def build(port: int) -> Starlette:
        return guarded_app(
            resolve_listener_security(
                ListenerSecurityOptions(
                    port=port,
                    auth_token="inbound-secret",
                    allowed_host_headers=["mcp.internal.example.com"],
                    allowed_origins=["https://ide.example"],
                )
            )
        )

    async with start_app(build) as listener:
        cases = [
            {"host": "other.internal:3000"},
            {"host": "mcp.internal.example.com:3000"},
            {"host": f"localhost:{listener.port}", "origin": "https://evil.example"},
            {"host": f"localhost:{listener.port}", "origin": "https://ide.example"},
        ]
        for headers in cases:
            res = await listener.request(headers={**headers, **JSON})
            assert res.status == 401, f"{json.dumps(headers)} is answered 401, not 403"
            assert not re.search(r"internal\.example\.com|ide\.example", res.body)


async def test_an_allow_listed_non_http_origin_reaches_the_route(start_app) -> None:
    def build(port: int) -> Starlette:
        return guarded_app(
            resolve_listener_security(
                ListenerSecurityOptions(port=port, allowed_origins=["vscode-webview://abc123"])
            )
        )

    async with start_app(build) as listener:
        allowed = await listener.request(
            headers={
                "host": f"localhost:{listener.port}",
                "origin": "vscode-webview://abc123",
                **JSON,
            }
        )
        assert allowed.status == 200

        # Another webview id is a different origin, so allow-listing one does not
        # admit every opaque-origin client.
        other = await listener.request(
            headers={
                "host": f"localhost:{listener.port}",
                "origin": "vscode-webview://def456",
                **JSON,
            }
        )
        assert other.status == 403
