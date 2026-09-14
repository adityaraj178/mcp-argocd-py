"""Tool argument models, the Python counterpart of the zod schemas.

Every tool's arguments extend `ArgoCDArgs`, which carries the optional
per-call `argocdBaseUrl` override. Field names are camelCase on the wire
(matching the TypeScript server) and snake_case in Python.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

APPLICATION_NAMESPACE_DESCRIPTION = (
    "The namespace where the ArgoCD application resource will be created.\n"
    "This is the namespace of the Application resource itself, not the destination "
    "namespace for the application's resources.\n"
    "You can specify any valid Kubernetes namespace (e.g., 'argocd', 'argocd-apps', "
    "'my-namespace', etc.).\n"
    "The default ArgoCD namespace is typically 'argocd', but you can use any namespace "
    "you prefer."
)

APPLICATION_NAMESPACE_LOCATION_DESCRIPTION = (
    "The namespace where the application is located. "
    "Required if application is not in the default namespace."
)

ARGOCD_BASE_URL_DESCRIPTION = (
    'ArgoCD base URL to use for this call (e.g. "https://argocd.example.com"). Overrides '
    "the server default. Optional if the server is configured with a default base URL "
    "(x-argocd-base-url header or ARGOCD_BASE_URL env var); otherwise required."
)


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class ArgoCDArgs(CamelModel):
    """Per-call argument any tool accepts to target a specific ArgoCD instance.

    The API token is deliberately NOT a tool argument: it is only ever resolved
    from the x-argocd-api-token header / ARGOCD_API_TOKEN env var so the secret
    never enters prompts, model context, or tool-call logs.
    """

    argocd_base_url: str | None = Field(default=None, description=ARGOCD_BASE_URL_DESCRIPTION)


class ResourceRefSchema(CamelModel):
    uid: str
    kind: str
    namespace: str
    name: str
    version: str
    group: str


class ApplicationMetadata(CamelModel):
    name: str
    namespace: str = Field(min_length=1, description=APPLICATION_NAMESPACE_DESCRIPTION)


class ApplicationSource(CamelModel):
    repo_url: str = Field(alias="repoURL")
    path: str
    target_revision: str


class SyncPolicyAutomated(CamelModel):
    prune: bool
    self_heal: bool


class RetryBackoff(CamelModel):
    duration: str
    max_duration: str
    # ArgoCD declares these as int64; a float here would serialize as `3.0` and be rejected.
    factor: int


class SyncPolicyRetry(CamelModel):
    limit: int
    backoff: RetryBackoff


class SyncPolicy(CamelModel):
    sync_options: list[str]
    automated: SyncPolicyAutomated | None = None
    retry: SyncPolicyRetry


class ApplicationDestination(CamelModel):
    server: str | None = None
    namespace: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> ApplicationDestination:
        if bool(self.server) == bool(self.name):
            raise ValueError("Only one of server or name must be specified in destination")
        return self


class ApplicationSpec(CamelModel):
    project: str
    source: ApplicationSource
    sync_policy: SyncPolicy
    destination: ApplicationDestination = Field(
        description=(
            "The destination of the application.\nOnly one of server or name must be specified."
        )
    )


class ApplicationSchema(CamelModel):
    metadata: ApplicationMetadata
    spec: ApplicationSpec


def _simplify(node: Any) -> Any:
    """Collapse `anyOf: [X, null]` into X and drop noise that only pydantic emits."""
    if isinstance(node, list):
        return [_simplify(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: _simplify(value) for key, value in node.items() if key != "title"}
    if node.get("default", ...) is None:
        del node["default"]
    variants = node.get("anyOf")
    if isinstance(variants, list):
        non_null = [v for v in variants if v != {"type": "null"}]
        if len(non_null) == 1 and len(variants) == 2:
            merged = {k: v for k, v in node.items() if k != "anyOf"}
            merged.update(non_null[0])
            return merged
    return node


def tool_input_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for a tool's arguments, keyed by wire (camelCase) names."""
    schema: dict[str, Any] = _simplify(model.model_json_schema(by_alias=True))
    schema.pop("description", None)
    return schema
