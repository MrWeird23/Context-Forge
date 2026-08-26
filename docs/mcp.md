# MCP integration

ContextForge 1.1.0 ships a local, read-only Model Context Protocol server over stdio. It lets MCP-capable coding agents request repository intelligence without parsing terminal output or receiving permission to modify source files.

The package supports MCP Python SDK `1.25` through `1.29`. MCP SDK 2.x uses a different high-level server API and is outside this compatibility line.

## Install and register

Install ContextForge with `pipx install contextforge-cli`, then register it with the client.

### Claude Code

```bash
claude mcp add --transport stdio --scope user contextforge -- contextforge-mcp
claude mcp get contextforge
```

Remove it with `claude mcp remove --scope user contextforge`.

### Codex

```bash
codex mcp add contextforge -- contextforge-mcp
codex mcp get contextforge
```

Remove it with `codex mcp remove contextforge`.

## Tools

The MCP tool protocol version is `1.0`.

| Tool | Purpose |
| --- | --- |
| `contextforge_index` | Build or refresh the incremental repository index. |
| `contextforge_repository_brief` | Return architecture, facts, inferences, and evidence-backed claims. |
| `contextforge_search` | Return ranked, line-cited search evidence. |
| `contextforge_symbol` | Find exact symbol definitions. |
| `contextforge_references` | Find indexed references to an exact symbol name. |
| `contextforge_trace` | Trace forward indexed relationships, with depth limited to 1–10. |
| `contextforge_investigate` | Produce an `investigate`, `impact`, `change`, or `debug` report. |

All results use the stable ContextForge `1.0` JSON envelope. Compatible releases may add optional tools, parameters, or response fields without removing or reinterpreting this tool set.

## Security and resource boundary

- The server exposes no file-write, shell, Git mutation, network, credential, or plugin-management tool.
- Third-party ContextForge plugins are disabled by default because they execute in-process. Set `CONTEXTFORGE_DISABLE_PLUGINS=0` only when every installed plugin is trusted.
- Every call requires an existing absolute repository directory. A repository argument that is itself a symbolic link is rejected.
- ContextForge's fail-closed source scanner continues to reject unsafe source traversal.
- The process inherits the operating-system permissions of its MCP client; MCP does not provide sandboxing by itself.
- Set `CONTEXTFORGE_MCP_ROOT` to an absolute directory to restrict repository arguments to that directory and its descendants.
- Indexing consumes CPU and writes ContextForge's local cache. Clients should avoid repeatedly indexing unchanged repositories.

## Development checkout

Use an absolute checkout path because clients may launch commands from another directory:

```bash
claude mcp add --transport stdio --scope local contextforge -- uv --directory /absolute/path/to/ContextForge run contextforge-mcp
codex mcp add contextforge -- uv --directory /absolute/path/to/ContextForge run contextforge-mcp
```
