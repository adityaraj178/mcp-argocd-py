# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for a suspected vulnerability.

To report one, create a draft GitHub security advisory on this repository:

https://github.com/adityaraj178/mcp-argocd-py/security/advisories/new

The advisory is private until it is published, and it is where the fix is coordinated. This is the same process the [Argo CD project](https://github.com/argoproj/argo-cd/blob/master/SECURITY.md) and the upstream [`mcp-for-argocd`](https://github.com/argoproj-labs/mcp-for-argocd/blob/main/SECURITY.md) server use.

This project is a Python port of [`argoproj-labs/mcp-for-argocd`](https://github.com/argoproj-labs/mcp-for-argocd) and shares its design. If the issue is in that shared design (the credential model, the listener protections, a tool's behaviour) rather than in this port's Python implementation, please also report it to the upstream project through [its advisory process](https://github.com/argoproj-labs/mcp-for-argocd/security/advisories/new), so both implementations get fixed.

Please include enough detail to reproduce the issue: the affected version, the configuration or transport involved (`stdio`, `http`, `sse`), and the steps or proof of concept.

This is a personal project, so a reply may take a little while. We are happy to coordinate a disclosure timeline with you and to credit you in the advisory.

**Findings from automated scanners** are already public, so report those as a normal [GitHub issue](https://github.com/adityaraj178/mcp-argocd-py/issues) — the discussion is usually of general benefit.

## Supported Versions

Fixes land on `main` and ship in the next release. Please confirm an issue against the latest release before reporting it.

## Operator Notes: Network Exposure

[Network Exposure](README.md#network-exposure) covers the settings. These are the parts that tend to surprise people.

- **The token gates the listener, not the caller.** `MCP_AUTH_TOKEN` is one shared secret, not a per-caller identity. Anyone holding it can call every registered tool and, with a [token registry](README.md#token-registry--per-base-url-tokens-multi-instance), target any base URL in it. For different reach per caller, run separate instances, each with its own token, registry entries, and [read-only mode](README.md#read-only-mode).
- **A proxy or a published port changes the `Origin`.** The allowed loopback origins use the port the server itself binds. Through `-p 8080:3000` or a TLS proxy the browser sends the origin it sees, so pass that to `--allowed-origin`. The `403` echoes the value it rejected.
- **Non-`http(s)` origins work, `null` does not.** `vscode-webview://<id>` and `chrome-extension://<id>` match as exact strings. The `null` a sandboxed or `file://` context sends is refused, since allowing it would admit every such context at once.
- **`Origin` alone is not enough on an exposed bind.** A browser omits `Origin` on a same-origin `GET`, and a DNS-rebound page believes it is same-origin. `Host` validation is what covers that, so pass `--allowed-host-header` or set `MCP_AUTH_TOKEN`. A proxy that forwards the client's original `Host` needs that name allow-listed too.
- **Browsers cannot call this server directly.** It sends no CORS headers, and `MCP_AUTH_TOKEN` cannot be attached to an `EventSource`. `--allowed-origin` only keeps *this* layer from rejecting a browser client that sits behind a CORS-terminating proxy.
- **Token whitespace.** Surrounding whitespace is stripped, since `$(cat token)` and `--from-file` secrets carry a trailing newline. Whitespace inside the token is a startup error, because such a token could never be sent in a header.
- **Bind address.** All of `127.0.0.0/8` counts as loopback, and IPv6 works with or without brackets. The default is IPv4 only, so a client insisting on `http://[::1]:3000` needs `--bind-address ::1`, or `::` for both stacks.
- **Loopback is reachable from a same-pod sidecar.** A published port (`docker run -p …`) is what does not reach a loopback listener; that needs an explicit wider bind.
- **Outbound TLS.** `ARGOCD_INSECURE_SKIP_VERIFY=true` disables certificate verification for the connection *to ArgoCD*. Prefer pointing `SSL_CERT_FILE` at your private CA bundle instead; `httpx` honours it.
