from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from contextforge.mcp_server import (
    MCP_PROTOCOL_VERSION,
    contextforge_index,
    contextforge_investigate,
    contextforge_references,
    contextforge_repository_brief,
    contextforge_search,
    contextforge_symbol,
    contextforge_trace,
    resolve_repository,
    server,
    tool_names,
)


@pytest.fixture
def sample_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(
        "CONTEXTFORGE_CACHE_DIR",
        str(tmp_path.parent / f".contextforge-mcp-cache-{tmp_path.name}"),
    )
    (tmp_path / "app.py").write_text(
        "from services import create_user\n\n"
        "def register(email):\n"
        "    return create_user(email)\n"
    )
    (tmp_path / "services.py").write_text(
        "def create_user(email):\n"
        "    return save_user(email)\n\n"
        "def save_user(email):\n"
        "    return {'email': email}\n"
    )
    return tmp_path


def assert_envelope(payload: dict, command: str, repository: Path) -> None:
    assert payload["schema_version"] == "1.0"
    assert payload["command"] == command
    assert payload["repository"] == str(repository.resolve())


def test_mcp_exposes_a_bounded_read_only_tool_surface() -> None:
    assert MCP_PROTOCOL_VERSION == "1.0"
    assert tool_names() == (
        "contextforge_index",
        "contextforge_investigate",
        "contextforge_references",
        "contextforge_repository_brief",
        "contextforge_search",
        "contextforge_symbol",
        "contextforge_trace",
    )


def test_mcp_tools_publish_read_only_annotations() -> None:
    tools = asyncio.run(server.list_tools())
    assert tools
    for tool in tools:
        assert tool.annotations is not None
        annotations = tool.annotations.model_dump(by_alias=True)
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
        assert annotations["idempotentHint"] is True
        assert annotations["openWorldHint"] is False


def test_mcp_disables_third_party_plugins_by_default() -> None:
    assert os.environ["CONTEXTFORGE_DISABLE_PLUGINS"] == "1"


def test_repository_path_must_be_an_existing_absolute_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        resolve_repository("relative/repository")
    with pytest.raises(ValueError, match="does not exist"):
        resolve_repository(str(tmp_path / "missing"))
    file_path = tmp_path / "file.py"
    file_path.write_text("pass\n")
    with pytest.raises(ValueError, match="directory"):
        resolve_repository(str(file_path))


def test_repository_symlink_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    link = tmp_path / "repository-link"
    link.symlink_to(repository, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        resolve_repository(str(link))


def test_index_and_repository_brief_return_stable_envelopes(sample_repo: Path) -> None:
    indexed = contextforge_index(str(sample_repo))
    brief = contextforge_repository_brief(str(sample_repo))

    assert_envelope(indexed, "index", sample_repo)
    assert indexed["total_files"] == 2
    assert_envelope(brief, "brief", sample_repo)
    assert "claims" in brief
    assert brief["symbols"]["total"] >= 3


def test_search_symbol_references_and_trace_return_structured_evidence(
    sample_repo: Path,
) -> None:
    search = contextforge_search(str(sample_repo), "create_user")
    symbol = contextforge_symbol(str(sample_repo), "create_user")
    references = contextforge_references(str(sample_repo), "create_user")
    trace = contextforge_trace(str(sample_repo), "register")

    assert_envelope(search, "search", sample_repo)
    assert search["results"]
    assert_envelope(symbol, "symbol", sample_repo)
    assert symbol["definitions"][0]["name"] == "create_user"
    assert_envelope(references, "refs", sample_repo)
    assert references["references"]
    assert_envelope(trace, "trace", sample_repo)
    assert trace["nodes"]


def test_investigate_rejects_unknown_modes_before_execution(sample_repo: Path) -> None:
    with pytest.raises(ValueError, match="mode"):
        contextforge_investigate(str(sample_repo), "How are users created?", "delete")


def test_investigate_returns_the_existing_task_report_contract(sample_repo: Path) -> None:
    payload = contextforge_investigate(
        str(sample_repo), "How are users created?", "investigate"
    )

    assert_envelope(payload, "investigate", sample_repo)
    assert payload["query"] == "How are users created?"
    json.dumps(payload)
