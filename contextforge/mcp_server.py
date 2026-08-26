from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Callable, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

# MCP clients grant this server repository read access, not authority to execute
# third-party analyzer code installed in the same environment. Users may opt in
# explicitly when every installed ContextForge plugin is trusted.
os.environ.setdefault("CONTEXTFORGE_DISABLE_PLUGINS", "1")

from .architecture import architecture_buckets
from .compatibility import MCP_TOOL_PROTOCOL_VERSION
from .detector import detect_project
from .intelligence import (
    build_index,
    envelope,
    find_references,
    find_symbols,
    repository_brief,
    task_report,
    trace_symbol,
)
from .search import structured_search

MCP_PROTOCOL_VERSION = MCP_TOOL_PROTOCOL_VERSION
_MAX_QUERY_LENGTH = 4_096
_MAX_NAME_LENGTH = 512
_TASK_MODES = frozenset({"investigate", "impact", "change", "debug"})
Query = Annotated[str, Field(min_length=1, max_length=_MAX_QUERY_LENGTH)]
SymbolName = Annotated[str, Field(min_length=1, max_length=_MAX_NAME_LENGTH)]
TraceDepth = Annotated[int, Field(ge=1, le=10)]
TaskMode = Literal["investigate", "impact", "change", "debug"]

server = FastMCP(
    "ContextForge",
    instructions=(
        "Read-only repository intelligence for coding agents. Pass the absolute path "
        "of the repository to every tool. Prefer contextforge_repository_brief for "
        "orientation, contextforge_search for discovery, and contextforge_investigate "
        "before proposing a change. Results use the ContextForge 1.0 JSON envelope."
    ),
)


def _text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} must not exceed {maximum} characters")
    return normalized


def resolve_repository(repository: str) -> Path:
    raw = Path(_text(repository, "repository", 32_768)).expanduser()
    if not raw.is_absolute():
        raise ValueError("repository must be an absolute path")
    if raw.is_symlink():
        raise ValueError("repository must not be a symbolic link")
    if not raw.exists():
        raise ValueError("repository does not exist")
    if not raw.is_dir():
        raise ValueError("repository must be a directory")

    resolved = raw.resolve(strict=True)
    allowed_root = os.environ.get("CONTEXTFORGE_MCP_ROOT")
    if allowed_root:
        root = Path(allowed_root).expanduser()
        if not root.is_absolute():
            raise ValueError("CONTEXTFORGE_MCP_ROOT must be an absolute path")
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("CONTEXTFORGE_MCP_ROOT must be a directory")
        if resolved != root and root not in resolved.parents:
            raise ValueError("repository is outside CONTEXTFORGE_MCP_ROOT")
    return resolved


def _tool(function: Callable[..., dict]) -> Callable[..., dict]:
    server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )(function)
    return function


@_tool
def contextforge_index(repository: str) -> dict:
    """Build or update the repository index and return index statistics."""
    repo = resolve_repository(repository)
    return envelope("index", repo, **build_index(repo))


@_tool
def contextforge_repository_brief(repository: str) -> dict:
    """Return a concise architecture and evidence brief for a repository."""
    repo = resolve_repository(repository)
    architecture = {
        category: [str(item) for item in items]
        for category, items in architecture_buckets(repo).items()
    }
    return envelope(
        "brief",
        repo,
        **repository_brief(repo, detect_project(repo), architecture),
    )


@_tool
def contextforge_search(repository: str, query: Query) -> dict:
    """Search repository source and return ranked, line-cited evidence."""
    repo = resolve_repository(repository)
    normalized_query = _text(query, "query", _MAX_QUERY_LENGTH)
    return envelope(
        "search",
        repo,
        query=normalized_query,
        results=structured_search(repo, normalized_query),
    )


@_tool
def contextforge_symbol(repository: str, name: SymbolName) -> dict:
    """Find exact symbol definitions by name."""
    repo = resolve_repository(repository)
    normalized_name = _text(name, "name", _MAX_NAME_LENGTH)
    return envelope(
        "symbol",
        repo,
        query=normalized_name,
        definitions=find_symbols(repo, normalized_name),
    )


@_tool
def contextforge_references(repository: str, name: SymbolName) -> dict:
    """Find indexed references to an exact symbol name."""
    repo = resolve_repository(repository)
    normalized_name = _text(name, "name", _MAX_NAME_LENGTH)
    return envelope(
        "refs",
        repo,
        query=normalized_name,
        references=find_references(repo, normalized_name),
    )


@_tool
def contextforge_trace(
    repository: str,
    name: SymbolName,
    depth: TraceDepth = 3,
) -> dict:
    """Trace indexed relationships forward from a symbol."""
    repo = resolve_repository(repository)
    normalized_name = _text(name, "name", _MAX_NAME_LENGTH)
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 10:
        raise ValueError("depth must be an integer between 1 and 10")
    nodes, edges = trace_symbol(repo, normalized_name, depth)
    return envelope(
        "trace",
        repo,
        query=normalized_name,
        depth=depth,
        direction="forward",
        nodes=nodes,
        edges=edges,
    )


@_tool
def contextforge_investigate(
    repository: str,
    query: Query,
    mode: TaskMode = "investigate",
) -> dict:
    """Build an evidence-backed investigation, impact, change, or debug report."""
    if mode not in _TASK_MODES:
        raise ValueError(
            "mode must be one of: " + ", ".join(sorted(_TASK_MODES))
        )
    repo = resolve_repository(repository)
    normalized_query = _text(query, "query", _MAX_QUERY_LENGTH)
    report = task_report(repo, normalized_query, mode)
    return envelope(
        mode,
        repo,
        **report,
    )


def tool_names() -> tuple[str, ...]:
    tools: tuple[Callable[..., dict], ...] = (
        contextforge_index,
        contextforge_investigate,
        contextforge_references,
        contextforge_repository_brief,
        contextforge_search,
        contextforge_symbol,
        contextforge_trace,
    )
    return tuple(tool.__name__ for tool in tools)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
