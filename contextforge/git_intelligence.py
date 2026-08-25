from __future__ import annotations

import itertools
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from .intelligence import ContextForgeError


_COMMIT_MARKER = "__CONTEXTFORGE_COMMIT__"
_BUG_FIX_PREFIXES = ("fix", "bugfix", "hotfix", "revert")


def _git(repo: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ContextForgeError("git_unavailable", "Git is required for history analysis") from exc
    if result.returncode:
        raise ContextForgeError(
            "git_command_failed",
            result.stderr.strip() or "Git history analysis failed",
            arguments=list(arguments),
        )
    return result.stdout


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ContextForgeError("git_unavailable", "Git is required for history analysis") from exc
    if result.returncode:
        raise ContextForgeError(
            "git_command_failed",
            result.stderr.decode(errors="replace").strip() or "Git history analysis failed",
            arguments=list(arguments),
        )
    return result.stdout


def _require_repository(repo: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ContextForgeError("git_unavailable", "Git is required for history analysis") from exc
    if result.returncode or result.stdout.strip() != "true":
        raise ContextForgeError(
            "git_repository_required",
            "This command must run inside a Git repository",
        )


def _is_bug_fix(subject: str) -> bool:
    normalized = subject.strip().lower()
    return normalized.startswith(_BUG_FIX_PREFIXES) or any(
        token in normalized for token in (" bug ", " defect ", " regression ")
    )


def _canonical(path: str, aliases: dict[str, str]) -> str:
    seen = set()
    while path in aliases and path not in seen:
        seen.add(path)
        path = aliases[path]
    return path


def _commits(repo: Path) -> list[dict]:
    _require_repository(repo)
    output = _git_bytes(
        repo,
        "log",
        "-z",
        "--date=iso-strict",
        "--format=" + _COMMIT_MARKER + "%x00%H%x00%aI%x00%aN%x00%s%x00",
        "--name-status",
        "-M",
        "--no-color",
    )
    raw_commits = []
    current = None
    tokens = output.split(b"\0")
    index = 0
    while index < len(tokens):
        token = tokens[index].lstrip(b"\n")
        if token == _COMMIT_MARKER.encode() and index + 4 < len(tokens):
            commit_hash, authored_at, author, subject = (
                value.decode("utf-8", errors="surrogateescape")
                for value in tokens[index + 1 : index + 5]
            )
            current = {
                "hash": commit_hash,
                "authored_at": authored_at,
                "author": author,
                "subject": subject,
                "changes": [],
            }
            raw_commits.append(current)
            index += 5
            continue
        if not token or current is None:
            index += 1
            continue
        status = token.decode("ascii", errors="replace")
        if status.startswith(("R", "C")) and index + 2 < len(tokens):
            first_path = tokens[index + 1].decode("utf-8", errors="surrogateescape")
            second_path = tokens[index + 2].decode("utf-8", errors="surrogateescape")
            current["changes"].append((status[0], first_path, second_path))
            index += 3
        elif index + 1 < len(tokens):
            path = tokens[index + 1].decode("utf-8", errors="surrogateescape")
            current["changes"].append((status[0], path, None))
            index += 2
        else:
            index += 1

    aliases: dict[str, str] = {}
    commits = []
    for raw in raw_commits:
        paths = set()
        renames = []
        for status, first_path, second_path in raw["changes"]:
            if status == "R" and second_path is not None:
                new_path = _canonical(second_path, aliases)
                old_path = _canonical(first_path, aliases)
                aliases[first_path] = new_path
                aliases[old_path] = new_path
                paths.add(new_path)
                renames.append({"old_path": first_path, "new_path": second_path})
            else:
                paths.add(_canonical(first_path, aliases))
        commits.append(
            {
                **{key: raw[key] for key in ("hash", "author", "subject")},
                "authored_at": datetime.fromisoformat(raw["authored_at"]).isoformat(),
                "is_bug_fix": _is_bug_fix(raw["subject"]),
                "paths": sorted(paths),
                "renames": renames,
                "rename_only": bool(renames) and len(renames) == len(raw["changes"]),
            }
        )
    return commits


def hotspots(repo: Path, limit: int = 20) -> list[dict]:
    statistics: dict[str, dict] = {}
    for commit in _commits(repo):
        for path in commit["paths"]:
            item = statistics.setdefault(
                path,
                {
                    "path": path,
                    "commits": 0,
                    "authors": set(),
                    "bug_fix_commits": 0,
                    "last_changed": commit["authored_at"],
                },
            )
            item["commits"] += 1
            item["authors"].add(commit["author"])
            item["bug_fix_commits"] += int(commit["is_bug_fix"])

    maximum = max((item["commits"] for item in statistics.values()), default=0)
    results = []
    for item in statistics.values():
        if maximum and item["commits"] >= max(3, maximum * 0.75):
            activity = "volatile"
        elif item["commits"] <= 1:
            activity = "stable"
        else:
            activity = "moderate"
        results.append({**item, "authors": len(item["authors"]), "activity": activity})
    results.sort(key=lambda item: (-item["commits"], item["path"]))
    return results[:limit]


def coupling(repo: Path, minimum_commits: int = 2, limit: int = 20) -> list[dict]:
    file_commits = Counter()
    pair_commits = Counter()
    for commit in _commits(repo):
        if commit["rename_only"]:
            continue
        paths = commit["paths"]
        file_commits.update(paths)
        pair_commits.update(itertools.combinations(paths, 2))

    results = []
    for (left, right), commits in pair_commits.items():
        if commits < minimum_commits:
            continue
        results.append(
            {
                "paths": [left, right],
                "commits": commits,
                "left_commits": file_commits[left],
                "right_commits": file_commits[right],
                "strength": commits / max(file_commits[left], file_commits[right]),
            }
        )
    results.sort(key=lambda item: (-item["strength"], -item["commits"], item["paths"]))
    return results[:limit]


def owners(repo: Path, path: str) -> dict:
    requested = path.replace("\\", "/").removeprefix("./")
    is_repository = requested in ("", ".")
    prefix = requested.rstrip("/") + "/"
    matching = [
        commit
        for commit in _commits(repo)
        if is_repository
        or requested in commit["paths"]
        or any(candidate.startswith(prefix) for candidate in commit["paths"])
    ]
    contributors = Counter(commit["author"] for commit in matching)
    total = len(matching)
    ranked = [
        {"name": name, "commits": commits, "share": commits / total}
        for name, commits in contributors.items()
    ]
    ranked.sort(key=lambda item: (-item["commits"], item["name"]))
    return {"path": requested, "commits": total, "contributors": ranked}


def history(repo: Path, query: str, limit: int = 50) -> dict:
    normalized = query.casefold()
    commits = _commits(repo)
    matching = [
        commit
        for commit in commits
        if normalized in commit["subject"].casefold()
        or any(normalized in path.casefold() for path in commit["paths"])
        or any(
            normalized in rename["old_path"].casefold()
            or normalized in rename["new_path"].casefold()
            for rename in commit["renames"]
        )
    ]
    rename_counts = Counter(
        (rename["old_path"], rename["new_path"])
        for commit in matching
        for rename in commit["renames"]
    )
    renames = [
        {"old_path": old, "new_path": new, "commits": count}
        for (old, new), count in sorted(rename_counts.items())
    ]
    public_commits = [
        {key: commit[key] for key in ("hash", "authored_at", "author", "subject", "is_bug_fix", "paths")}
        for commit in matching[:limit]
    ]
    matched_paths = {
        path
        for commit in matching
        for path in commit["paths"]
        if normalized in path.casefold()
    }
    return {
        "query": query,
        "commits": public_commits,
        "renames": renames,
        "summary": {
            "matched_commits": len(matching),
            "bug_fix_commits": sum(commit["is_bug_fix"] for commit in matching),
            "authors": len({commit["author"] for commit in matching}),
            "paths": len(matched_paths),
        },
    }
