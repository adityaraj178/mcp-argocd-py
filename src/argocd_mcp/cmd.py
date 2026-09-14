"""Command-line entry point: `argocd-mcp {stdio|sse|http} [options]`."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence

from dotenv import load_dotenv

from argocd_mcp import __version__
from argocd_mcp.log import logger
from argocd_mcp.server.security import DEFAULT_BIND_ADDRESS


def _port(value: str) -> int:
    # Port 0 is rejected because the Origin allow list is port-specific and would
    # be pinned to ':0' while the kernel hands out a real one.
    try:
        port = int(value)
    except ValueError:
        port = None
    if port is None or not 1 <= port <= 65535:
        shown = "not a number" if port is None else port
        raise argparse.ArgumentTypeError(
            f"Invalid --port value: {shown}. Expected an integer between 1 and 65535."
        )
    return port


# Options shared by the two network transports. The inbound token is env-only:
# argv is readable by every user on the machine via `ps`.
def _add_listener_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--port", type=_port, default=3000, help="Port to listen on (default 3000)."
    )
    # This decides who may connect. --allowed-host-header only checks what an
    # already-connected client claims, which is why they are not named as a pair.
    parser.add_argument(
        "--bind-address",
        default=os.environ.get("MCP_BIND_ADDRESS") or DEFAULT_BIND_ADDRESS,
        help=(
            "Address to listen on. Defaults to loopback; widening it exposes every ArgoCD tool "
            "to the network and requires MCP_AUTH_TOKEN or --allow-unauthenticated."
        ),
    )
    # One value per occurrence, so '--allowed-host-header a.example stateless'
    # cannot swallow a following positional.
    parser.add_argument(
        "--allowed-host-header",
        action="append",
        default=[],
        metavar="HOSTNAME",
        help=(
            "Additional hostname accepted in a request Host header. Does not control who may "
            "connect. Repeatable. Loopback names are always accepted."
        ),
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help=(
            "Additional browser origin accepted in the Origin header, e.g. https://ide.example. "
            "Repeatable. Loopback origins on this port are always accepted."
        ),
    )
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help=(
            "Permit a non-loopback bind without MCP_AUTH_TOKEN. Only use this when the listener "
            "is already protected by an external layer."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False: otherwise a misspelled security flag ('--allowed-host')
    # is silently taken as a prefix of '--allowed-host-header' and the listener
    # starts with a different protection than the operator asked for.
    parser = argparse.ArgumentParser(
        prog="argocd-mcp", description="Argo CD MCP Server", allow_abbrev=False
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="{stdio,sse,http}")

    commands.add_parser("stdio", help="Start ArgoCD MCP server using stdio.", allow_abbrev=False)

    sse = commands.add_parser("sse", help="Start ArgoCD MCP server using SSE.", allow_abbrev=False)
    _add_listener_options(sse)

    http = commands.add_parser(
        "http", help="Start ArgoCD MCP server using Http Stream.", allow_abbrev=False
    )
    _add_listener_options(http)
    http.add_argument("--stateless", action="store_true", help="Run in stateless mode")
    return parser


def _start(run: Callable[[], None]) -> None:
    # A configuration the listener cannot honour is reported as a startup error
    # rather than an unhandled exception stack.
    try:
        run()
    except KeyboardInterrupt:
        pass
    except Exception as error:  # noqa: BLE001
        logger.error(str(error))
        sys.exit(1)


def main(argv: Sequence[str] | None = None) -> None:
    load_dotenv()
    args = build_parser().parse_args(argv)

    # Imported here so `--help` and argument errors do not pay for the SDK import.
    from argocd_mcp.server import transport

    if args.command == "stdio":
        _start(transport.connect_stdio_transport)
        return

    listener = dict(
        port=args.port,
        bind_address=args.bind_address,
        allowed_host_headers=list(args.allowed_host_header),
        allowed_origins=list(args.allowed_origin),
        auth_token=os.environ.get("MCP_AUTH_TOKEN"),
        allow_unauthenticated=args.allow_unauthenticated,
    )
    if args.command == "sse":
        _start(lambda: transport.connect_sse_transport(transport.TransportOptions(**listener)))
    else:
        _start(
            lambda: transport.connect_http_transport(
                transport.HttpTransportOptions(**listener, stateless=args.stateless)
            )
        )


if __name__ == "__main__":
    main()
