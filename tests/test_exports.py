import os
import subprocess
import sys
from pathlib import Path

import pytest


def run_cf(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["CONTEXTFORGE_CACHE_DIR"] = str(repo / ".cache")
    return subprocess.run(
        [sys.executable, "-m", "contextforge", *arguments],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def export_repo(tmp_path: Path) -> Path:
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
    (tmp_path / "test_services.py").write_text(
        "from services import create_user\n\n"
        "def test_create_user():\n"
        "    assert create_user('a@example.com')\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'sample'\nversion = '1.0.0'\n"
    )
    return tmp_path


def test_brief_exports_deterministic_markdown(export_repo: Path):
    first = run_cf(export_repo, "brief", "--format", "markdown")
    second = run_cf(export_repo, "brief", "--format", "markdown")

    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert first.stdout.startswith("# ContextForge Brief\n")
    assert "## Repository" in first.stdout
    assert "## Architecture" in first.stdout
    assert "`app.py`" in first.stdout


def test_trace_exports_mermaid_flowchart(export_repo: Path):
    result = run_cf(export_repo, "trace", "register", "--format", "mermaid")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("flowchart TD\n")
    assert '"register"' in result.stdout
    assert '"create_user"' in result.stdout
    assert "-->" in result.stdout


def test_trace_exports_graphviz_dot(export_repo: Path):
    result = run_cf(export_repo, "trace", "register", "--format", "graphviz")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("digraph contextforge {\n")
    assert 'label="register"' in result.stdout
    assert 'label="create_user"' in result.stdout
    assert "->" in result.stdout
    assert result.stdout.endswith("}\n")


@pytest.mark.parametrize("format_name", ["mermaid", "graphviz"])
def test_graph_formats_are_rejected_for_non_graph_commands(
    export_repo: Path, format_name: str
):
    result = run_cf(export_repo, "brief", "--format", format_name)

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_markdown_escapes_untrusted_repository_content(export_repo: Path):
    (export_repo / "unsafe|name.py").write_text("def unsafe():\n    return 1\n")

    result = run_cf(export_repo, "brief", "--format", "markdown")

    assert result.returncode == 0, result.stderr
    assert "`unsafe|name.py`" in result.stdout
