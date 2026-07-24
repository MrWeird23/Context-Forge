from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from collections import defaultdict, deque
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .analyzers import (
    Relationship,
    Reference,
    Repository,
    SourceFile,
    Symbol,
    analyzer_for_path,
    analyzer_registry,
)
from .ranking import classify_file
from .scanner import iter_code_files

SCHEMA_VERSION = "1.0"
INDEX_NAME = "index.sqlite"
INDEX_SCHEMA_VERSION = 2
INDEX_USER_VERSION_SQL = f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}"
ANALYZER_FINGERPRINT = "python-ast-1|javascript-tree-sitter-1|typescript-tree-sitter-1|generic-lexical-1"
DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024
DEFAULT_MAX_REPOSITORY_SIZE = 1024 * 1024 * 1024

EXPECTED_TABLE_COLUMNS = {
    "metadata": (("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)),
    "files": (("path", "TEXT", 0, 1), ("digest", "TEXT", 1, 0), ("category", "TEXT", 1, 0)),
    "symbols": (
        ("name", "TEXT", 1, 0),
        ("kind", "TEXT", 1, 0),
        ("path", "TEXT", 1, 0),
        ("line", "INTEGER", 1, 0),
        ("end_line", "INTEGER", 1, 0),
        ("parent", "TEXT", 0, 0),
        ("signature", "TEXT", 0, 0),
    ),
    "refs": (
        ("name", "TEXT", 1, 0),
        ("path", "TEXT", 1, 0),
        ("line", "INTEGER", 1, 0),
        ("context", "TEXT", 1, 0),
        ("caller", "TEXT", 0, 0),
    ),
    "relationships": (
        ("source", "TEXT", 1, 0),
        ("target", "TEXT", 1, 0),
        ("kind", "TEXT", 1, 0),
        ("path", "TEXT", 1, 0),
        ("line", "INTEGER", 1, 0),
        ("confidence", "TEXT", 1, 0),
    ),
}
TABLE_INFO_SQL = {
    name: f"PRAGMA table_xinfo({name})" for name in EXPECTED_TABLE_COLUMNS
}
EXPECTED_INDEX_COLUMNS = {
    "symbols_name": ("symbols", ("name",)),
    "refs_name": ("refs", ("name",)),
    "relationships_source": ("relationships", ("source",)),
    "relationships_target": ("relationships", ("target",)),
}
INDEX_LIST_SQL = {
    table: f"PRAGMA index_list({table})" for table in EXPECTED_TABLE_COLUMNS
}
INDEX_XINFO_SQL = {
    name: f"PRAGMA index_xinfo({name})" for name in EXPECTED_INDEX_COLUMNS
}
FILES_PRIMARY_KEY_XINFO_SQL = "PRAGMA index_xinfo(sqlite_autoindex_files_1)"
SCHEMA_INVENTORY_SQL = "SELECT type, name, tbl_name, sql FROM sqlite_schema"

INDEX_SCHEMA = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    category TEXT NOT NULL
);
CREATE TABLE symbols (
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    parent TEXT,
    signature TEXT
);
CREATE INDEX symbols_name ON symbols(name);
CREATE TABLE refs (
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    context TEXT NOT NULL,
    caller TEXT
);
CREATE INDEX refs_name ON refs(name);
CREATE TABLE relationships (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    confidence TEXT NOT NULL
);
CREATE INDEX relationships_source ON relationships(source);
CREATE INDEX relationships_target ON relationships(target);
"""


@lru_cache(maxsize=1)
def _expected_schema_objects() -> frozenset[tuple[str, str, str, str | None]]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(INDEX_SCHEMA)
        return frozenset(
            tuple(row) for row in connection.execute(SCHEMA_INVENTORY_SQL)
        )
    finally:
        connection.close()


class ContextForgeError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class SourceAccessError(ContextForgeError):
    def __init__(self, message: str, **details):
        super().__init__("unsafe_source_access", message, **details)


class ResourceLimitError(ContextForgeError):
    pass


class CacheAccessError(ContextForgeError):
    def __init__(self, message: str, **details):
        super().__init__("unsafe_cache_path", message, **details)


def envelope(command: str, repo: Path, **payload):
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "repository": str(repo),
        **payload,
    }


def cache_root() -> Path:
    configured = os.environ.get("CONTEXTFORGE_CACHE_DIR")
    if configured:
        return Path(configured).expanduser().absolute()
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache).expanduser().absolute() / "contextforge"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "contextforge"
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "ContextForge" / "Cache"
    return Path.home() / ".cache" / "contextforge"


def repository_identity(repo: Path) -> str:
    canonical = os.path.normcase(str(repo.resolve(strict=True)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def index_path(repo: Path) -> Path:
    canonical = repo.resolve(strict=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", canonical.name).strip("-.") or "repository"
    return cache_root() / f"{safe_name}-{repository_identity(canonical)}" / INDEX_NAME


def _assert_no_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode) and metadata.st_uid != 0:
            raise CacheAccessError(
                f"Unsafe cache path contains a symlink: {current}", path=str(current)
            )


def read_source_snapshot(repo: Path, relative: Path, max_bytes: int) -> bytes:
    parts = relative.parts
    if not parts or relative.is_absolute() or ".." in parts:
        raise SourceAccessError(f"Unsafe source path: {relative}", path=str(relative))
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
    ):
        raise SourceAccessError(
            "Race-resistant source access is not supported on this platform",
            path=str(relative),
            platform=sys.platform,
        )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    directory_fd = None
    file_fd = None
    try:
        directory_fd = os.open(repo, directory_flags)
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise SourceAccessError(f"Source is not a regular file: {relative}", path=str(relative))
        if before.st_size > max_bytes:
            raise ResourceLimitError(
                "file_size_limit_exceeded",
                f"Source file exceeds the configured size limit: {relative}",
                path=str(relative),
                limit=max_bytes,
                observed=before.st_size,
            )
        chunks = []
        total = 0
        while chunk := os.read(file_fd, min(65536, max_bytes - total + 1)):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ResourceLimitError(
                    "file_size_limit_exceeded",
                    f"Source file exceeds the configured size limit: {relative}",
                    path=str(relative),
                    limit=max_bytes,
                    observed=total,
                )
        after = os.fstat(file_fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise SourceAccessError(f"Source changed while being read: {relative}", path=str(relative))
        return b"".join(chunks)
    except OSError as exc:
        raise SourceAccessError(
            f"Source is a symlink, inaccessible, or unsafe: {relative}", path=str(relative)
        ) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _initialize_database(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.executescript(INDEX_SCHEMA)
        connection.execute(INDEX_USER_VERSION_SQL)
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('analyzer_fingerprint', ?)",
            (ANALYZER_FINGERPRINT,),
        )
        connection.commit()
    finally:
        connection.close()


def _database_sidecars(database: Path) -> tuple[Path, Path, Path]:
    return (
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
    )


def _assert_safe_database_path(database: Path) -> None:
    if database.is_symlink():
        raise CacheAccessError(
            f"Unsafe cache path: {database} must not be a symlink",
            path=str(database),
        )
    if database.exists() and not stat.S_ISREG(database.lstat().st_mode):
        raise CacheAccessError(
            f"Unsafe cache database type: {database}", path=str(database)
        )


def _database_identity(database: Path) -> tuple[int, int]:
    _assert_safe_database_path(database)
    metadata = database.lstat()
    return metadata.st_dev, metadata.st_ino


def _assert_database_identity(database: Path, expected: tuple[int, int]) -> None:
    if _database_identity(database) != expected:
        raise CacheAccessError(
            f"Unsafe cache path changed during database access: {database}",
            path=str(database),
        )


def _prepare_database_replacement(database: Path) -> None:
    sidecars = _database_sidecars(database)
    if database.exists():
        try:
            identity = _database_identity(database)
            connection = sqlite3.connect(database, timeout=0.05)
            try:
                _assert_database_identity(database, identity)
                mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
                if str(mode).lower() != "delete":
                    raise CacheAccessError(
                        f"Cannot disable active SQLite journal for cache: {database}",
                        path=str(database),
                    )
            finally:
                connection.close()
        except sqlite3.OperationalError as exc:
            raise CacheAccessError(
                f"Cache database is active or locked: {database}", path=str(database)
            ) from exc
        except sqlite3.DatabaseError as exc:
            if any(sidecar.exists() or sidecar.is_symlink() for sidecar in sidecars):
                raise CacheAccessError(
                    f"Cannot safely replace cache with unreadable SQLite sidecars: {database}",
                    path=str(database),
                ) from exc
    rollback_journal = sidecars[-1]
    if rollback_journal.is_symlink() or rollback_journal.exists():
        raise CacheAccessError(
            f"Cache database has an active or unsafe rollback journal: {database}",
            path=str(database),
        )
    for sidecar in sidecars[:-1]:
        if sidecar.is_symlink():
            raise CacheAccessError(
                f"Unsafe SQLite sidecar path: {sidecar}", path=str(sidecar)
            )
        if sidecar.exists():
            metadata = sidecar.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise CacheAccessError(
                    f"Unsafe SQLite sidecar type: {sidecar}", path=str(sidecar)
                )
            sidecar.unlink()


def _rebuild_database(database: Path) -> None:
    _prepare_database_replacement(database)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{INDEX_NAME}.", suffix=".tmp", dir=database.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        _initialize_database(temporary)
        os.replace(temporary, database)
    finally:
        temporary.unlink(missing_ok=True)


def _database_is_compatible(connection: sqlite3.Connection) -> bool:
    if connection.execute("PRAGMA user_version").fetchone()[0] != INDEX_SCHEMA_VERSION:
        return False
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        return False
    schema_objects = frozenset(
        tuple(row) for row in connection.execute(SCHEMA_INVENTORY_SQL)
    )
    if schema_objects != _expected_schema_objects():
        return False
    fingerprint_rows = tuple(
        connection.execute("SELECT key, value FROM metadata ORDER BY key")
    )
    if fingerprint_rows != (("analyzer_fingerprint", ANALYZER_FINGERPRINT),):
        return False
    for table, expected in EXPECTED_TABLE_COLUMNS.items():
        columns = tuple(connection.execute(TABLE_INFO_SQL[table]))
        actual = tuple(
            (row[1], row[2].upper(), row[3], row[5])
            for row in columns
        )
        if actual != expected or any(row[6] != 0 for row in columns):
            return False
    files_indexes = tuple(connection.execute(INDEX_LIST_SQL["files"]))
    if len(files_indexes) != 1:
        return False
    _sequence, _name, unique, origin, partial = files_indexes[0]
    if unique != 1 or origin != "pk" or partial != 0:
        return False
    primary_key = tuple(
        (row[2], row[3], str(row[4]).upper())
        for row in connection.execute(FILES_PRIMARY_KEY_XINFO_SQL)
        if row[5] == 1
    )
    if primary_key != (("path", 0, "BINARY"),):
        return False
    for index, (table, expected_columns) in EXPECTED_INDEX_COLUMNS.items():
        definitions = {
            row[1]: row for row in connection.execute(INDEX_LIST_SQL[table])
        }
        expected_names = {
            name
            for name, (owning_table, _columns) in EXPECTED_INDEX_COLUMNS.items()
            if owning_table == table
        }
        if set(definitions) != expected_names:
            return False
        definition = definitions.get(index)
        if definition is None:
            return False
        _sequence, _name, unique, origin, partial = definition
        if unique != 0 or origin != "c" or partial != 0:
            return False
        key_columns = tuple(
            (row[2], row[3], str(row[4]).upper())
            for row in connection.execute(INDEX_XINFO_SQL[index])
            if row[5] == 1
        )
        expected_keys = tuple((column, 0, "BINARY") for column in expected_columns)
        if key_columns != expected_keys:
            return False
    return True


def _ensure_database(database: Path) -> None:
    _assert_safe_database_path(database)
    if not database.exists():
        _rebuild_database(database)
        return
    try:
        identity = _database_identity(database)
        connection = sqlite3.connect(database)
        try:
            _assert_database_identity(database, identity)
            compatible = _database_is_compatible(connection)
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        compatible = False
    if not compatible:
        _rebuild_database(database)


def _connect(repo: Path) -> tuple[sqlite3.Connection, tuple[int, int]]:
    root = cache_root()
    cache = index_path(repo).parent
    database = index_path(repo)
    _assert_no_symlink_components(root)
    if root.is_symlink() or cache.is_symlink() or database.is_symlink():
        raise CacheAccessError(
            f"Unsafe cache path: {cache} must not be a symlink", path=str(cache)
        )
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_no_symlink_components(cache)
    if root.is_symlink() or cache.is_symlink() or database.is_symlink():
        raise CacheAccessError(
            f"Unsafe cache path changed during setup: {cache}", path=str(cache)
        )
    _ensure_database(database)
    identity = _database_identity(database)
    connection = sqlite3.connect(database)
    try:
        _assert_database_identity(database, identity)
        connection.row_factory = sqlite3.Row
        return connection, identity
    except Exception:
        connection.close()
        raise


def extract_entities(path: str, content: str) -> tuple[list[Symbol], list[Reference]]:
    source_file = SourceFile(path, content)
    analyzer = analyzer_for_path(Path(path))
    return (
        analyzer.extract_symbols(source_file),
        analyzer.extract_references(source_file),
    )


def extract_analysis(
    path: str,
    content: str | bytes,
) -> tuple[list[Symbol], list[Reference], list[Relationship]]:
    source_file = SourceFile(path, content)
    analyzer = analyzer_for_path(Path(path))
    return (
        analyzer.extract_symbols(source_file),
        analyzer.extract_references(source_file),
        analyzer.extract_relationships(source_file),
    )


def _configured_limit(explicit: int | None, environment: str, default: int) -> int:
    raw_value = explicit if explicit is not None else os.environ.get(environment, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ContextForgeError(
            "invalid_configuration",
            f"{environment} must be a positive integer",
            setting=environment,
            value=str(raw_value),
        ) from exc
    if value <= 0:
        raise ContextForgeError(
            "invalid_configuration",
            f"{environment} must be a positive integer",
            setting=environment,
            value=value,
        )
    return value


def iter_source_snapshots(
    repo: Path,
    paths: Iterable[Path],
    *,
    max_file_size: int | None = None,
    max_repository_size: int | None = None,
):
    file_limit = _configured_limit(
        max_file_size, "CONTEXTFORGE_MAX_FILE_SIZE", DEFAULT_MAX_FILE_SIZE
    )
    repository_limit = _configured_limit(
        max_repository_size,
        "CONTEXTFORGE_MAX_REPOSITORY_SIZE",
        DEFAULT_MAX_REPOSITORY_SIZE,
    )
    repository_size = 0
    for path in paths:
        try:
            relative = path.relative_to(repo)
        except ValueError as exc:
            raise SourceAccessError(
                f"Source path escapes the repository: {path}", path=str(path)
            ) from exc
        snapshot = read_source_snapshot(repo, relative, file_limit)
        repository_size += len(snapshot)
        if repository_size > repository_limit:
            raise ResourceLimitError(
                "repository_size_limit_exceeded",
                "Repository exceeds the configured indexing size limit",
                path=str(relative),
                limit=repository_limit,
                observed=repository_size,
            )
        yield path, relative, snapshot


def build_index(
    repo: Path,
    *,
    max_file_size: int | None = None,
    max_repository_size: int | None = None,
) -> dict:
    connection, database_identity = _connect(repo)
    files = {str(path.relative_to(repo)): path for path in iter_code_files(repo)}
    existing = {row["path"]: row["digest"] for row in connection.execute("SELECT path, digest FROM files")}
    indexed = unchanged = removed = 0
    try:
        snapshots = iter_source_snapshots(
            repo,
            files.values(),
            max_file_size=max_file_size,
            max_repository_size=max_repository_size,
        )
        for path, relative_path, snapshot in snapshots:
            relative = str(relative_path)
            digest = hashlib.sha256(snapshot).hexdigest()
            if existing.get(relative) == digest:
                unchanged += 1
                continue
            symbols, references, relationships = extract_analysis(relative, snapshot)
            connection.execute("DELETE FROM symbols WHERE path = ?", (relative,))
            connection.execute("DELETE FROM refs WHERE path = ?", (relative,))
            connection.execute("DELETE FROM relationships WHERE path = ?", (relative,))
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
            connection.executemany(
                "INSERT INTO relationships(source, target, kind, path, line, confidence) VALUES (:source, :target, :kind, :path, :line, :confidence)",
                [asdict(item) for item in relationships],
            )
            indexed += 1
        stale = set(existing) - set(files)
        for relative in stale:
            connection.execute("DELETE FROM files WHERE path = ?", (relative,))
            connection.execute("DELETE FROM symbols WHERE path = ?", (relative,))
            connection.execute("DELETE FROM refs WHERE path = ?", (relative,))
            connection.execute("DELETE FROM relationships WHERE path = ?", (relative,))
            removed += 1
        _assert_database_identity(index_path(repo), database_identity)
        connection.commit()
        _assert_database_identity(index_path(repo), database_identity)
        symbol_count = connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        reference_count = connection.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        relationship_count = connection.execute(
            "SELECT COUNT(*) FROM relationships"
        ).fetchone()[0]
    finally:
        connection.close()
    return {"indexed": indexed, "unchanged": unchanged, "removed": removed, "total_files": len(files), "symbols": symbol_count, "references": reference_count, "relationships": relationship_count}


def find_symbols(repo: Path, name: str) -> list[dict]:
    build_index(repo)
    connection, _database_identity_value = _connect(repo)
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
    connection, _database_identity_value = _connect(repo)
    try:
        rows = connection.execute(
            "SELECT name, path, line, context, caller FROM refs WHERE lower(name) = lower(?) ORDER BY path, line",
            (name,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def find_relationships(
    repo: Path,
    *,
    source: str | None = None,
    target: str | None = None,
    kind: str | None = None,
) -> list[dict]:
    build_index(repo)
    connection, _database_identity_value = _connect(repo)
    clauses: list[str] = []
    parameters: list[str] = []
    for column, value in (("source", source), ("target", target), ("kind", kind)):
        if value is not None:
            clauses.append(f"lower({column}) = lower(?)")
            parameters.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        rows = connection.execute(
            "SELECT source, target, kind, path, line, confidence "
            f"FROM relationships{where} ORDER BY path, line, kind, source, target",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def trace_symbol(repo: Path, name: str, max_depth: int = 6) -> tuple[list[dict], list[dict]]:
    build_index(repo)
    connection, _database_identity_value = _connect(repo)
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
                    "SELECT DISTINCT target AS name FROM relationships "
                    "WHERE path=? AND source=? AND kind='calls' AND line BETWEEN ? AND ?",
                    (node["path"], node["name"], node["line"], node["end_line"]),
                ).fetchall()
                for call in calls:
                    target = call["name"]
                    if connection.execute("SELECT 1 FROM symbols WHERE lower(name)=lower(?) LIMIT 1", (target,)).fetchone():
                        edges.append(
                            {
                                "from": node["name"],
                                "to": target,
                                "path": node["path"],
                                "from_line": node["line"],
                            }
                        )
                        queue.append((target, depth + 1))
    finally:
        connection.close()
    return list(nodes.values()), edges


def parse_pyproject_snapshot(snapshot: bytes) -> dict:
    try:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
            from importlib import import_module

            tomllib = import_module("tomli")
        document = tomllib.loads(snapshot.decode("utf-8", errors="replace"))
        return document if isinstance(document, dict) else {}
    except Exception:
        return {}


def read_pyproject(repo: Path) -> dict:
    path = repo / "pyproject.toml"
    if not path.exists():
        return {}
    file_limit = _configured_limit(
        None, "CONTEXTFORGE_MAX_FILE_SIZE", DEFAULT_MAX_FILE_SIZE
    )
    snapshot = read_source_snapshot(repo, Path("pyproject.toml"), file_limit)
    return parse_pyproject_snapshot(snapshot)


def analyzer_repository(repo: Path) -> Repository:
    file_limit = _configured_limit(
        None, "CONTEXTFORGE_MAX_FILE_SIZE", DEFAULT_MAX_FILE_SIZE
    )
    manifests: dict[str, SourceFile] = {}
    for name in (
        "pyproject.toml",
        "setup.py",
        "requirements.txt",
        "package.json",
        "tsconfig.json",
    ):
        if (repo / name).exists():
            snapshot = read_source_snapshot(repo, Path(name), file_limit)
            manifests[name] = SourceFile(name, snapshot)
    paths = tuple(
        sorted(str(path.relative_to(repo)) for path in iter_code_files(repo))
    )
    return Repository(repo, manifests, paths)


def repository_brief(repo: Path, detections: Iterable[str], architecture: dict[str, list[str]]) -> dict:
    stats = build_index(repo)
    repository = analyzer_repository(repo)
    analyzer_results = [
        result
        for analyzer in analyzer_registry()
        if (result := analyzer.detect(repository)).detected
    ]
    pyproject = repository.manifest("pyproject.toml")
    project = (
        parse_pyproject_snapshot(pyproject.snapshot).get("project", {})
        if pyproject
        else {}
    )
    discovered_entry_points = [
        item
        for analyzer in analyzer_registry()
        for item in analyzer.discover_entry_points(repository)
    ]
    entry_points = []
    for item in discovered_entry_points:
        module, _, symbol = item.target.partition(":")
        entry_points.append(
            {
                "command": item.name,
                "target": item.target,
                "module": module,
                "symbol": symbol,
            }
        )
    connection, _database_identity_value = _connect(repo)
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
        "analyzers": [
            {
                "name": result.analyzer,
                "confidence": result.confidence,
                "evidence": list(result.evidence),
            }
            for result in analyzer_results
        ],
        "entry_points": entry_points,
        "architecture": architecture,
        "symbols": {"total": stats["symbols"], "references": stats["references"]},
        "relationships": stats["relationships"],
        "evidence": evidence,
        "confidence": "high" if stats["symbols"] else "low",
    }


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)
