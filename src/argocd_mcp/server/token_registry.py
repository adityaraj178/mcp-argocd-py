"""Base URL -> ArgoCD API token registry.

A read-only registry that maps an ArgoCD base URL to the API token that should
be used for it. It lets a single server target multiple ArgoCD instances, each
with its own token, without the token ever being passed in a tool-call payload:
the caller supplies only the (non-secret) base URL and the server pairs it with
the configured token.

Configuration source: a JSON file whose path is given by the
ARGOCD_TOKEN_REGISTRY_PATH environment variable. The tokens are secrets, so they
are read from a file (e.g. a mounted Kubernetes secret) rather than an env var,
keeping them out of the process environment, crash dumps, and child-process
inheritance. The file contains a JSON array of {"baseUrl", "token"} entries:

    [
      {"baseUrl": "https://argo-a.example.com", "token": "<token-a>"},
      {"baseUrl": "https://argo-b.example.com", "token": "<token-b>"}
    ]

Base URLs are normalized (lowercased scheme and host, trailing slashes stripped)
so trivial formatting differences between the configured value and the
requested value don't cause a lookup miss.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from argocd_mcp.log import logger

_DEFAULT_PORTS = {"http": "80", "https": "443", "ws": "80", "wss": "443"}


class TokenRegistry:
    def __init__(self, entries: Iterable[Mapping[str, Any]] | None = None) -> None:
        self._tokens_by_base_url: dict[str, str] = {}
        for entry in entries or ():
            base_url = entry.get("baseUrl") if isinstance(entry, Mapping) else None
            token = entry.get("token") if isinstance(entry, Mapping) else None
            if not base_url or not token:
                # Fail closed: a missing baseUrl/token is a misconfigured credential,
                # not something to silently skip. Don't include the token in the error.
                raise ValueError("ArgoCD token registry entry is missing baseUrl or token")
            self._tokens_by_base_url[TokenRegistry.normalize(str(base_url))] = str(token)

    def get_token(self, base_url: str) -> str | None:
        """The configured token for `base_url`, or None when it is not registered."""
        if not base_url:
            return None
        return self._tokens_by_base_url.get(TokenRegistry.normalize(base_url))

    def get_size(self) -> int:
        return len(self._tokens_by_base_url)

    def __len__(self) -> int:
        return self.get_size()

    @staticmethod
    def normalize(base_url: str) -> str:
        """Lowercase the scheme and host and drop trailing slashes.

        Falls back to a trimmed, de-slashed string when the value is not an
        absolute URL. Public so callers compare base URLs against the registry
        with exactly the normalization the lookup uses.
        """
        trimmed = base_url.strip()
        parts = urlsplit(trimmed)
        if not parts.scheme or not parts.netloc:
            return trimmed.rstrip("/")
        scheme = parts.scheme.lower()
        # The origin excludes userinfo and a default port, as WHATWG URL.origin does.
        host = parts.netloc.rsplit("@", 1)[-1].lower()
        if ":" in host and not host.endswith("]"):
            hostname, _, port = host.rpartition(":")
            if port == _DEFAULT_PORTS.get(scheme):
                host = hostname
        return f"{scheme}://{host}{parts.path.rstrip('/')}"


def parse_token_registry(raw: str) -> TokenRegistry:
    """Parse the raw JSON contents of a token registry file.

    Raises when the contents are not valid JSON or not a JSON array: an operator
    who configured a registry file expects token routing, so a malformed file is
    a misconfiguration surfaced loudly rather than silently degraded.
    """
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise ValueError(f"ArgoCD token registry file is not valid JSON: {error}") from error
    if not isinstance(parsed, list):
        raise ValueError("ArgoCD token registry file must contain a JSON array")
    return TokenRegistry(parsed)


_UNSET = object()


def token_registry_from_env(registry_path: str | None | object = _UNSET) -> TokenRegistry:
    """Build a TokenRegistry from the JSON file at ARGOCD_TOKEN_REGISTRY_PATH.

    Returns an empty registry when the variable is unset (the server then runs
    on its single default credential). When the variable IS set, this fails
    closed: if the file cannot be read or is malformed it raises, so the process
    exits at startup rather than silently falling back to the default credential,
    which could route calls to an instance with the wrong token.
    """
    if registry_path is _UNSET:
        registry_path = os.environ.get("ARGOCD_TOKEN_REGISTRY_PATH")
    if not isinstance(registry_path, str) or not registry_path.strip():
        return TokenRegistry()
    try:
        with open(registry_path.strip(), encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as error:
        raise ValueError(
            f'Failed to read ArgoCD token registry file at "{registry_path}": {error}'
        ) from error
    registry = parse_token_registry(raw)
    size = registry.get_size()
    logger.info(
        f'Loaded ArgoCD token registry from "{registry_path}" with {size} '
        f"entr{'y' if size == 1 else 'ies'}"
    )
    return registry
