from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .ranking import classify_file
from .scanner import iter_code_files, read_file

SCHEMA_VERSION = "1.0"
CACHE_DIR = ".contextforge"
INDEX_NAME = "index.sqlite"


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    path: str
    line: int
    end_line: int
    parent: str | None = None
    signature: str | None = None


@dataclass(frozen=True)
class Reference:
    name: str
    path: str
    line: int
    context: str
    caller: str | None = None


def envelope(command: str, repo: Path, **payload):
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "repository": str(repo),
        **payload,
    }


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            value.update(chunk)
    return value.hexdigest()


def _connect(repo: Path) -> sqlite3.Connection:
    cache = repo / CACHE_DIR
    cache.mkdir(exist_ok=True)
    connection = sqlite3.connect(cache / INDEX_NAME)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            digest TEXT NOT NULL,
            category TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS symbols (
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            parent TEXT,
            signature TEXT
        );
        CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
        CREATE TABLE IF NOT EXISTS refs (
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            line INTEGER NOT NULL,
            context TEXT NOT NULL,
            caller TEXT
        );
        CREATE INDEX IF NOT EXISTS refs_name ON refs(name);
        """
    )
    return connection


def _source_line(lines: list[str], line: int) -> str:
    return lines[line - 1].strip()[:240] if 0 < line <= len(lines) else ""


def _python_entities(path: str, content: str) -> tuple[list[Symbol], list[Reference]]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return [], []
    lines = content.splitlines()
    symbols: list[Symbol] = []
    references: list[Reference] = []
    scope: list[str] = []

    class Visitor(ast.NodeVisitor):
        def _signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
            try:
                return ast.unparse(node.args)
            except Exception:
                return ""

        def visit_ClassDef(self, node: ast.ClassDef):
            symbols.append(Symbol(node.name, "class", path, node.lineno, getattr(node, "end_lineno", node.lineno), scope[-1] if scope else None))
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef):
            kind = "method" if scope else "function"
            symbols.append(Symbol(node.name, kind, path, node.lineno, getattr(node, "end_lineno", node.lineno), scope[-1] if scope else None, self._signature(node)))
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call):
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                name = None
            if name:
                references.append(Reference(name, path, node.lineno, _source_line(lines, node.lineno), scope[-1] if scope else None))
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom):
            for alias in node.names:
                references.append(Reference(alias.name, path, node.lineno, _source_line(lines, node.lineno), scope[-1] if scope else None))

    Visitor().visit(tree)
    return symbols, references


def _generic_entities(path: str, content: str) -> tuple[list[Symbol], list[Reference]]:
    symbols: list[Symbol] = []
    references: list[Reference] = []
    lines = content.splitlines()
    definition_patterns = [
        ("class", re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"\b(?:function|def|fn)\s+([A-Za-z_$][\w$]*)\s*\(")),
        ("function", re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>")),
    ]
    call_pattern = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
    keywords = {"if", "for", "while", "switch", "catch", "return", "function", "def", "class"}
    for number, line in enumerate(lines, 1):
        defined: set[str] = set()
        for kind, pattern in definition_patterns:
            match = pattern.search(line)
            if match:
                name = match.group(1)
                defined.add(name)
                symbols.append(Symbol(name, kind, path, number, number, signature=line.strip()[:240]))
        for match in call_pattern.finditer(line):
            name = match.group(1)
            if name not in keywords and name not in defined:
                references.append(Reference(name, path, number, line.strip()[:240]))
    return symbols, references


def extract_entities(path: str, content: str) -> tuple[list[Symbol], list[Reference]]:
    if Path(path).suffix == ".py":
        return _python_entities(path, content)
    return _generic_entities(path, content)


def build_index(repo: Path) -> dict:
    connection = _connect(repo)
    files = {str(path.relative_to(repo)): path for path in iter_code_files(repo)}
    existing = {row["path"]: row["digest"] for row in connection.execute("SELECT path, digest FROM files")}
    indexed = unchanged = removed = 0
    try:
        for relative, path in files.items():
            digest = _digest(path)
            if existing.get(relative) == digest:
                unchanged += 1
                continue
            content = read_file(path)
            symbols, references = extract_entities(relative, content)
            connection.execute("DELETE FROM symbols WHERE path = ?", (relative,))
            connection.execute("DELETE FROM refs WHERE path = ?", (relative,))
            connection.execute(
                "INSERT OR REPLACE INTO files(path, digest, category) VALUES (?, ?, ?)",
                (relative, digest, classify_file(Path(relative))),
            )
            connection.executemany(
                "INSERT INTO symbols(name, kind, path, line, end_line, parent, signature) VALUES (:name, :kind, :path, :line, :end_line, :parent, :signature)",
                [asdict(item) for item in symbols],
            )
            connection.executemany(
                "INSERT INTO refs(name, path, line, context, caller) VALUES (:name, :path, :line, :context, :caller)",
                [asdict(item) for item in references],
            )
            indexed += 1
        stale = set(existing) - set(files)
        for relative in stale:
            connection.execute("DELETE FROM files WHERE path = ?", (relative,))
            connection.execute("DELETE FROM symbols WHERE path = ?", (relative,))
            connection.execute("DELETE FROM refs WHERE path = ?", (relative,))
            removed += 1
        connection.commit()
        symbol_count = connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        reference_count = connection.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
    finally:
        connection.close()
    return {"indexed": indexed, "unchanged": unchanged, "removed": removed, "total_files": len(files), "symbols": symbol_count, "references": reference_count}


def find_symbols(repo: Path, name: str) -> list[dict]:
    build_index(repo)
    connection = _connect(repo)
    try:
        rows = connection.execute(
            "SELECT name, kind, path, line, end_line, parent, signature FROM symbols WHERE lower(name) = lower(?) ORDER BY path, line",
            (name,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def find_references(repo: Path, name: str) -> list[dict]:
    build_index(repo)
    connection = _connect(repo)
    try:
        rows = connection.execute(
            "SELECT name, path, line, context, caller FROM refs WHERE lower(name) = lower(?) ORDER BY path, line",
            (name,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def trace_symbol(repo: Path, name: str, max_depth: int = 6) -> tuple[list[dict], list[dict]]:
    build_index(repo)
    connection = _connect(repo)
    nodes: dict[tuple[str, str, int], dict] = {}
    edges: list[dict] = []
    queue = deque([(name, 0)])
    visited: set[str] = set()
    try:
        while queue:
            current, depth = queue.popleft()
            if current.lower() in visited or depth > max_depth:
                continue
            visited.add(current.lower())
            definitions = connection.execute(
                "SELECT name, kind, path, line, end_line, parent, signature FROM symbols WHERE lower(name) = lower(?)",
                (current,),
            ).fetchall()
            for row in definitions:
                node = dict(row)
                nodes[(node["name"], node["path"], node["line"])] = node
                calls = connection.execute(
                    "SELECT DISTINCT r.name FROM refs r JOIN symbols s ON r.path=s.path AND r.caller=s.name WHERE s.path=? AND s.name=? AND r.line BETWEEN s.line AND s.end_line",
                    (node["path"], node["name"]),
                ).fetchall()
                for call in calls:
                    target = call["name"]
                    if connection.execute("SELECT 1 FROM symbols WHERE lower(name)=lower(?) LIMIT 1", (target,)).fetchone():
                        edges.append({"from": node["name"], "to": target, "path": node["path"]})
                        queue.append((target, depth + 1))
    finally:
        connection.close()
    return list(nodes.values()), edges


def read_pyproject(repo: Path) -> dict:
    path = repo / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        import tomllib

        return tomllib.loads(path.read_text(errors="ignore"))
    except Exception:
        return {}


def repository_brief(repo: Path, detections: Iterable[str], architecture: dict[str, list[str]]) -> dict:
    stats = build_index(repo)
    connection = _connect(repo)
    project = read_pyproject(repo).get("project", {})
    scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
    entry_points = []
    for command, target in scripts.items():
        module, _, symbol = str(target).partition(":")
        entry_points.append({"command": command, "target": target, "module": module, "symbol": symbol})
    try:
        largest = connection.execute(
            "SELECT path, COUNT(*) AS count FROM symbols GROUP BY path ORDER BY count DESC, path LIMIT 10"
        ).fetchall()
        evidence = [{"claim": "symbol density", "path": row["path"], "symbol_count": row["count"]} for row in largest]
    finally:
        connection.close()
    return {
        "name": project.get("name") if isinstance(project, dict) else None,
        "description": project.get("description") if isinstance(project, dict) else None,
        "detections": list(detections),
        "entry_points": entry_points,
        "architecture": architecture,
        "symbols": {"total": stats["symbols"], "references": stats["references"]},
        "evidence": evidence,
        "confidence": "high" if stats["symbols"] else "low",
    }


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)
