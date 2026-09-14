"""The ArgoCD MCP server: tool registry plus per-call credential resolution."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import mcp_types as types
from mcp.server.lowlevel import Server as LowLevelServer
from mcp.server.models import InitializationOptions
from mcp.shared.exceptions import MCPError
from pydantic import Field, ValidationError

from argocd_mcp import SERVER_NAME, __version__
from argocd_mcp.argocd.client import ArgoCDClient
from argocd_mcp.argocd.types import ResourceRef
from argocd_mcp.models import (
    APPLICATION_NAMESPACE_DESCRIPTION,
    APPLICATION_NAMESPACE_LOCATION_DESCRIPTION,
    ApplicationSchema,
    ArgoCDArgs,
    ResourceRefSchema,
    tool_input_schema,
)
from argocd_mcp.server.token_registry import TokenRegistry, token_registry_from_env

ToolHandler = Callable[[Any, ArgoCDClient], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[ArgoCDArgs]
    handler: ToolHandler


# --- Tool argument models ---------------------------------------------------


class ListApplicationsArgs(ArgoCDArgs):
    search: str | None = Field(
        default=None,
        description=(
            "Search applications by name. This is a partial match on the application name "
            'and does not support glob patterns (e.g. "*"). Optional.'
        ),
    )
    limit: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Maximum number of applications to return. Use this to reduce token usage when "
            "there are many applications. Optional."
        ),
    )
    offset: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Number of applications to skip before returning results. Use with limit for "
            "pagination. Optional."
        ),
    )


class ListClustersArgs(ArgoCDArgs):
    server: str | None = Field(default=None, description="Filter clusters by server URL. Optional.")
    name: str | None = Field(default=None, description="Filter clusters by name. Optional.")


class GetApplicationArgs(ArgoCDArgs):
    application_name: str
    application_namespace: str | None = Field(
        default=None, min_length=1, description=APPLICATION_NAMESPACE_DESCRIPTION
    )


class GetAppProjectArgs(ArgoCDArgs):
    project_name: str = Field(description="The name of the ArgoCD AppProject to fetch.")


class ApplicationInNamespaceArgs(ArgoCDArgs):
    application_name: str
    application_namespace: str | None = Field(
        default=None, min_length=1, description=APPLICATION_NAMESPACE_LOCATION_DESCRIPTION
    )


class GetApplicationManagedResourcesArgs(ArgoCDArgs):
    application_name: str
    kind: str | None = Field(
        default=None,
        description=(
            'Filter by Kubernetes resource kind (e.g., "ConfigMap", "Secret", "Deployment")'
        ),
    )
    namespace: str | None = Field(default=None, description="Filter by Kubernetes namespace")
    name: str | None = Field(default=None, description="Filter by resource name")
    version: str | None = Field(default=None, description="Filter by resource API version")
    group: str | None = Field(default=None, description="Filter by API group")
    app_namespace: str | None = Field(
        default=None, description="Filter by Argo CD application namespace"
    )
    project: str | None = Field(default=None, description="Filter by Argo CD project")


class GetApplicationWorkloadLogsArgs(ArgoCDArgs):
    application_name: str
    application_namespace: str = Field(min_length=1, description=APPLICATION_NAMESPACE_DESCRIPTION)
    resource_ref: ResourceRefSchema
    container: str


class GetResourceEventsArgs(ArgoCDArgs):
    application_name: str
    application_namespace: str = Field(min_length=1, description=APPLICATION_NAMESPACE_DESCRIPTION)
    resource_uid: str = Field(alias="resourceUID")
    resource_namespace: str
    resource_name: str


class GetResourcesArgs(ArgoCDArgs):
    application_name: str
    application_namespace: str = Field(min_length=1, description=APPLICATION_NAMESPACE_DESCRIPTION)
    resource_refs: list[ResourceRefSchema] | None = None


class ResourceRefArgs(ArgoCDArgs):
    application_name: str
    application_namespace: str = Field(min_length=1, description=APPLICATION_NAMESPACE_DESCRIPTION)
    resource_ref: ResourceRefSchema


class CreateApplicationArgs(ArgoCDArgs):
    application: ApplicationSchema


class UpdateApplicationArgs(ArgoCDArgs):
    application_name: str
    application: ApplicationSchema


class DeleteApplicationArgs(ApplicationInNamespaceArgs):
    cascade: bool | None = Field(
        default=None, description="Whether to cascade the deletion to child resources"
    )
    propagation_policy: str | None = Field(
        default=None,
        description='Deletion propagation policy (e.g., "Foreground", "Background", "Orphan")',
    )


class SyncApplicationArgs(ApplicationInNamespaceArgs):
    dry_run: bool | None = Field(
        default=None, description="Perform a dry run sync without applying changes"
    )
    prune: bool | None = Field(
        default=None, description="Remove resources that are no longer defined in the source"
    )
    revision: str | None = Field(
        default=None, description="Sync to a specific revision instead of the latest"
    )
    sync_options: list[str] | None = Field(
        default=None,
        description=(
            'Additional sync options (e.g., ["CreateNamespace=true", '
            '"PrunePropagationPolicy=foreground"])'
        ),
    )


class RunResourceActionArgs(ResourceRefArgs):
    action: str


def _resource_ref(ref: ResourceRefSchema) -> ResourceRef:
    return ResourceRef(
        uid=ref.uid,
        kind=ref.kind,
        namespace=ref.namespace,
        name=ref.name,
        version=ref.version,
        group=ref.group,
    )


def _application_payload(application: ApplicationSchema) -> dict[str, Any]:
    return application.model_dump(by_alias=True, exclude_none=True)


def is_read_only() -> bool:
    return os.environ.get("MCP_READ_ONLY", "").strip().lower() == "true"


# --- Server -----------------------------------------------------------------


class Server:
    """An MCP server bound to one session's ArgoCD credentials.

    `default_base_url` / `default_api_token` are the session credentials
    resolved at connect time (headers or env). `token_registry` maps additional
    base URLs to their tokens; when omitted it is loaded from
    ARGOCD_TOKEN_REGISTRY_PATH.
    """

    def __init__(
        self,
        argocd_base_url: str,
        argocd_api_token: str,
        token_registry: TokenRegistry | None = None,
    ) -> None:
        self.default_base_url = argocd_base_url
        self.default_api_token = argocd_api_token
        self.token_registry = (
            token_registry if token_registry is not None else token_registry_from_env()
        )
        self.argocd_client = ArgoCDClient(argocd_base_url, argocd_api_token)
        # Cache per-credential clients so the HttpClient is not rebuilt on every
        # call. Keyed by base URL + token, since the same base URL may resolve to
        # different tokens (request token vs. registry token vs. default).
        self._client_cache: dict[str, ArgoCDClient] = {}
        self._tools: dict[str, ToolSpec] = {}

        self.mcp: LowLevelServer[Any] = LowLevelServer(
            SERVER_NAME,
            version=__version__,
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )

        self._register_read_tools()
        if not is_read_only():
            self._register_write_tools()

    # -- tool registration --------------------------------------------------

    def add_json_output_tool(
        self,
        name: str,
        description: str,
        args_model: type[ArgoCDArgs],
        handler: ToolHandler,
    ) -> None:
        self._tools[name] = ToolSpec(name, description, args_model, handler)

    @property
    def tools(self) -> dict[str, ToolSpec]:
        return dict(self._tools)

    def _register_read_tools(self) -> None:
        async def list_applications(args: ListApplicationsArgs, client: ArgoCDClient) -> Any:
            return await client.list_applications(
                search=args.search or None, limit=args.limit, offset=args.offset
            )

        async def list_clusters(args: ListClustersArgs, client: ArgoCDClient) -> Any:
            return await client.list_clusters(server=args.server or None, name=args.name or None)

        async def get_application(args: GetApplicationArgs, client: ArgoCDClient) -> Any:
            return await client.get_application(args.application_name, args.application_namespace)

        async def get_appproject(args: GetAppProjectArgs, client: ArgoCDClient) -> Any:
            return await client.get_app_project(args.project_name)

        async def get_application_resource_tree(
            args: ApplicationInNamespaceArgs, client: ArgoCDClient
        ) -> Any:
            return await client.get_application_resource_tree(
                args.application_name, args.application_namespace
            )

        async def get_application_managed_resources(
            args: GetApplicationManagedResourcesArgs, client: ArgoCDClient
        ) -> Any:
            filters = {
                key: value
                for key, value in {
                    "kind": args.kind,
                    "namespace": args.namespace,
                    "name": args.name,
                    "version": args.version,
                    "group": args.group,
                    "appNamespace": args.app_namespace,
                    "project": args.project,
                }.items()
                if value
            }
            return await client.get_application_managed_resources(
                args.application_name, filters or None
            )

        async def get_application_workload_logs(
            args: GetApplicationWorkloadLogsArgs, client: ArgoCDClient
        ) -> Any:
            return await client.get_workload_logs(
                args.application_name,
                args.application_namespace,
                _resource_ref(args.resource_ref),
                args.container,
            )

        async def get_application_events(
            args: ApplicationInNamespaceArgs, client: ArgoCDClient
        ) -> Any:
            return await client.get_application_events(
                args.application_name, args.application_namespace
            )

        async def get_resource_events(args: GetResourceEventsArgs, client: ArgoCDClient) -> Any:
            return await client.get_resource_events(
                args.application_name,
                args.application_namespace,
                args.resource_uid,
                args.resource_namespace,
                args.resource_name,
            )

        async def get_resources(args: GetResourcesArgs, client: ArgoCDClient) -> Any:
            refs = [_resource_ref(ref) for ref in args.resource_refs or []]
            if not refs:
                tree = await client.get_application_resource_tree(args.application_name)
                refs = [
                    ResourceRef(
                        uid=node.get("uid"),
                        version=node.get("version"),
                        group=node.get("group"),
                        kind=node.get("kind"),
                        name=node.get("name"),
                        namespace=node.get("namespace"),
                    )
                    for node in (tree or {}).get("nodes") or []
                ]
            return list(
                await asyncio.gather(
                    *(
                        client.get_resource(args.application_name, args.application_namespace, ref)
                        for ref in refs
                    )
                )
            )

        async def get_resource_actions(args: ResourceRefArgs, client: ArgoCDClient) -> Any:
            return await client.get_resource_actions(
                args.application_name, args.application_namespace, _resource_ref(args.resource_ref)
            )

        self.add_json_output_tool(
            "list_applications",
            "list_applications returns list of applications",
            ListApplicationsArgs,
            list_applications,
        )
        self.add_json_output_tool(
            "list_clusters",
            "list_clusters returns list of clusters registered with ArgoCD",
            ListClustersArgs,
            list_clusters,
        )
        self.add_json_output_tool(
            "get_application",
            "get_application returns application by application name. Optionally specify the "
            "application namespace to get applications from non-default namespaces.",
            GetApplicationArgs,
            get_application,
        )
        self.add_json_output_tool(
            "get_appproject",
            "get_appproject returns an ArgoCD AppProject (project) by its name. AppProjects "
            "provide a logical grouping of applications and define allowed sources, "
            "destinations, cluster/repository whitelists, and RBAC roles.",
            GetAppProjectArgs,
            get_appproject,
        )
        self.add_json_output_tool(
            "get_application_resource_tree",
            "get_application_resource_tree returns resource tree for application by application "
            "name. Optionally specify the application namespace to get resource tree from "
            "applications in non-default namespaces.",
            ApplicationInNamespaceArgs,
            get_application_resource_tree,
        )
        self.add_json_output_tool(
            "get_application_managed_resources",
            "get_application_managed_resources returns managed resources for application by "
            "application name with optional filtering. Use filters to avoid token limits with "
            'large applications. Examples: kind="ConfigMap" for config maps only, '
            'namespace="production" for specific namespace, or combine multiple filters.',
            GetApplicationManagedResourcesArgs,
            get_application_managed_resources,
        )
        self.add_json_output_tool(
            "get_application_workload_logs",
            "get_application_workload_logs returns logs for application workload (Deployment, "
            "StatefulSet, Pod, etc.) by application name and resource ref and optionally "
            "container name",
            GetApplicationWorkloadLogsArgs,
            get_application_workload_logs,
        )
        self.add_json_output_tool(
            "get_application_events",
            "get_application_events returns events for application by application name. "
            "Optionally specify the application namespace to get events from applications in "
            "non-default namespaces.",
            ApplicationInNamespaceArgs,
            get_application_events,
        )
        self.add_json_output_tool(
            "get_resource_events",
            "get_resource_events returns events for a resource that is managed by an application",
            GetResourceEventsArgs,
            get_resource_events,
        )
        self.add_json_output_tool(
            "get_resources",
            "get_resources return manifests for resources specified by resourceRefs. If "
            "resourceRefs is empty or not provided, fetches all resources managed by the "
            "application.",
            GetResourcesArgs,
            get_resources,
        )
        self.add_json_output_tool(
            "get_resource_actions",
            "get_resource_actions returns actions for a resource that is managed by an application",
            ResourceRefArgs,
            get_resource_actions,
        )

    def _register_write_tools(self) -> None:
        async def create_application(args: CreateApplicationArgs, client: ArgoCDClient) -> Any:
            return await client.create_application(_application_payload(args.application))

        async def update_application(args: UpdateApplicationArgs, client: ArgoCDClient) -> Any:
            return await client.update_application(
                args.application_name, _application_payload(args.application)
            )

        async def delete_application(args: DeleteApplicationArgs, client: ArgoCDClient) -> Any:
            return await client.delete_application(
                args.application_name,
                app_namespace=args.application_namespace,
                cascade=args.cascade,
                propagation_policy=args.propagation_policy,
            )

        async def sync_application(args: SyncApplicationArgs, client: ArgoCDClient) -> Any:
            return await client.sync_application(
                args.application_name,
                app_namespace=args.application_namespace,
                dry_run=args.dry_run,
                prune=args.prune,
                revision=args.revision,
                sync_options=args.sync_options,
            )

        async def run_resource_action(args: RunResourceActionArgs, client: ArgoCDClient) -> Any:
            return await client.run_resource_action(
                args.application_name,
                args.application_namespace,
                _resource_ref(args.resource_ref),
                args.action,
            )

        self.add_json_output_tool(
            "create_application",
            "create_application creates a new ArgoCD application in the specified namespace. "
            "The application.metadata.namespace field determines where the Application resource "
            'will be created (e.g., "argocd", "argocd-apps", or any custom namespace).',
            CreateApplicationArgs,
            create_application,
        )
        self.add_json_output_tool(
            "update_application",
            "update_application updates application",
            UpdateApplicationArgs,
            update_application,
        )
        self.add_json_output_tool(
            "delete_application",
            "delete_application deletes application. Specify applicationNamespace if the "
            "application is in a non-default namespace to avoid permission errors.",
            DeleteApplicationArgs,
            delete_application,
        )
        self.add_json_output_tool(
            "sync_application",
            "sync_application syncs application. Specify applicationNamespace if the application "
            "is in a non-default namespace to avoid permission errors.",
            SyncApplicationArgs,
            sync_application,
        )
        self.add_json_output_tool(
            "run_resource_action",
            "run_resource_action runs an action on a resource",
            RunResourceActionArgs,
            run_resource_action,
        )

    # -- credential resolution ----------------------------------------------

    def resolve_client(self, argocd_base_url: str | None = None) -> ArgoCDClient:
        """Pick the ArgoCD client for a single tool call.

        The base URL may be overridden per call; the API token never is. The
        default (session) token is bound to the default base URL ONLY: it must
        never be paired with a caller-supplied base URL, or an attacker (or a
        prompt-injected model) could set argocdBaseUrl to an arbitrary host and
        have the server send the default token there. For any other base URL
        the token must come from the registry, i.e. the operator registered it.
        """
        base_url = argocd_base_url or self.default_base_url

        if not base_url:
            raise ValueError(
                "Missing required ArgoCD base URL: argocdBaseUrl. "
                "Provide it as a tool argument, or configure the server via the "
                "x-argocd-base-url header or ARGOCD_BASE_URL env var."
            )

        is_default_base_url = TokenRegistry.normalize(base_url) == TokenRegistry.normalize(
            self.default_base_url
        )
        if is_default_base_url:
            api_token = self.default_api_token or self.token_registry.get_token(base_url)
        else:
            api_token = self.token_registry.get_token(base_url)

        if not api_token:
            raise ValueError(
                f'Missing required ArgoCD API token for base URL "{base_url}". '
                "Provide it via the x-argocd-api-token header / ARGOCD_API_TOKEN env var, "
                "or register a token for this base URL in ARGOCD_TOKEN_REGISTRY."
            )

        # Fast path: default base URL with the default token reuses the session client.
        if base_url == self.default_base_url and api_token == self.default_api_token:
            return self.argocd_client

        cache_key = f"{base_url} {api_token}"
        client = self._client_cache.get(cache_key)
        if client is None:
            client = ArgoCDClient(base_url, api_token)
            self._client_cache[cache_key] = client
        return client

    # -- MCP handlers ---------------------------------------------------------

    async def _on_list_tools(
        self, _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=spec.name,
                    description=spec.description,
                    input_schema=tool_input_schema(spec.args_model),
                )
                for spec in self._tools.values()
            ]
        )

    async def _on_call_tool(
        self, _ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await self.call_tool(params.name, params.arguments or {})

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Run a registered tool and wrap its JSON result (or its error) for the client."""
        spec = self._tools.get(name)
        if spec is None:
            raise MCPError(types.INVALID_PARAMS, f"Tool {name} not found")
        try:
            args = spec.args_model.model_validate(arguments)
        except ValidationError as error:
            raise MCPError(types.INVALID_PARAMS, f"Input validation error: {error}") from error

        try:
            client = self.resolve_client(args.argocd_base_url)
            result = await spec.handler(args, client)
            return types.CallToolResult(
                is_error=False,
                content=[
                    types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))
                ],
            )
        except Exception as error:  # noqa: BLE001 - every failure is reported to the caller
            return types.CallToolResult(
                is_error=True, content=[types.TextContent(type="text", text=str(error))]
            )

    # -- running --------------------------------------------------------------

    def create_initialization_options(self) -> InitializationOptions:
        return self.mcp.create_initialization_options()

    async def run(self, read_stream: Any, write_stream: Any) -> None:
        await self.mcp.run(read_stream, write_stream, self.create_initialization_options())


def create_server(
    argocd_base_url: str,
    argocd_api_token: str,
    token_registry: TokenRegistry | None = None,
) -> Server:
    return Server(argocd_base_url, argocd_api_token, token_registry)
