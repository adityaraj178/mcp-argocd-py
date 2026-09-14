"""Process-wide logger.

Logs go to stderr as JSON lines. stdout is reserved for the stdio transport's
JSON-RPC frames, so nothing here may ever write to it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

# Log-level names pino would emit, so the output stays greppable across ports.
_PINO_LEVELS = {
    logging.DEBUG: 20,
    logging.INFO: 30,
    logging.WARNING: 40,
    logging.ERROR: 50,
    logging.CRITICAL: 60,
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": _PINO_LEVELS.get(record.levelno, record.levelno),
            "time": int(record.created * 1000),
            "pid": record.process,
            "name": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["err"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _configure() -> logging.Logger:
    root = logging.getLogger("argocd_mcp")
    if root.handlers:
        return root
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    root.propagate = False
    return root


logger = _configure()


def adopt(name: str) -> None:
    """Route another library's logger (e.g. uvicorn's) through the same stderr JSON handler."""
    other = logging.getLogger(name)
    other.handlers = list(logger.handlers)
    other.setLevel(logger.level)
    other.propagate = False


__all__ = ["adopt", "logger"]
