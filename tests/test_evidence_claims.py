import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from contextforge.claims import claim


@pytest.fixture
def evidence_repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / ".cache"))
    (tmp_path / "app.py").write_text(
        "from service import create_user\n\n"
        "def register(email):\n"
        "    return create_user(email)\n",
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text(
        "def create_user(email):\n"
        "    return {'email': email}\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'evidence-demo'\n"
        "[project.scripts]\ndemo = 'app:register'\n",
        encoding="utf-8",
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


def assert_standard_claim(item: dict) -> None:
    assert set(item) == {
        "claim",
        "status",
        "confidence",
        "supporting_evidence",
        "conflicting_evidence",
        "unresolved_uncertainty",
    }
    assert item["status"] in {"observed", "inferred"}
    assert item["confidence"] in {"high", "medium", "low", "unknown"}
    assert isinstance(item["supporting_evidence"], list)
    assert isinstance(item["conflicting_evidence"], list)
    assert isinstance(item["unresolved_uncertainty"], list)


def test_claim_without_supporting_evidence_cannot_assert_confidence():
    result = claim(
        "Runtime dispatch reaches the handler.",
        status="inferred",
        confidence="high",
        unresolved_uncertainty=["Runtime wiring was not observed."],
    )

    assert result["confidence"] == "unknown"
    assert result["supporting_evidence"] == []
    assert result["unresolved_uncertainty"] == ["Runtime wiring was not observed."]


def test_claim_rejects_invalid_status_and_confidence():
    with pytest.raises(ValueError, match="status"):
        claim("Invalid status", status="assumed", confidence="low")
    with pytest.raises(ValueError, match="confidence"):
        claim("Invalid confidence", status="observed", confidence="certain")


def test_report_without_matching_evidence_emits_unknown_claim(evidence_repo: Path):
    result = run_cf(evidence_repo, "investigate", "nonexistent runtime behavior", "--format=json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)["report"]
    assert report["facts"] == []
    assert report["claims"]
    item = report["claims"][0]
    assert item["status"] == "inferred"
    assert item["confidence"] == "unknown"
    assert item["supporting_evidence"] == []
    assert item["unresolved_uncertainty"] == report["unresolved_questions"]


def test_inferred_claims_include_report_specific_uncertainty(evidence_repo: Path):
    result = run_cf(evidence_repo, "investigate", "create user", "--format=json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)["report"]
    inferred = [item for item in report["claims"] if item["status"] == "inferred"]
    assert inferred
    for item in inferred:
        assert set(report["unresolved_questions"]).issubset(item["unresolved_uncertainty"])


@pytest.mark.parametrize("command", ["investigate", "impact", "change", "debug"])
def test_task_reports_emit_standardized_claims(evidence_repo: Path, command: str):
    result = run_cf(evidence_repo, command, "create user", "--format=json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)["report"]
    assert report["claims"]
    for item in report["claims"]:
        assert_standard_claim(item)
    assert {item["status"] for item in report["claims"]} == {
        "observed",
        "inferred",
    }
    assert all(
        item["supporting_evidence"]
        for item in report["claims"]
        if item["confidence"] != "unknown"
    )


def test_repository_brief_emits_standardized_claims(evidence_repo: Path):
    result = run_cf(evidence_repo, "brief", "--format=json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["claims"]
    for item in payload["claims"]:
        assert_standard_claim(item)
    assert any(
        item["status"] == "observed"
        and item["claim"] == "Python is detected as a repository technology."
        for item in payload["claims"]
    )


@pytest.mark.parametrize(
    "arguments",
    [("brief",), ("investigate", "create user")],
)
def test_terminal_reports_expose_standardized_claims(
    evidence_repo: Path, arguments: tuple[str, ...]
):
    result = run_cf(evidence_repo, *arguments)

    assert result.returncode == 0, result.stderr
    assert "Claims" in result.stdout
    assert "status=" in result.stdout
    assert "confidence=" in result.stdout
    assert "support=" in result.stdout
    assert "conflicts=" in result.stdout
    assert "uncertainty=" in result.stdout


def test_terminal_claim_support_includes_line_numbers(evidence_repo: Path):
    result = run_cf(evidence_repo, "investigate", "create user")

    assert result.returncode == 0, result.stderr
    assert "support=app.py:1" in result.stdout


def test_documented_schema_rejects_malformed_evidence_objects():
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    schema_path = Path(__file__).parents[1] / "docs" / "json-schema-1.0.json"
    schema = json.loads(schema_path.read_text())
    malformed = {
        "schema_version": "1.0",
        "command": "brief",
        "repository": ".",
        "claims": [
            {
                "claim": "Malformed evidence must not validate.",
                "status": "observed",
                "confidence": "high",
                "supporting_evidence": [{"line": 1}],
                "conflicting_evidence": [42],
                "unresolved_uncertainty": [],
            }
        ],
    }

    assert list(Draft202012Validator(schema).iter_errors(malformed))


def test_documented_schema_rejects_malformed_nested_report_claims():
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    schema_path = Path(__file__).parents[1] / "docs" / "json-schema-1.0.json"
    schema = json.loads(schema_path.read_text())
    malformed = {
        "schema_version": "1.0",
        "command": "investigate",
        "repository": ".",
        "report": {"claims": [{"garbage": 1}]},
    }

    assert list(Draft202012Validator(schema).iter_errors(malformed))
