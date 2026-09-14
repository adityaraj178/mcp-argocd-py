# Argo CD MCP Server (Python)

An implementation of [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for [Argo CD](https://argo-cd.readthedocs.io/en/stable/), enabling AI assistants to interact with your Argo CD applications through natural language. This server allows for seamless integration with Visual Studio Code and other MCP clients through stdio and HTTP stream transport protocols.

## About this repository

**This is a Python port of [`argoproj-labs/mcp-for-argocd`](https://github.com/argoproj-labs/mcp-for-argocd), the official Argo CD MCP Server written in TypeScript.** It was ported from upstream release [v0.9.0](https://github.com/argoproj-labs/mcp-for-argocd/releases/tag/v0.9.0) and is intended to behave identically:

| | Upstream (TypeScript) | This repository (Python) |
|---|---|---|
| Tools, argument schemas, and results | 16 tools | the same 16 tools, same names and arguments |
| Transports | `stdio`, `sse`, `http` (`--stateless`) | the same |
| Environment variables and CLI flags | `ARGOCD_BASE_URL`, `ARGOCD_API_TOKEN`, `ARGOCD_TOKEN_REGISTRY_PATH`, `MCP_AUTH_TOKEN`, `MCP_BIND_ADDRESS`, `MCP_READ_ONLY`, `--port`, `--bind-address`, `--allowed-host-header`, `--allowed-origin`, `--allow-unauthenticated`, `--stateless` | the same |
| Credential and listener security model | see below | the same rules, same error messages, same test suite (ported) |
| Server identity (`serverInfo.name`) | `argocd-mcp` | `argocd-mcp` |
| How it is launched | `npx argocd-mcp@latest stdio` | `uvx --from git+https://github.com/adityaraj178/mcp-argocd-py argocd-mcp stdio` |
| Self-signed certificates | `NODE_TLS_REJECT_UNAUTHORIZED=0` | `SSL_CERT_FILE` or `ARGOCD_INSECURE_SKIP_VERIFY=true` |
| Runtime | Node.js, `@modelcontextprotocol/sdk` | Python 3.10+, the official `mcp` Python SDK |

The documentation below is adapted from the upstream README so that the two projects stay easy to compare. Credit for the design, the tool surface, the security model, and the original documentation and tests goes to the [Argo Proj Contributors](https://github.com/argoproj-labs/mcp-for-argocd/graphs/contributors); see [NOTICE](NOTICE). Both projects are licensed under the [Apache License 2.0](LICENSE). This port is a personal project and is not affiliated with or endorsed by the Argo Project or argoproj-labs. If you want the official, supported server, use [`argoproj-labs/mcp-for-argocd`](https://github.com/argoproj-labs/mcp-for-argocd).

## Features

- **Transport Protocols**: Supports both stdio and HTTP stream transport modes for flexible integration with different clients
- **Complete Argo CD API Integration**: Provides comprehensive access to Argo CD resources and operations
- **AI Assistant Ready**: Pre-configured tools for AI assistants to interact with Argo CD in natural language

## Available Tools

The server provides the following ArgoCD management tools:

### Cluster Management
- `list_clusters`: List all clusters registered with ArgoCD

### Project Management
- `get_appproject`: Get detailed information about a specific AppProject (project)

### Application Management
- `list_applications`: List and filter all applications
- `get_application`: Get detailed information about a specific application
- `create_application`: Create a new application
- `update_application`: Update an existing application
- `delete_application`: Delete an application
- `sync_application`: Trigger a sync operation on an application

### Resource Management
- `get_application_resource_tree`: Get the resource tree for a specific application
- `get_application_managed_resources`: Get managed resources for a specific application
- `get_application_workload_logs`: Get logs for application workloads (Pods, Deployments, etc.)
- `get_application_events`: Get events for an application
- `get_resource_events`: Get events for resources managed by an application
- `get_resources`: Get manifests for resources managed by an application
- `get_resource_actions`: Get available actions for resources
- `run_resource_action`: Run an action on a resource

## Installation

### Prerequisites

- Python 3.10 or higher
- [`uv`](https://docs.astral.sh/uv/) (recommended; `uvx` runs the server straight from this repository with no install step) or `pip`
- Argo CD instance with API access
- Argo CD API token (see the [docs for instructions](https://argo-cd.readthedocs.io/en/stable/developer-guide/api-docs/#authorization))

The package is not published to PyPI; install it from this repository:

```bash
uv tool install git+https://github.com/adityaraj178/mcp-argocd-py
# or: pip install git+https://github.com/adityaraj178/mcp-argocd-py
argocd-mcp --help
```

Or run it directly without installing, which is what the client configurations below do (`--from` names the package, `argocd-mcp` is the command it provides):

```bash
uvx --from git+https://github.com/adityaraj178/mcp-argocd-py argocd-mcp stdio
```

Pin a release by appending `@<tag>` to the Git URL, e.g. `git+https://github.com/adityaraj178/mcp-argocd-py@v0.9.0`.

### Usage with Cursor
1. Follow the [Cursor documentation for MCP support](https://docs.cursor.com/context/model-context-protocol), and create a `.cursor/mcp.json` file in your project:
```json
{
  "mcpServers": {
    "argocd-mcp": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/adityaraj178/mcp-argocd-py",
        "argocd-mcp",
        "stdio"
      ],
      "env": {
        "ARGOCD_BASE_URL": "<argocd_url>",
        "ARGOCD_API_TOKEN": "<argocd_token>"
      }
    }
  }
}
```

2. Start a conversation with Agent mode to use the MCP.

### Usage with VSCode

1. Follow the [Use MCP servers in VS Code documentation](https://code.visualstudio.com/docs/copilot/chat/mcp-servers), and create a `.vscode/mcp.json` file in your project:
```json
{
  "servers": {
    "argocd-mcp-stdio": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/adityaraj178/mcp-argocd-py",
        "argocd-mcp",
        "stdio"
      ],
      "env": {
        "ARGOCD_BASE_URL": "<argocd_url>",
        "ARGOCD_API_TOKEN": "<argocd_token>"
      }
    }
  }
}
```

2. Start a conversation with an AI assistant in VS Code that supports MCP.

### Usage with Claude Desktop

1. Follow the [MCP in Claude Desktop documentation](https://modelcontextprotocol.io/quickstart/user), and create a `claude_desktop_config.json` configuration file:
```json
{
  "mcpServers": {
    "argocd-mcp": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/adityaraj178/mcp-argocd-py",
        "argocd-mcp",
        "stdio"
      ],
      "env": {
        "ARGOCD_BASE_URL": "<argocd_url>",
        "ARGOCD_API_TOKEN": "<argocd_token>"
      }
    }
  }
}
```

2. Configure Claude Desktop to use this configuration file in settings.

### Self-signed Certificates

If your Argo CD instance uses self-signed certificates or certificates from a private Certificate Authority (CA), point the client at your CA bundle:

```
"SSL_CERT_FILE": "/path/to/ca-bundle.pem"
```

If that is not possible, TLS certificate validation for the connection to Argo CD can be disabled with:

```
"ARGOCD_INSECURE_SKIP_VERIFY": "true"
```

(This is the equivalent of `NODE_TLS_REJECT_UNAUTHORIZED=0` in the TypeScript server.)

> **Warning**: Disabling SSL verification reduces security. Use this setting only in development environments or when you understand the security implications.


### Providing ArgoCD Credentials

The server connects to ArgoCD using a **base URL** and an **API token**.

#### API token — header / env var only (mandatory)

The ArgoCD **API token is a secret and is only ever read from the transport layer**, never from a tool-call argument:

- **HTTP headers** (HTTP transport only): `x-argocd-api-token`.
- **Environment variables**: `ARGOCD_API_TOKEN` (all transports).

This token is **outbound only**: it authenticates this server to ArgoCD and never authorizes an inbound caller. See [Network Exposure](#network-exposure) for who may reach the listener.

This is the **default token**. It is **mandatory unless a [token registry](#token-registry--per-base-url-tokens-multi-instance) is configured**: on the HTTP transport, a connection that supplies no token (neither header nor env var) is rejected with `400 Bad Request`, but when a registry is configured a tokenless connection is allowed because each call resolves its own [registry token](#two-kinds-of-token). Keeping the token out of tool arguments ensures it never enters prompts, model context, or tool-call logs.

#### Base URL — header / env var, or per-call argument

The base URL may be supplied at the session level (resolved once when the server starts or when an HTTP client connects):

- **HTTP headers** (HTTP transport only): `x-argocd-base-url`.
- **Environment variables**: `ARGOCD_BASE_URL` (all transports).

In addition, **every tool accepts an optional `argocdBaseUrl` argument**:

- If a session default base URL exists, `argocdBaseUrl` is **optional** and overrides the default for that single call.
- If no session default base URL is configured (header and env var both absent), `argocdBaseUrl` is **required**; a call without it returns an error.

#### Token registry — per-base-URL tokens (multi-instance)

To target **multiple ArgoCD instances, each with its own token**, configure a token registry. Because the tokens are secrets, the registry is **read from a JSON file**, not an environment variable — point `ARGOCD_TOKEN_REGISTRY_PATH` at the file (e.g. a mounted Kubernetes secret). This keeps the tokens out of the process environment, crash dumps, and child-process inheritance.

```bash
ARGOCD_TOKEN_REGISTRY_PATH=/app/argocd-mcp/token-registry.json
```

The file contains a JSON array mapping a base URL to the token that should be used for it:

```json
[
  { "baseUrl": "https://argo-a.example.com", "token": "<token-a>" },
  { "baseUrl": "https://argo-b.example.com", "token": "<token-b>" }
]
```

> **Secure the file.** Restrict it to the server's user (e.g. `chmod 400`) and prefer a secret-management mechanism (Kubernetes secret volume, Vault agent, etc.) over a plaintext file on disk.

> **Local development.** The `make run` / `make dev` targets run without a registry by default; pass `ARGOCD_TOKEN_REGISTRY_PATH=/path/to/tokens.json` to use one. See [Running locally](#running-locally).

With a registry configured, a caller targets an instance by passing only the (non-secret) `argocdBaseUrl` argument; the server pairs it with the registered token. The token never appears in the tool-call payload.

##### Two kinds of token

The server resolves calls using one of two distinct tokens. Keeping them straight is what makes the security model work:

| | **Default token** | **Registry token** |
|---|---|---|
| **Source** | `x-argocd-api-token` header / `ARGOCD_API_TOKEN` env var (the session credential) | A `token` entry in the `ARGOCD_TOKEN_REGISTRY_PATH` JSON file, keyed by `baseUrl` |
| **Scope** | The **default base URL only** (`x-argocd-base-url` / `ARGOCD_BASE_URL`) | The **specific base URL** its entry is keyed to |
| **Used for** | A call that targets the default base URL | A call that targets any base URL present in the registry (including the default, as a fallback) |
| **Never used for** | Any base URL other than the default — it is **never** sent to a different host | Any base URL not registered |

The cardinal rule: **the default token is bound to the default base URL; every other host's token must come from the registry.** A registry token is bound to exactly the host it is registered under.

##### Resolution order

For a given call, the resolved base URL is the `argocdBaseUrl` argument if supplied, otherwise the session default. The token is then chosen by:

1. **Call targets the default base URL** → use the **default token**. If no default token was supplied (a tokenless session), fall back to the **registry token** for that base URL, if one exists.
2. **Call targets any other base URL** → use the **registry token** for that base URL only. The **default token is never used here** — it is not sent to a host other than the default one.
3. If neither applies (no token can be resolved for the requested base URL), the call returns a "Missing required ArgoCD API token" error and **no request is made** to that host.

> **Why the default token is bound to the default base URL.** The `argocdBaseUrl` argument comes from the tool call, so a caller (or a prompt-injected model) could point it at an arbitrary host. If the default token were paired with any supplied base URL, that token would be sent — as an `Authorization: Bearer` header — to the attacker's host. Restricting the default token to the default base URL, and requiring an explicit registry entry for every other host, prevents this token exfiltration. To target additional instances you must register their tokens (and thus their hostnames) up front.

Base URLs are normalized for lookup (lowercased scheme and host, default port and trailing slashes ignored), so minor formatting differences still match. When a registry is configured, the HTTP transport no longer requires `x-argocd-api-token` at connection time — a tokenless connection is allowed because the per-call base URL resolves its own token. If `ARGOCD_TOKEN_REGISTRY_PATH` is set but the file is missing, unreadable, or malformed, the server **fails closed**: it exits at startup rather than silently falling back to its default credential, so a misconfigured registry can never cause calls to be routed with the wrong token.

For example, a `tools/call` request overriding only the base URL:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "list_applications",
    "arguments": {
      "argocdBaseUrl": "https://argocd.other-cluster.example.com"
    }
  }
}
```

> **Overriding the base URL to a different instance requires a registry token.** The default token (`x-argocd-api-token` / `ARGOCD_API_TOKEN`) is bound to the default base URL only and is never sent to a different host. Overriding `argocdBaseUrl` to point at the **default** instance (same host, formatting aside) reuses the default token; pointing it at any **other** instance requires a [registry token](#two-kinds-of-token) for that instance, otherwise the call fails with "Missing required ArgoCD API token" and no request is sent. This is intentional — see [why the default token is bound to the default base URL](#token-registry--per-base-url-tokens-multi-instance) above.

### Network Exposure

The `http` and `sse` transports open a network listener that reaches every ArgoCD tool, including `create_application`, `delete_application`, `sync_application`, and `run_resource_action`. By default it binds loopback only.

**`ARGOCD_API_TOKEN` does not protect it.** That token authenticates this server *to ArgoCD*. It says nothing about who the caller is. Inbound access is controlled by the settings below.

| Setting | Flag | Env var | Default | What it does |
|---|---|---|---|---|
| Bind address | `--bind-address` | `MCP_BIND_ADDRESS` | `127.0.0.1` | Which address the listener accepts connections on. |
| Inbound token | — | `MCP_AUTH_TOKEN` | unset | When set, every request must carry `Authorization: Bearer <token>`. |
| Allowed `Host` | `--allowed-host-header` | — | loopback names | Extra hostname accepted in a request's `Host` header. Repeat per name. |
| Allowed `Origin` | `--allowed-origin` | — | loopback origins | Extra browser origin accepted in a request's `Origin` header. Repeat per origin. |
| External auth | `--allow-unauthenticated` | — | `false` | Allows a non-loopback bind with no token, when something in front already authenticates callers. |
| Port | `--port` | — | `3000` | Which port to listen on. |

> `--bind-address` decides who may connect. `--allowed-host-header` only checks what an already-connected client claims. They are not a pair, and the second is not a firewall.

Flags with no env var are passed as arguments, in a container too: `docker run <image> http --allow-unauthenticated`.

Behaviour:

- Widening the bind requires `MCP_AUTH_TOKEN` or `--allow-unauthenticated`. Otherwise the server logs why and exits non-zero instead of starting exposed.
- `Origin` is always checked, on scheme, host, and port. This is what stops a malicious web page, including one using DNS rebinding.
- `Host` is checked on a loopback bind, or on any bind with at least one `--allowed-host-header`. Otherwise the hostname clients legitimately use is unknown, so the check is skipped and a warning is logged.
- `GET /healthz` is exempt, so a kubelet probe still succeeds. It returns liveness only.
- Unusable configuration fails at startup with the reason, rather than being ignored.
- [Read-only mode](#read-only-mode) is independent of all of this and caps what any caller can do.

Exposing the listener deliberately:

```bash
export MCP_AUTH_TOKEN=<inbound_token>
argocd-mcp http --bind-address 0.0.0.0 --allowed-host-header mcp.internal.example.com
```

The container image (`ghcr.io/adityaraj178/mcp-argocd-py`, built by [this repository's workflow](.github/workflows/docker.yml)) keeps the same loopback default, so it needs no extra configuration when the caller shares its network namespace, such as a sidecar in the same Kubernetes pod:

```bash
docker run -e ARGOCD_BASE_URL=<argocd_url> -e ARGOCD_API_TOKEN=<argocd_token> \
  ghcr.io/adityaraj178/mcp-argocd-py
```

To publish a port, widen the bind and set an inbound credential:

```bash
docker run -p 3000:3000 \
  -e ARGOCD_BASE_URL=<argocd_url> -e ARGOCD_API_TOKEN=<argocd_token> \
  -e MCP_BIND_ADDRESS=0.0.0.0 -e MCP_AUTH_TOKEN=<inbound_token> \
  ghcr.io/adityaraj178/mcp-argocd-py
```

When the bind is widened and a proxy or mesh already authenticates callers, use `--allow-unauthenticated` instead of `MCP_AUTH_TOKEN`.

See [Operator notes](SECURITY.md#operator-notes-network-exposure) for the deployment caveats.

### Read Only Mode

If you want to run the MCP Server in a ReadOnly mode to avoid resource or application modification, you should set the environment variable:
```
"MCP_READ_ONLY": "true"
```
This will disable the following tools:
- `create_application`
- `update_application`
- `delete_application`
- `sync_application`
- `run_resource_action`

By default, all the tools will be available.

### Stateless Mode

By default, the HTTP transport assigns a session ID to each client connection and keeps an in-memory map of active sessions. This works well for single-instance deployments but causes `400` errors when multiple replicas are running without sticky sessions, because a request routed to a different pod will not find the session that was created on the original pod.

To run without session affinity requirements, start the server with the `--stateless` flag:

```bash
argocd-mcp http --stateless
```

Or with Docker:

```bash
docker run -p 3000:3000 \
  -e ARGOCD_BASE_URL=<argocd_url> -e ARGOCD_API_TOKEN=<argocd_token> \
  -e MCP_BIND_ADDRESS=0.0.0.0 -e MCP_AUTH_TOKEN=<inbound_token> \
  ghcr.io/adityaraj178/mcp-argocd-py http --stateless
```

The image has an `ENTRYPOINT`, so overriding the command replaces only the arguments. Publishing a port is what makes the wider bind and the inbound token necessary here; see [Network Exposure](#network-exposure).

In stateless mode:
- No `Mcp-Session-Id` is returned or required — any replica can handle any request
- ArgoCD credentials must be supplied on every request via environment variables or `x-argocd-base-url` / `x-argocd-api-token` headers (the base URL may also be overridden per call via the `argocdBaseUrl` tool argument; the API token is always header/env only)
- `GET /mcp` and `DELETE /mcp` return `405 Method Not Allowed` (session-level SSE and termination are not supported)

This mode is recommended for Kubernetes deployments with Horizontal Pod Autoscaling (HPA) where network-level sticky sessions are not available.

### Logging

Logs are written to **stderr** as JSON lines (stdout is reserved for the stdio transport's protocol frames). Set `LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR`) to change the verbosity; the default is `INFO`.

## For Development

1. Clone the repository:
```bash
git clone https://github.com/adityaraj178/mcp-argocd-py.git
cd mcp-argocd-py
```

2. Install project dependencies (creates `.venv`):
```bash
uv sync --all-groups
```

3. Start the development server with hot reloading enabled:
```bash
make dev
```
Once the server is running, you can utilize the MCP server within Visual Studio Code or other MCP client.

Run the checks the CI runs:

```bash
make lint   # ruff check, ruff format --check, mypy
make test   # pytest
```

### Running locally

The `Makefile` provides targets for running the server over the HTTP transport:

```bash
make run    # run the HTTP server
make dev    # run the HTTP server, restarting when the source changes
```

By default neither target sets any credentials — the server starts with no default base URL or token, so callers must supply them per request (`x-argocd-base-url` / `x-argocd-api-token` headers, or the `argocdBaseUrl` tool argument once a registry is configured). Override the port the same way:

```bash
make run PORT=4000
```

To configure credentials, export the relevant environment variable on the command line. There are three (all optional):

| Variable | Purpose |
|---|---|
| `ARGOCD_BASE_URL` | Default ArgoCD instance URL used when a call doesn't override it. |
| `ARGOCD_API_TOKEN` | Static API token for the default base URL. |
| `ARGOCD_TOKEN_REGISTRY_PATH` | Path to a JSON [token registry](#token-registry--per-base-url-tokens-multi-instance) mapping base URLs to tokens (for targeting multiple instances). |

These are all outbound credentials. For who may reach the listener, see [Network Exposure](#network-exposure).

```bash
# Single instance with a static base URL + token:
make run ARGOCD_BASE_URL=https://argo.example.com ARGOCD_API_TOKEN=<token>

# Multiple instances via a token registry:
make run ARGOCD_TOKEN_REGISTRY_PATH=/path/to/tokens.json

# Both — a default instance plus extra instances resolved from the registry:
make dev ARGOCD_BASE_URL=https://argo.example.com ARGOCD_API_TOKEN=<token> \
  ARGOCD_TOKEN_REGISTRY_PATH=/path/to/tokens.json
```

A `.env` file in the working directory is loaded at startup (values already present in the environment win).

See [Token resolution](#token-registry--per-base-url-tokens-multi-instance) for how the default token and registry interact. If `ARGOCD_TOKEN_REGISTRY_PATH` is set but the file is missing, unreadable, or malformed, the server fails closed at startup.

### Project layout

```
src/argocd_mcp/
├── cmd.py                 CLI (argparse): stdio | sse | http
├── log.py                 JSON-lines logger on stderr
├── models.py              Tool argument schemas (pydantic)
├── argocd/
│   ├── http.py            httpx wrapper (JSON + NDJSON log streaming)
│   └── client.py          ArgoCD REST client
└── server/
    ├── server.py          MCP server: tool registry, per-call credential resolution
    ├── security.py        Bind policy, bearer auth, Host/Origin validation
    ├── token_registry.py  Base URL -> token registry
    └── transport.py       stdio / SSE / Streamable HTTP (stateful & stateless)
tests/                     pytest suite (boots real listeners on loopback)
```
