import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def investigation_repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / ".cache"))
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
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_services.py").write_text(
        "from services import create_user\n\n"
        "def test_create_user():\n"
        "    assert create_user('a@example.com')\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'sample'\nversion = '1.0.0'\n"
    )
    return tmp_path


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


@pytest.mark.parametrize("command", ["investigate", "impact", "change", "debug"])
def test_task_report_commands_emit_a_stable_evidence_contract(
    investigation_repo: Path, command: str
):
    result = run_cf(
        investigation_repo,
        command,
        "create user",
        "--format",
        "json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["command"] == command
    report = payload["report"]
    assert report["kind"] == command
    assert report["query"] == "create user"
    assert report["summary"]
    assert report["confidence"] in {"high", "medium", "low"}
    assert report["facts"]
    assert all(
        fact["evidence"]["path"] and fact["evidence"]["line"] >= 1
        for fact in report["facts"]
    )
    assert report["inferences"]
    assert all(
        inference["confidence"] in {"high", "medium", "low"}
        and inference["evidence"]
        for inference in report["inferences"]
    )
    assert isinstance(report["unresolved_questions"], list)
    assert report["relevant_paths"]
    assert report["definitions"]
    assert report["references"]
    assert report["tests"]
    assert report["risks"]


def test_investigate_report_connects_definitions_references_and_execution_paths(
    investigation_repo: Path,
):
    result = run_cf(
        investigation_repo,
        "investigate",
        "create user",
        "--format=json",
    )

    report = json.loads(result.stdout)["report"]
    assert any(item["name"] == "create_user" for item in report["definitions"])
    assert any(item["path"] == "app.py" for item in report["references"])
    assert any(path["nodes"] for path in report["execution_paths"])
    assert any(
        node["name"] == "save_user"
        for path in report["execution_paths"]
        for node in path["nodes"]
    )
    assert any(item["path"] == "tests/test_services.py" for item in report["tests"])


def test_each_task_report_adds_mode_specific_guidance(investigation_repo: Path):
    reports = {}
    for command in ("impact", "change", "debug"):
        result = run_cf(
            investigation_repo,
            command,
            "create user",
            "--format=json",
        )
        reports[command] = json.loads(result.stdout)["report"]

    assert reports["impact"]["affected_areas"]
    assert reports["change"]["implementation_patterns"]
    hypotheses = reports["debug"]["hypotheses"]
    assert hypotheses
    assert [item["rank"] for item in hypotheses] == list(
        range(1, len(hypotheses) + 1)
    )
    assert all(
        item["confidence"] in {"high", "medium", "low"} for item in hypotheses
    )


@pytest.mark.parametrize(
    ("command", "section"),
    [
        ("investigate", "Inferences"),
        ("impact", "Affected areas"),
        ("change", "Implementation patterns"),
        ("debug", "Ranked hypotheses"),
    ],
)
def test_text_reports_surface_evidence_and_mode_specific_guidance(
    investigation_repo: Path, command: str, section: str
):
    result = run_cf(investigation_repo, command, "create user")

    assert result.returncode == 0, result.stderr
    assert "Observed facts" in result.stdout
    assert "Execution paths" in result.stdout
    assert section in result.stdout


def test_text_report_escapes_repository_and_query_markup(
    investigation_repo: Path,
):
    marked_path = investigation_repo / "[bold]create_user.py"
    marked_path.write_text("def create_user():\n    return True\n")

    result = run_cf(investigation_repo, "investigate", "[bold]create user[/bold]")

    assert result.returncode == 0, result.stderr
    assert "[bold]create user[/bold]" in result.stdout
    assert "[bold]create_user.py" in result.stdout


def test_task_report_is_deterministic_across_rebuilds(
    investigation_repo: Path,
):
    first = run_cf(investigation_repo, "investigate", "create user", "--format=json")
    assert first.returncode == 0, first.stderr
    cache_directory = investigation_repo / ".cache"
    shutil.rmtree(cache_directory)

    second = run_cf(investigation_repo, "investigate", "create user", "--format=json")

    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["report"] == json.loads(second.stdout)["report"]


def test_task_report_matches_common_inflections(
    investigation_repo: Path,
):
    result = run_cf(
        investigation_repo,
        "debug",
        "Creating users fails after save",
        "--format=json",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)["report"]
    assert report["definitions"]
    assert any(item["name"] == "create_user" for item in report["definitions"])
    assert report["confidence"] != "low"


def test_report_with_no_repository_evidence_stays_explicitly_uncertain(
    investigation_repo: Path,
):
    result = run_cf(
        investigation_repo,
        "debug",
        "quantum aardvark telemetry",
        "--format=json",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)["report"]
    assert report["confidence"] == "low"
    assert report["facts"] == []
    assert report["definitions"] == []
    assert report["references"] == []
    assert report["unresolved_questions"]
    assert report["hypotheses"] == []


def test_partial_query_coverage_cannot_produce_high_confidence(
    investigation_repo: Path,
):
    result = run_cf(
        investigation_repo,
        "investigate",
        "create user quantum",
        "--format=json",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)["report"]
    assert report["facts"]
    assert report["confidence"] != "high"
