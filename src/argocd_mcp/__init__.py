"""Argo CD MCP Server (Python port of argoproj-labs/mcp-for-argocd)."""

from importlib.metadata import PackageNotFoundError, version

# The installable distribution, as named on PyPI / in pyproject.toml.
DIST_NAME = "mcp-argocd-py"
# What the server calls itself in the MCP handshake and on the CLI. Kept identical
# to the upstream TypeScript server so clients see the same server identity.
SERVER_NAME = "argocd-mcp"

try:
    __version__ = version(DIST_NAME)
except PackageNotFoundError:  # pragma: no cover - running from a source checkout
    __version__ = "0.0.0"

__all__ = ["DIST_NAME", "SERVER_NAME", "__version__"]
