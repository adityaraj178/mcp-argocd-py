"""ArgoCD REST API client covering the operations the MCP tools expose."""

from __future__ import annotations

from typing import Any

from argocd_mcp.argocd.http import HttpClient
from argocd_mcp.argocd.types import JsonObject, ResourceRef


def _resource_query(application_namespace: str, resource_ref: ResourceRef) -> dict[str, Any]:
    return {
        "appNamespace": application_namespace,
        "namespace": resource_ref["namespace"],
        "resourceName": resource_ref["name"],
        "group": resource_ref["group"],
        "kind": resource_ref["kind"],
        "version": resource_ref["version"],
    }


class ArgoCDClient:
    def __init__(self, base_url: str, api_token: str) -> None:
        self.base_url = base_url
        self.api_token = api_token
        self.client = HttpClient(self.base_url, self.api_token)

    async def list_applications(
        self,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> JsonObject:
        response = await self.client.get(
            "/api/v1/applications", {"search": search} if search else None
        )
        body: JsonObject = response.body or {}

        # Strip heavy fields to reduce token usage.
        stripped_items = []
        for app in body.get("items") or []:
            metadata = app.get("metadata") or {}
            spec = app.get("spec") or {}
            status = app.get("status") or {}
            stripped_items.append(
                {
                    "metadata": {
                        "name": metadata.get("name"),
                        "namespace": metadata.get("namespace"),
                        "labels": metadata.get("labels"),
                        "creationTimestamp": metadata.get("creationTimestamp"),
                    },
                    "spec": {
                        "project": spec.get("project"),
                        "source": spec.get("source"),
                        "destination": spec.get("destination"),
                    },
                    "status": {
                        "sync": status.get("sync"),
                        "health": status.get("health"),
                        "summary": status.get("summary"),
                    },
                }
            )

        start = offset or 0
        end = start + limit if limit else len(stripped_items)
        items = stripped_items[start:end]

        return {
            "items": items,
            "metadata": {
                "resourceVersion": (body.get("metadata") or {}).get("resourceVersion"),
                "totalItems": len(stripped_items),
                "returnedItems": len(items),
                "hasMore": end < len(stripped_items),
            },
        }

    async def list_clusters(self, server: str | None = None, name: str | None = None) -> Any:
        query: dict[str, str] = {}
        if server:
            query["server"] = server
        if name:
            query["name"] = name
        response = await self.client.get("/api/v1/clusters", query or None)
        return response.body

    async def get_application(self, application_name: str, app_namespace: str | None = None) -> Any:
        response = await self.client.get(
            f"/api/v1/applications/{application_name}",
            {"appNamespace": app_namespace} if app_namespace else None,
        )
        return response.body

    async def get_app_project(self, project_name: str) -> Any:
        response = await self.client.get(f"/api/v1/projects/{project_name}")
        return response.body

    async def create_application(self, application: JsonObject) -> Any:
        response = await self.client.post("/api/v1/applications", None, application)
        return response.body

    async def update_application(self, application_name: str, application: JsonObject) -> Any:
        response = await self.client.put(
            f"/api/v1/applications/{application_name}", None, application
        )
        return response.body

    async def delete_application(
        self,
        application_name: str,
        app_namespace: str | None = None,
        cascade: bool | None = None,
        propagation_policy: str | None = None,
    ) -> Any:
        query: dict[str, str | bool] = {}
        if app_namespace:
            query["appNamespace"] = app_namespace
        if cascade is not None:
            query["cascade"] = cascade
        if propagation_policy:
            query["propagationPolicy"] = propagation_policy
        response = await self.client.delete(
            f"/api/v1/applications/{application_name}", query or None
        )
        return response.body

    async def sync_application(
        self,
        application_name: str,
        app_namespace: str | None = None,
        dry_run: bool | None = None,
        prune: bool | None = None,
        revision: str | None = None,
        sync_options: list[str] | None = None,
    ) -> Any:
        sync_request: dict[str, Any] = {}
        if app_namespace:
            sync_request["appNamespace"] = app_namespace
        if dry_run is not None:
            sync_request["dryRun"] = dry_run
        if prune is not None:
            sync_request["prune"] = prune
        if revision:
            sync_request["revision"] = revision
        if sync_options:
            sync_request["syncOptions"] = sync_options
        response = await self.client.post(
            f"/api/v1/applications/{application_name}/sync", None, sync_request or None
        )
        return response.body

    async def get_application_resource_tree(
        self, application_name: str, app_namespace: str | None = None
    ) -> Any:
        response = await self.client.get(
            f"/api/v1/applications/{application_name}/resource-tree",
            {"appNamespace": app_namespace} if app_namespace else None,
        )
        return response.body

    async def get_application_managed_resources(
        self, application_name: str, filters: dict[str, str] | None = None
    ) -> Any:
        response = await self.client.get(
            f"/api/v1/applications/{application_name}/managed-resources", filters
        )
        return response.body

    async def get_application_logs(self, application_name: str) -> list[Any]:
        logs: list[Any] = []
        await self.client.get_stream(
            f"/api/v1/applications/{application_name}/logs",
            {"follow": False, "tailLines": 100},
            logs.append,
        )
        return logs

    async def get_workload_logs(
        self,
        application_name: str,
        application_namespace: str,
        resource_ref: ResourceRef,
        container: str,
    ) -> list[Any]:
        logs: list[Any] = []
        await self.client.get_stream(
            f"/api/v1/applications/{application_name}/logs",
            {
                **_resource_query(application_namespace, resource_ref),
                "follow": False,
                "tailLines": 100,
                "container": container,
            },
            logs.append,
        )
        return logs

    async def get_pod_logs(self, application_name: str, pod_name: str) -> list[Any]:
        logs: list[Any] = []
        await self.client.get_stream(
            f"/api/v1/applications/{application_name}/pods/{pod_name}/logs",
            {"follow": False, "tailLines": 100},
            logs.append,
        )
        return logs

    async def get_application_events(
        self, application_name: str, app_namespace: str | None = None
    ) -> Any:
        response = await self.client.get(
            f"/api/v1/applications/{application_name}/events",
            {"appNamespace": app_namespace} if app_namespace else None,
        )
        return response.body

    async def get_resource(
        self, application_name: str, application_namespace: str, resource_ref: ResourceRef
    ) -> Any:
        response = await self.client.get(
            f"/api/v1/applications/{application_name}/resource",
            _resource_query(application_namespace, resource_ref),
        )
        return (response.body or {}).get("manifest")

    async def get_resource_events(
        self,
        application_name: str,
        application_namespace: str,
        resource_uid: str,
        resource_namespace: str,
        resource_name: str,
    ) -> Any:
        response = await self.client.get(
            f"/api/v1/applications/{application_name}/events",
            {
                "appNamespace": application_namespace,
                "resourceNamespace": resource_namespace,
                "resourceUID": resource_uid,
                "resourceName": resource_name,
            },
        )
        return response.body

    async def get_resource_actions(
        self, application_name: str, application_namespace: str, resource_ref: ResourceRef
    ) -> Any:
        response = await self.client.get(
            f"/api/v1/applications/{application_name}/resource/actions",
            _resource_query(application_namespace, resource_ref),
        )
        return response.body

    async def run_resource_action(
        self,
        application_name: str,
        application_namespace: str,
        resource_ref: ResourceRef,
        action: str,
    ) -> Any:
        response = await self.client.post(
            f"/api/v1/applications/{application_name}/resource/actions",
            _resource_query(application_namespace, resource_ref),
            action,
        )
        return response.body
