"""The argparse front end: strictness, port validation, and option plumbing."""

from __future__ import annotations

import pytest

from argocd_mcp.cmd import build_parser


def test_a_subcommand_is_required() -> None:
    with pytest.raises(SystemExit) as info:
        build_parser().parse_args([])
    assert info.value.code == 2


def test_stdio_takes_no_listener_options() -> None:
    assert build_parser().parse_args(["stdio"]).command == "stdio"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["stdio", "--port", "4000"])


def test_listener_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_BIND_ADDRESS", raising=False)
    args = build_parser().parse_args(["http"])
    assert args.port == 3000
    assert args.bind_address == "127.0.0.1"
    assert args.allowed_host_header == []
    assert args.allowed_origin == []
    assert args.allow_unauthenticated is False
    assert args.stateless is False


def test_mcp_bind_address_env_is_the_bind_address_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_BIND_ADDRESS", "0.0.0.0")
    assert build_parser().parse_args(["sse"]).bind_address == "0.0.0.0"
    assert build_parser().parse_args(["sse", "--bind-address", "::1"]).bind_address == "::1"


def test_repeatable_allow_list_flags_take_one_value_each() -> None:
    args = build_parser().parse_args(
        [
            "http",
            "--allowed-host-header",
            "a.example",
            "--allowed-host-header",
            "b.example",
            "--allowed-origin",
            "https://ide.example",
            "--stateless",
        ]
    )
    assert args.allowed_host_header == ["a.example", "b.example"]
    assert args.allowed_origin == ["https://ide.example"]
    assert args.stateless is True


@pytest.mark.parametrize("value", ["abc", "0", "65536", "-1", "3000.5"])
def test_an_out_of_range_or_non_numeric_port_is_rejected(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as info:
        build_parser().parse_args(["http", "--port", value])
    assert info.value.code == 2
    assert "Invalid --port value" in capsys.readouterr().err


def test_a_misspelled_security_flag_is_an_error_not_a_prefix_match() -> None:
    # argparse would otherwise accept '--allowed-host' as an abbreviation of
    # '--allowed-host-header' and start with a protection the operator did not ask for.
    for argv in (["http", "--allowed-host", "x"], ["sse", "--allow-unauth"], ["http", "--state"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)
