"""Minimal typing for the slice of the ArgoCD API this server touches.

The TypeScript original generated a full type surface from ArgoCD's swagger
definition. The Python port passes ArgoCD payloads through untouched, so only
the shapes this code constructs itself are spelled out.
"""

from __future__ import annotations

from typing import Any, TypedDict

JsonObject = dict[str, Any]


class ResourceRef(TypedDict):
    uid: str
    kind: str
    namespace: str
    name: str
    version: str
    group: str
