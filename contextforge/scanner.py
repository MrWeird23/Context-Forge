import os
import subprocess
from pathlib import Path
from typing import Iterator

IGNORE_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".next",
    ".angular",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".env",
    ".headroom-venv",
    "pb_data",
    "pb_public",
    ".idea",
    ".vscode",
    ".cache",
    ".contextforge",
    ".pytest_cache",
    "tmp",
    "logs",
}

CODE_EXTENSIONS = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".py",
    ".go",
    ".rs",
    ".php",
    ".java",
    ".cs",
    ".html",
    ".css",
    ".scss",
    ".yml",
    ".yaml",
    ".md",
    ".sql",
}


def repo_root() -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return Path.cwd().resolve()

    return Path(result.stdout.strip()).resolve()


def iter_code_files(repo: Path) -> Iterator[Path]:
    resolved_repo = repo.resolve()
    for current_root, dirs, filenames in os.walk(repo):
        root_path = Path(current_root)
        dirs[:] = [
            d for d in dirs
            if d not in IGNORE_DIRS and not (root_path / d).is_symlink()
        ]

        for filename in filenames:
            path = Path(current_root) / filename

            if path.is_symlink():
                continue
            try:
                path.resolve().relative_to(resolved_repo)
            except ValueError:
                continue

            if path.suffix in CODE_EXTENSIONS and path.is_file():
                yield path
