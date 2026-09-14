"""TokenRegistry construction, lookup, parsing, and fail-closed loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argocd_mcp.server.token_registry import (
    TokenRegistry,
    parse_token_registry,
    token_registry_from_env,
)

ENTRY_A = {"baseUrl": "https://argo-a.example.com", "token": "token-a"}

# --- TokenRegistry construction & lookup ---------------------------------


def test_get_token_returns_the_configured_token_for_a_registered_base_url() -> None:
    registry = TokenRegistry([ENTRY_A])
    assert registry.get_token("https://argo-a.example.com") == "token-a"


def test_get_token_returns_none_for_an_unregistered_base_url() -> None:
    registry = TokenRegistry([ENTRY_A])
    assert registry.get_token("https://argo-b.example.com") is None


def test_get_token_returns_none_for_an_empty_base_url() -> None:
    registry = TokenRegistry([ENTRY_A])
    assert registry.get_token("") is None


def test_lookups_are_normalized_host_case_and_trailing_slashes_are_ignored() -> None:
    registry = TokenRegistry([{"baseUrl": "https://Argo-A.Example.com/", "token": "token-a"}])
    assert registry.get_token("https://argo-a.example.com") == "token-a"
    assert registry.get_token("https://ARGO-A.EXAMPLE.COM///") == "token-a"


def test_normalize_drops_a_default_port_and_keeps_a_path() -> None:
    assert TokenRegistry.normalize("HTTPS://Argo.Example.com:443/argocd/") == (
        "https://argo.example.com/argocd"
    )
    assert (
        TokenRegistry.normalize("https://argo.example.com:8443") == "https://argo.example.com:8443"
    )
    assert TokenRegistry.normalize("  not-a-url// ") == "not-a-url"


def test_an_empty_registry_has_size_0_and_finds_nothing() -> None:
    registry = TokenRegistry()
    assert registry.get_size() == 0
    assert len(registry) == 0
    assert registry.get_token("https://argo-a.example.com") is None


# --- Fail-closed: constructor rejects malformed entries ------------------


def test_constructor_raises_when_an_entry_is_missing_its_token() -> None:
    with pytest.raises(ValueError, match="missing baseUrl or token"):
        TokenRegistry([{"baseUrl": "https://argo-a.example.com", "token": ""}])


def test_constructor_raises_when_an_entry_is_missing_its_base_url() -> None:
    with pytest.raises(ValueError, match="missing baseUrl or token"):
        TokenRegistry([{"baseUrl": "", "token": "token-a"}])


def test_constructor_error_does_not_leak_the_token_value() -> None:
    with pytest.raises(ValueError) as info:
        TokenRegistry([{"baseUrl": "", "token": "super-secret-token"}])
    assert "super-secret-token" not in str(info.value)


# --- parse_token_registry -------------------------------------------------


def test_parse_token_registry_builds_a_registry_from_a_valid_json_array() -> None:
    registry = parse_token_registry(
        json.dumps([ENTRY_A, {"baseUrl": "https://argo-b.example.com", "token": "token-b"}])
    )
    assert registry.get_size() == 2
    assert registry.get_token("https://argo-b.example.com") == "token-b"


def test_parse_token_registry_raises_on_invalid_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_token_registry("not json {")


def test_parse_token_registry_raises_when_the_json_is_not_an_array() -> None:
    with pytest.raises(ValueError, match="must contain a JSON array"):
        parse_token_registry(json.dumps({"baseUrl": "x", "token": "y"}))


# --- token_registry_from_env ---------------------------------------------


def test_token_registry_from_env_returns_an_empty_registry_when_the_path_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARGOCD_TOKEN_REGISTRY_PATH", raising=False)
    assert token_registry_from_env().get_size() == 0
    assert token_registry_from_env(None).get_size() == 0
    assert token_registry_from_env("").get_size() == 0
    assert token_registry_from_env("   ").get_size() == 0


def test_token_registry_from_env_reads_the_path_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps([ENTRY_A]), encoding="utf-8")
    monkeypatch.setenv("ARGOCD_TOKEN_REGISTRY_PATH", str(path))
    assert token_registry_from_env().get_token("https://argo-a.example.com") == "token-a"


def test_token_registry_from_env_loads_a_registry_from_a_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps([ENTRY_A]), encoding="utf-8")
    registry = token_registry_from_env(str(path))
    assert registry.get_size() == 1
    assert registry.get_token("https://argo-a.example.com") == "token-a"


def test_token_registry_from_env_fails_closed_when_the_configured_file_is_missing() -> None:
    with pytest.raises(ValueError, match="Failed to read ArgoCD token registry file"):
        token_registry_from_env("/nonexistent/path/registry.json")


def test_token_registry_from_env_fails_closed_when_the_configured_file_is_malformed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.json"
    path.write_text("not json {", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        token_registry_from_env(str(path))
