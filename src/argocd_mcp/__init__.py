"""Argo CD MCP Server."""

from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = "argocd-mcp"

try:
    __version__ = version(PACKAGE_NAME)
except PackageNotFoundError:  # pragma: no cover - running from a source checkout
    __version__ = "0.0.0"

__all__ = ["PACKAGE_NAME", "__version__"]
