"""Listener security: bind-address policy, inbound bearer auth, Host/Origin validation.

The http and sse transports open a network listener that reaches every ArgoCD
tool. ARGOCD_API_TOKEN authenticates this server *to ArgoCD* and says nothing
about who the caller is; the settings here are what control inbound access.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from argocd_mcp.log import logger

# Loopback names, in the bracketed form the Host header uses for IPv6.
LOOPBACK_HOSTNAMES = ["localhost", "127.0.0.1", "[::1]"]

# The MCP Streamable HTTP spec recommends loopback-only for local servers.
DEFAULT_BIND_ADDRESS = "127.0.0.1"

# Wildcard binds. Not names a client can send in a Host header, so they never
# enter an allow list.
WILDCARD_BIND_ADDRESSES = ["0.0.0.0", "::", "[::]"]

# WHATWG "special" schemes and their default ports; everything else has an opaque
# host and serializes its origin as the string "null".
_SPECIAL_SCHEMES = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*$")

# A DNS label. Narrower than a URL host parser, which also accepts '*', ',', '$',
# '{' and '}'.
_HOSTNAME_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

# All of 127.0.0.0/8 is loopback (RFC 5735).
_LOOPBACK_IPV4 = re.compile(r"^127(\.\d{1,3}){3}$")

# An opaque host is a client-generated id rather than a DNS name, but must still
# be a single token.
_OPAQUE_HOST = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


# --- URL helpers (a WHATWG-flavoured subset on top of urllib) -----------------


@dataclass(frozen=True)
class _ParsedUrl:
    scheme: str
    username: str | None
    password: str | None
    # Canonical hostname: lowercased, IPv6 bracketed and compressed, IPv4 dotted decimal.
    hostname: str | None
    # hostname plus ':port' when a non-default port is present (WHATWG url.host).
    host: str
    # 'scheme://host' for a special scheme, None where WHATWG serializes "null".
    origin: str | None


def _canonical_hostname(hostname: str) -> str:
    try:
        return f"[{ipaddress.IPv6Address(hostname).compressed}]"
    except ValueError:
        pass
    try:
        return str(ipaddress.IPv4Address(hostname))
    except ValueError:
        return hostname.lower()


def _parse_url(value: str) -> _ParsedUrl | None:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if not scheme or not _SCHEME.match(scheme):
        return None
    authority = parts.netloc.rsplit("@", 1)[-1]
    if scheme in _SPECIAL_SCHEMES:
        if not parts.hostname:
            return None
        hostname = _canonical_hostname(parts.hostname)
        host = hostname if port in (None, _SPECIAL_SCHEMES[scheme]) else f"{hostname}:{port}"
        return _ParsedUrl(
            scheme, parts.username, parts.password, hostname, host, f"{scheme}://{host}"
        )
    # Opaque host: kept as written, which is also how a browser serializes it.
    if authority.startswith("["):
        hostname = authority.split("]", 1)[0] + "]"
    else:
        hostname = authority.rpartition(":")[0] if port is not None else authority
    return _ParsedUrl(scheme, parts.username, parts.password, hostname or None, authority, None)


def _with_scheme(value: str, default: str = "http") -> str:
    return value if "://" in value else f"{default}://{value}"


# Reduce a hostname, host:port, or origin to its hostname, keeping the brackets
# around IPv6 so the result matches a LOOPBACK_HOSTNAMES entry.
def _normalize_hostname(value: str) -> str | None:
    url = _parse_url(_with_scheme(value))
    return url.hostname if url else None


# A bind address never carries a port, so a colon means IPv6.
def _bracket_if_ipv6(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _is_ipv6(value: str) -> bool:
    try:
        ipaddress.IPv6Address(value)
    except ValueError:
        return False
    return True


# --- Bind address ---------------------------------------------------------------


def is_loopback_address(address: str) -> bool:
    hostname = _normalize_hostname(_bracket_if_ipv6(address.lower()))
    return hostname is not None and (
        hostname in LOOPBACK_HOSTNAMES or bool(_LOOPBACK_IPV4.match(hostname))
    )


# Reject names the OS resolver and a URL parser could read differently. A URL
# parser reads 'evil.example@127.0.0.1' as userinfo plus a loopback host, and
# '0177.0.0.1' as octal for 127.0.0.1 where getaddrinfo gives 177.0.0.1. The
# loopback decision uses one parser and the socket bind uses the other.
def _is_usable_hostname(hostname: str) -> bool:
    bare = normalize_bind_address(hostname)
    if bare != hostname:
        return _is_ipv6(bare)
    if _is_ip(hostname):
        return True
    if len(hostname) > 253:
        return False
    labels = hostname.split(".")
    # An all-digit final label is a mistyped IP ('0177.0.0.1', '127.1'). DNS
    # forbids a numeric TLD, so no legitimate name has this shape.
    if labels[-1].isdigit():
        return False
    return all(_HOSTNAME_LABEL.match(label) for label in labels)


def canonicalize_bind_address(address: str) -> str:
    """Canonicalize a bind address before anything decides whether it is loopback."""
    canonical = address.strip().lower()
    if not canonical or not _is_usable_hostname(canonical):
        raise ValueError(
            f"Invalid --bind-address value: {address}. Expected an IP address or a hostname, "
            f"e.g. {DEFAULT_BIND_ADDRESS}, 0.0.0.0, [::1], or mcp.internal.example.com."
        )
    return canonical


def normalize_bind_address(address: str) -> str:
    """A socket takes a bare address ('::1'); Host headers use the bracketed form."""
    if address.startswith("[") and address.endswith("]"):
        return address[1:-1]
    return address


# --- Allow lists ----------------------------------------------------------------


# Same policy as canonicalize_bind_address, applied to an allow-list entry: reject
# anything the URL parser would rewrite rather than store a name the operator
# never wrote. A Host header is an authority, so compare the raw authority alone
# and refuse a path, query, or fragment.
def _strict_hostname(value: str) -> str | None:
    candidate = _with_scheme(value)
    url = _parse_url(candidate)
    if url is None or url.username or url.password or not url.hostname:
        return None
    after_scheme = candidate[candidate.index("://") + 3 :]
    authority = re.split(r"[/?#]", after_scheme, maxsplit=1)[0]
    if authority != after_scheme:
        return None
    # The port pattern cannot match inside a bracketed IPv6 literal, which ends in ']'.
    raw_host = re.sub(r":\d*$", "", authority).lower()
    return url.hostname if raw_host == url.hostname else None


def resolve_allowed_hostnames(
    bind_address: str, allowed_host_headers: Sequence[str] | None = None
) -> list[str]:
    """The Host allow list: loopback names, the bind address itself, and operator entries.

    Raises on an unusable entry, since a URL host parser accepts '*', a
    comma-joined list, and an unexpanded '${VAR}', none of which a request can match.
    """
    names = dict.fromkeys(LOOPBACK_HOSTNAMES)
    for value in allowed_host_headers or ():
        hostname = _strict_hostname(value)
        if not hostname or not _is_usable_hostname(hostname):
            raise ValueError(
                f"Invalid --allowed-host-header value: {value}. Expected a single hostname, "
                "host:port, or origin. Wrap an IPv6 literal in brackets, e.g. [::1]. There is "
                "no wildcard: repeat --allowed-host-header for each name, and omit it entirely "
                "to accept any Host."
            )
        names[hostname] = None
    if bind_address not in WILDCARD_BIND_ADDRESSES:
        hostname = _normalize_hostname(_bracket_if_ipv6(bind_address))
        if hostname:
            names[hostname] = None
    return list(names)


def _canonical_origin(value: str) -> str | None:
    url = _parse_url(value)
    if url is None:
        return None
    # Parsing is not enough: 'https://a.example,https://b.example' yields the
    # host 'a.example,https', and 'https://*' the host '*'.
    if url.origin is not None:
        return url.origin if url.hostname and _is_usable_hostname(url.hostname) else None
    if not url.host or not url.hostname or not _OPAQUE_HOST.match(url.hostname):
        return None
    return f"{url.scheme}://{url.host}"


def resolve_allowed_origins(port: int, allowed_origins: Sequence[str] | None = None) -> list[str]:
    """The Origin allow list.

    Unlike Host these are full origins: scheme and port are part of the browser's
    origin boundary, so a co-resident dev server on another port is not a match.
    """
    origins: dict[str, None] = {}
    # How a page served by this listener over loopback addresses it. https covers
    # reaching it through a TLS proxy.
    for hostname in LOOPBACK_HOSTNAMES:
        for scheme in ("http", "https"):
            url = _parse_url(f"{scheme}://{hostname}:{port}")
            if url is None or url.origin is None:
                raise ValueError(f"Invalid port for the Origin allow list: {port}")
            origins[url.origin] = None
    for value in allowed_origins or ():
        # 'null' is the opaque-origin sentinel for a sandboxed iframe or file:// page.
        # Allow-listing it would admit every such context at once.
        if value.strip().lower() == "null":
            raise ValueError(
                f'Invalid --allowed-origin value: {value}. "null" is what a browser sends for '
                "any sandboxed or file:// context and cannot be allowed."
            )
        origin = _canonical_origin(_with_scheme(value, "https"))
        if not origin:
            raise ValueError(
                f"Invalid --allowed-origin value: {value}. Expected an origin such as "
                "https://ide.example or vscode-webview://<id>."
            )
        origins[origin] = None
    return list(origins)


# --- Middleware ------------------------------------------------------------------


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            header: str = value.decode("latin-1")
            return header
    return None


async def _send_json_rpc_error(
    send: Send, status: int, message: str, extra_headers: Sequence[tuple[bytes, bytes]] = ()
) -> None:
    body = json.dumps(
        {"jsonrpc": "2.0", "error": {"code": -32000, "message": message}, "id": None}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *extra_headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# Hashing first gives compare_digest equal-length inputs, so a wrong-length guess
# is indistinguishable from a wrong-value one.
def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def secrets_match(a: str, b: str) -> bool:
    return hmac.compare_digest(_sha256(a), _sha256(b))


def bearer_auth_ok(authorization: str | None, expected_token: str) -> bool:
    """RFC 9110: the scheme is case-insensitive and any whitespace may follow it."""
    parts = (authorization or "").split()
    if len(parts) != 2:
        return False
    scheme, presented = parts
    return scheme.lower() == "bearer" and secrets_match(presented, expected_token)


def host_header_ok(host: str | None, allowed_hostnames: Sequence[str]) -> bool:
    if not host:
        return False
    hostname = _normalize_hostname(host)
    return hostname is not None and hostname in {name.lower() for name in allowed_hostnames}


def origin_header_ok(origin: str | None, allowed_origins: Sequence[str]) -> bool:
    """Only requests carrying an Origin are checked.

    A browser omits it on a same-origin GET, and a DNS-rebound page believes it
    is same-origin, so Host validation is what covers that case.
    """
    if not origin:
        return True
    normalized = None if origin.lower() == "null" else _canonical_origin(origin)
    return normalized is not None and normalized in allowed_origins


@dataclass(frozen=True)
class ListenerSecurity:
    # Bind address in the bare form a socket accepts.
    bind_address: str
    allowed_hostnames: list[str]
    allowed_origins: list[str]
    auth_token: str | None
    # Host validation only makes sense for a loopback bind or an operator-supplied
    # allow list. On an exposed bind with no list, the name clients legitimately
    # use is unknown.
    validate_host_header: bool


@dataclass(frozen=True)
class ListenerSecurityOptions:
    port: int
    bind_address: str | None = None
    allowed_host_headers: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    auth_token: str | None = None
    allow_unauthenticated: bool = False


def resolve_listener_security(options: ListenerSecurityOptions) -> ListenerSecurity:
    """Validate a listener's exposure before anything binds a socket.

    Raises rather than warns: a bind past loopback puts every MCP tool in reach
    of anything that can route here.
    """
    bind_address = canonicalize_bind_address(options.bind_address or DEFAULT_BIND_ADDRESS)

    # A set-but-empty token is a broken config (an unexpanded '${VAR}', a k8s secret
    # key that resolved to nothing), not a request to run without authentication.
    if options.auth_token is not None and options.auth_token.strip() == "":
        raise ValueError(
            "MCP_AUTH_TOKEN is set but empty. Give it a value to require an inbound bearer "
            "token, or unset it entirely."
        )
    # Surrounding whitespace comes from how the secret was delivered ($(cat token),
    # a k8s --from-file secret) and is stripped. Whitespace inside is fatal: RFC 6750
    # token68 has no room for it and CR/LF cannot appear in a header at all.
    auth_token = options.auth_token.strip() if options.auth_token is not None else None
    if auth_token is not None and re.search(r"\s", auth_token):
        raise ValueError(
            "MCP_AUTH_TOKEN contains whitespace, which cannot be sent in an Authorization "
            "header. Use a token of printable non-space characters, e.g. `openssl rand -hex 32`."
        )

    if (
        not is_loopback_address(bind_address)
        and not auth_token
        and not options.allow_unauthenticated
    ):
        raise ValueError(
            f"Refusing to bind to {bind_address} without inbound authentication. The MCP "
            "listener exposes every ArgoCD tool to anything that can reach it, and "
            "ARGOCD_API_TOKEN authenticates this server to ArgoCD, not the caller. Either set "
            'MCP_AUTH_TOKEN to require an inbound "Authorization: Bearer <token>" header, or '
            "pass --allow-unauthenticated if the listener is already protected by an external "
            "layer (a sidecar proxy, service mesh, or network policy). To listen locally only, "
            f"set --bind-address to {DEFAULT_BIND_ADDRESS}."
        )

    return ListenerSecurity(
        bind_address=normalize_bind_address(bind_address),
        allowed_hostnames=resolve_allowed_hostnames(bind_address, options.allowed_host_headers),
        allowed_origins=resolve_allowed_origins(options.port, options.allowed_origins),
        auth_token=auth_token,
        validate_host_header=is_loopback_address(bind_address)
        or len(options.allowed_host_headers or ()) > 0,
    )


class ListenerSecurityMiddleware:
    """ASGI middleware guarding every route of the wrapped app.

    The credential runs first. Last, it would answer 403 for a Host or Origin
    outside the allow list and 401 for one inside it, which is enough to
    enumerate the allow lists without presenting a token.
    """

    def __init__(self, app: ASGIApp, security: ListenerSecurity) -> None:
        self.app = app
        self.security = security

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        security = self.security
        if security.auth_token and not bearer_auth_ok(
            _header(scope, b"authorization"), security.auth_token
        ):
            await _send_json_rpc_error(
                send,
                401,
                "Missing or invalid inbound bearer token",
                [(b"www-authenticate", b"Bearer")],
            )
            return
        if security.validate_host_header:
            host = _header(scope, b"host")
            if not host_header_ok(host, security.allowed_hostnames):
                message = f"Invalid Host header: {host}" if host else "Invalid Host header"
                await _send_json_rpc_error(send, 403, message)
                return
        origin = _header(scope, b"origin")
        if not origin_header_ok(origin, security.allowed_origins):
            await _send_json_rpc_error(
                send,
                403,
                f"Invalid Origin: {origin}. Pass --allowed-origin to accept it; note that scheme "
                "and port are part of an origin, so a proxy or a published container port "
                "changes it.",
            )
            return
        await self.app(scope, receive, send)


def apply_listener_security(app: ASGIApp, security: ListenerSecurity) -> ASGIApp:
    """Guard every route of `app`; anything mounted beside the result stays exempt."""
    if not security.validate_host_header:
        logger.warning(
            f"Host header validation is disabled because the listener binds "
            f"{security.bind_address} with no --allowed-host-header entries. Pass "
            "--allowed-host-header <hostname> for each name clients use to reach this server "
            "to enable it."
        )
    _warn_if_unauthenticated_and_exposed(security)
    return ListenerSecurityMiddleware(app, security)


# --allow-unauthenticated is the one setting that leaves the listener open. It is
# legitimate behind a proxy or mesh, but an operator who later removes that layer
# has nothing else to remind them.
def _warn_if_unauthenticated_and_exposed(security: ListenerSecurity) -> None:
    if security.auth_token or is_loopback_address(security.bind_address):
        return
    logger.warning(
        f"The listener binds {security.bind_address} with no inbound authentication "
        "(--allow-unauthenticated). Every ArgoCD tool, including "
        "create/update/delete/sync_application and run_resource_action, is callable by anything "
        "that can reach this address. This is only safe behind an external layer that "
        "authenticates callers (sidecar proxy, service mesh, network policy). Set MCP_AUTH_TOKEN "
        "to require a bearer token here instead, and consider MCP_READ_ONLY=true to unregister "
        "the write tools."
    )
