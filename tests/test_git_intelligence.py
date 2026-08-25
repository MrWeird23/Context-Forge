import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from contextforge.git_intelligence import coupling, history, hotspots, owners


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **(env or {})},
    )
    return result.stdout.strip()


def commit(repo: Path, message: str, when: str, author: str = "Ada") -> None:
    env = {
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": f"{author.lower()}@example.com",
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": f"{author.lower()}@example.com",
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
    }
    git(repo, "add", ".")
    git(repo, "commit", "-m", message, env=env)


@pytest.fixture
def history_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Test User")
    git(tmp_path, "config", "user.email", "test@example.com")

    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "config.py").write_text("ENABLED = True\n", encoding="utf-8")
    commit(tmp_path, "feat: add application", "2025-01-01T12:00:00+00:00", "Ada")

    (tmp_path / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    (tmp_path / "config.py").write_text("ENABLED = False\n", encoding="utf-8")
    commit(tmp_path, "fix: correct application", "2025-01-02T12:00:00+00:00", "Bob")

    (tmp_path / "app.py").write_text("def run():\n    return 3\n", encoding="utf-8")
    commit(tmp_path, "refactor application", "2025-01-03T12:00:00+00:00", "Ada")

    git(tmp_path, "mv", "config.py", "settings.py")
    commit(tmp_path, "rename configuration", "2025-01-04T12:00:00+00:00", "Carol")
    return tmp_path


def run_cf(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "contextforge", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )


def test_hotspots_rank_by_commit_count_with_deterministic_ties(history_repo: Path):
    result = hotspots(history_repo)

    assert [item["path"] for item in result[:2]] == ["app.py", "settings.py"]
    assert result[0]["commits"] == 3
    assert result[0]["authors"] == 2
    assert result[0]["bug_fix_commits"] == 1
    assert result[0]["activity"] == "volatile"
    assert result[0]["last_changed"] == "2025-01-03T12:00:00+00:00"


def test_coupling_uses_commit_level_cochange_and_excludes_renames(history_repo: Path):
    result = coupling(history_repo, minimum_commits=2)

    assert result == [
        {
            "paths": ["app.py", "settings.py"],
            "commits": 2,
            "left_commits": 3,
            "right_commits": 2,
            "strength": pytest.approx(2 / 3),
        }
    ]


def test_owners_resolves_renames_and_has_deterministic_contributors(history_repo: Path):
    result = owners(history_repo, "settings.py")

    assert result["path"] == "settings.py"
    assert result["commits"] == 3
    assert result["contributors"] == [
        {"name": "Ada", "commits": 1, "share": pytest.approx(1 / 3)},
        {"name": "Bob", "commits": 1, "share": pytest.approx(1 / 3)},
        {"name": "Carol", "commits": 1, "share": pytest.approx(1 / 3)},
    ]


def test_owners_accepts_a_directory_prefix(history_repo: Path):
    result = owners(history_repo, ".")

    assert result["commits"] == 4
    assert [item["name"] for item in result["contributors"]] == ["Ada", "Bob", "Carol"]


def test_history_searches_commit_messages_and_reports_bug_fix_and_renames(history_repo: Path):
    message_result = history(history_repo, "correct")
    file_result = history(history_repo, "settings.py")

    assert message_result["commits"][0]["subject"] == "fix: correct application"
    assert message_result["commits"][0]["is_bug_fix"] is True
    assert file_result["renames"] == [
        {"old_path": "config.py", "new_path": "settings.py", "commits": 1}
    ]
    assert file_result["summary"] == {
        "matched_commits": 3,
        "bug_fix_commits": 1,
        "authors": 3,
        "paths": 1,
    }


def test_git_commands_emit_stable_json_envelopes(history_repo: Path):
    commands = [
        ("hotspots", "--format", "json"),
        ("coupling", "--minimum-commits", "2", "--format", "json"),
        ("owners", "settings.py", "--format", "json"),
        ("history", "correct", "--format", "json"),
    ]

    for command in commands:
        result = run_cf(history_repo, *command)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == "1.0"
        assert payload["command"] == command[0]
        assert payload["repository"] == str(history_repo)


def test_git_commands_fail_cleanly_outside_a_repository(tmp_path: Path):
    result = run_cf(tmp_path, "hotspots", "--format", "json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "git_repository_required"


def test_paths_with_tabs_and_newlines_remain_distinct(history_repo: Path):
    tabbed = "tab\tname.py"
    multiline = "line\nbreak.py"
    (history_repo / tabbed).write_text("tab", encoding="utf-8")
    (history_repo / multiline).write_text("line", encoding="utf-8")
    commit(history_repo, "add unusual paths", "2025-01-05T12:00:00+00:00", "Ada")

    result = hotspots(history_repo)

    assert tabbed in [item["path"] for item in result]
    assert multiline in [item["path"] for item in result]
