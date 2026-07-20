import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(
        "from services import create_user\n\n"
        "def register(email):\n"
        "    return create_user(email)\n"
    )
    (tmp_path / "services.py").write_text(
        "class UserService:\n"
        "    def create(self, email):\n"
        "        return save_user(email)\n\n"
        "def create_user(email):\n"
        "    return UserService().create(email)\n\n"
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
        "[project.scripts]\nsample = 'app:register'\n"
    )
    return tmp_path


def run_cf(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "contextforge", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_search_json_is_machine_readable_and_explains_scores(sample_repo: Path):
    result = run_cf(sample_repo, "search", "create_user", "--format", "json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["command"] == "search"
    assert payload["results"][0]["path"] in {"services.py", "test_services.py"}
    assert payload["results"][0]["score_breakdown"]
    assert payload["results"][0]["evidence"]


def test_index_is_incremental_and_excludes_its_own_cache(sample_repo: Path):
    first = run_cf(sample_repo, "index", "--format", "json")
    second = run_cf(sample_repo, "index", "--format", "json")

    assert first.returncode == second.returncode == 0
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["indexed"] == 3
    assert second_payload["indexed"] == 0
    assert second_payload["unchanged"] == first_payload["total_files"]
    assert (sample_repo / ".contextforge" / "index.sqlite").exists()


def test_symbol_and_refs_connect_definitions_to_call_sites(sample_repo: Path):
    symbol_result = run_cf(sample_repo, "symbol", "create_user", "--format", "json")
    refs_result = run_cf(sample_repo, "refs", "create_user", "--format", "json")

    symbol_payload = json.loads(symbol_result.stdout)
    refs_payload = json.loads(refs_result.stdout)
    assert any(item["path"] == "services.py" and item["kind"] == "function" for item in symbol_payload["definitions"])
    assert any(item["path"] == "app.py" for item in refs_payload["references"])
    assert any(item["path"] == "test_services.py" for item in refs_payload["references"])


def test_trace_follows_calls_from_entry_symbol(sample_repo: Path):
    result = run_cf(sample_repo, "trace", "register", "--format", "json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    names = {node["name"] for node in payload["nodes"]}
    assert {"register", "create_user", "create", "save_user"}.issubset(names)
    assert payload["edges"]


def test_brief_reports_entry_points_commands_architecture_and_evidence(sample_repo: Path):
    result = run_cf(sample_repo, "brief", "--format", "json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert "Python: Yes" in payload["detections"]
    assert any(item["symbol"] == "register" for item in payload["entry_points"])
    assert payload["architecture"]
    assert payload["symbols"]["total"] >= 5
    assert payload["evidence"]


def test_index_ignores_source_symlinks_that_escape_repository(sample_repo: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.py"
    outside.write_text("SECRET_TOKEN = 'must-not-be-indexed'\n")
    (sample_repo / "leak.py").symlink_to(outside)

    result = run_cf(sample_repo, "search", "must-not-be-indexed", "--format", "json")

    assert result.returncode == 0
    assert json.loads(result.stdout)["results"] == []


def test_index_rejects_symlinked_cache_directory(sample_repo: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("cache-target")
    (sample_repo / ".contextforge").symlink_to(outside, target_is_directory=True)

    result = run_cf(sample_repo, "index", "--format", "json")

    assert result.returncode != 0
    assert "unsafe cache path" in result.stderr.lower()
    assert not (outside / "index.sqlite").exists()
