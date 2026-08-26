from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from collections import Counter, defaultdict, deque
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
from .claims import claim, evidence as claim_evidence
from .frameworks import FrameworkEntity, extract_framework_entities
from .ranking import classify_file, query_words
from .scanner import iter_code_files

SCHEMA_VERSION = "1.0"
INDEX_NAME = "index.sqlite"
INDEX_SCHEMA_VERSION = 3
INDEX_USER_VERSION_SQL = f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}"
ANALYZER_FINGERPRINT = "python-ast-1|javascript-tree-sitter-2|typescript-tree-sitter-2|generic-lexical-1|route-identity-1|fastapi-framework-12|flask-framework-3|django-framework-3|express-framework-9|nestjs-framework-9|sqlalchemy-framework-10|celery-framework-8|react-framework-4|nextjs-framework-9|angular-framework-4|prisma-framework-9|typeorm-framework-3"
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
    "framework_entities": (
        ("framework", "TEXT", 1, 0),
        ("kind", "TEXT", 1, 0),
        ("name", "TEXT", 1, 0),
        ("target", "TEXT", 0, 0),
        ("path", "TEXT", 1, 0),
        ("line", "INTEGER", 1, 0),
        ("end_line", "INTEGER", 1, 0),
        ("confidence", "TEXT", 1, 0),
        ("attributes", "TEXT", 1, 0),
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
    "framework_entities_kind": ("framework_entities", ("kind",)),
    "framework_entities_name": ("framework_entities", ("name",)),
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
CREATE TABLE framework_entities (
    framework TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    target TEXT,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    attributes TEXT NOT NULL
);
CREATE INDEX framework_entities_kind ON framework_entities(kind);
CREATE INDEX framework_entities_name ON framework_entities(name);
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


_PUBLIC_CONFIDENCES = frozenset({"high", "medium", "low"})


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_framework_attributes(value: object) -> dict[str, str]:
    if not isinstance(value, str):
        raise ValueError("framework attributes must use SQLite TEXT storage")
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(constant)
            ),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("invalid framework attributes") from exc
    if not isinstance(decoded, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in decoded.items()
    ):
        raise ValueError("framework attributes must be a string map")
    if json.dumps(decoded, sort_keys=True) != value:
        raise ValueError("framework attributes must use canonical JSON")
    return decoded


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
    expected_handles: Counter[tuple[str, str, int, str, str]] = Counter()
    for row in connection.execute(
        "SELECT framework, kind, name, target, path, line, end_line, "
        "confidence, attributes FROM framework_entities"
    ):
        framework, kind, name, target, path, line, end_line, confidence, raw = row
        if confidence not in _PUBLIC_CONFIDENCES:
            return False
        try:
            attributes = _decode_framework_attributes(raw)
        except ValueError:
            return False
        if kind == "route" and target is not None:
            identity = _route_registration_identity(
                framework=framework,
                kind=kind,
                name=name,
                target=target,
                path=path,
                line=line,
                end_line=end_line,
                confidence=confidence,
                attributes=attributes,
            )
            encoded = f"{confidence}{_ROUTE_IDENTITY_SEPARATOR}{identity}"
            expected_handles[(name, path, line, target, encoded)] += 1
    actual_handles: Counter[tuple[str, str, int, str, str]] = Counter()
    for source, target, kind, path, line, confidence in connection.execute(
        "SELECT source, target, kind, path, line, confidence FROM relationships"
    ):
        if not isinstance(confidence, str):
            return False
        if kind == "handles":
            match = re.fullmatch(
                r"(high|medium|low)\x1froute:([0-9a-f]{64})", confidence
            )
            if match is None:
                return False
            actual_handles[(source, path, line, target, confidence)] += 1
        elif (
            confidence not in _PUBLIC_CONFIDENCES
            or _ROUTE_IDENTITY_SEPARATOR in confidence
        ):
            return False
    if actual_handles != expected_handles:
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


_ROUTE_IDENTITY_SEPARATOR = "\x1froute:"


def _route_registration_identity(
    *,
    framework: str,
    kind: str,
    name: str,
    target: str | None,
    path: str,
    line: int,
    end_line: int,
    confidence: str,
    attributes: dict[str, str],
) -> str:
    payload = json.dumps(
        [
            framework,
            kind,
            name,
            target,
            path,
            line,
            end_line,
            confidence,
            attributes,
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _route_relationship_confidence(entity: FrameworkEntity) -> str:
    identity = _route_registration_identity(
        framework=entity.framework,
        kind=entity.kind,
        name=entity.name,
        target=entity.target,
        path=entity.path,
        line=entity.line,
        end_line=entity.end_line,
        confidence=entity.confidence,
        attributes=dict(entity.attributes),
    )
    return f"{entity.confidence}{_ROUTE_IDENTITY_SEPARATOR}{identity}"


def _public_relationship_confidence(confidence: str) -> str:
    if confidence in _PUBLIC_CONFIDENCES:
        return confidence
    match = re.fullmatch(
        r"(high|medium|low)\x1froute:[0-9a-f]{64}", confidence
    )
    if match is None:
        raise ValueError("invalid persisted relationship confidence")
    return match.group(1)


def extract_analysis(
    path: str,
    content: str | bytes,
) -> tuple[
    list[Symbol],
    list[Reference],
    list[Relationship],
    list[FrameworkEntity],
]:
    source_file = SourceFile(path, content)
    analyzer = analyzer_for_path(Path(path))
    framework_entities = extract_framework_entities(source_file)
    relationships = analyzer.extract_relationships(source_file)
    relationships.extend(
        Relationship(
            source=entity.name,
            target=entity.target,
            kind="handles",
            path=entity.path,
            line=entity.line,
            confidence=_route_relationship_confidence(entity),
        )
        for entity in framework_entities
        if entity.kind == "route" and entity.target is not None
    )
    return (
        analyzer.extract_symbols(source_file),
        analyzer.extract_references(source_file),
        relationships,
        framework_entities,
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
            symbols, references, relationships, framework_entities = extract_analysis(
                relative, snapshot
            )
            connection.execute("DELETE FROM symbols WHERE path = ?", (relative,))
            connection.execute("DELETE FROM refs WHERE path = ?", (relative,))
            connection.execute("DELETE FROM relationships WHERE path = ?", (relative,))
            connection.execute("DELETE FROM framework_entities WHERE path = ?", (relative,))
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
            connection.executemany(
                "INSERT INTO framework_entities(framework, kind, name, target, path, line, end_line, confidence, attributes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item.framework,
                        item.kind,
                        item.name,
                        item.target,
                        item.path,
                        item.line,
                        item.end_line,
                        item.confidence,
                        json.dumps(
                            dict(item.attributes), sort_keys=True, allow_nan=False
                        ),
                    )
                    for item in framework_entities
                ],
            )
            indexed += 1
        stale = set(existing) - set(files)
        for relative in stale:
            connection.execute("DELETE FROM files WHERE path = ?", (relative,))
            connection.execute("DELETE FROM symbols WHERE path = ?", (relative,))
            connection.execute("DELETE FROM refs WHERE path = ?", (relative,))
            connection.execute("DELETE FROM relationships WHERE path = ?", (relative,))
            connection.execute("DELETE FROM framework_entities WHERE path = ?", (relative,))
            removed += 1
        _assert_database_identity(index_path(repo), database_identity)
        connection.commit()
        _assert_database_identity(index_path(repo), database_identity)
        symbol_count = connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        reference_count = connection.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        relationship_count = connection.execute(
            "SELECT COUNT(*) FROM relationships"
        ).fetchone()[0]
        framework_entity_count = connection.execute(
            "SELECT COUNT(*) FROM framework_entities"
        ).fetchone()[0]
    finally:
        connection.close()
    return {"indexed": indexed, "unchanged": unchanged, "removed": removed, "total_files": len(files), "symbols": symbol_count, "references": reference_count, "relationships": relationship_count, "framework_entities": framework_entity_count}


def find_symbols(repo: Path, name: str) -> list[dict]:
    build_index(repo)
    connection, _database_identity_value = _connect(repo)
    try:
        rows = connection.execute(
            "SELECT name, kind, path, line, end_line, parent, signature FROM symbols "
            "WHERE lower(name) = lower(?) "
            "ORDER BY path, line, end_line, kind, name, parent, signature",
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
            "SELECT name, path, line, context, caller FROM refs "
            "WHERE lower(name) = lower(?) "
            "ORDER BY path, line, name, context, caller",
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
            f"FROM relationships{where} "
            "ORDER BY path, line, kind, source, target, confidence",
            parameters,
        ).fetchall()
        relationships = []
        for row in rows:
            relationship = dict(row)
            relationship["confidence"] = _public_relationship_confidence(
                relationship["confidence"]
            )
            relationships.append(relationship)
        return relationships
    finally:
        connection.close()


def find_framework_entities(
    repo: Path,
    *,
    framework: str | None = None,
    kind: str | None = None,
    kinds: Iterable[str] | None = None,
    name: str | None = None,
) -> list[dict]:
    build_index(repo)
    connection, _database_identity_value = _connect(repo)
    clauses: list[str] = []
    parameters: list[str] = []
    for column, value in (("framework", framework), ("kind", kind), ("name", name)):
        if value is not None:
            clauses.append(f"lower({column}) = lower(?)")
            parameters.append(value)
    selected_kinds = tuple(kinds or ())
    if kind is None and selected_kinds:
        placeholders = ", ".join("lower(?)" for _kind in selected_kinds)
        clauses.append(f"lower(kind) IN ({placeholders})")
        parameters.extend(selected_kinds)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        rows = connection.execute(
            "SELECT framework, kind, name, target, path, line, end_line, confidence, attributes "
            f"FROM framework_entities{where} "
            "ORDER BY path, line, end_line, kind, name, framework, target, confidence, attributes",
            parameters,
        ).fetchall()
        entities = []
        for row in rows:
            entity = dict(row)
            entity["attributes"] = _decode_framework_attributes(
                entity["attributes"]
            )
            entities.append(entity)
        return entities
    finally:
        connection.close()


_REPORT_STOP_WORDS = frozenset(
    {
        "a",
        "add",
        "after",
        "an",
        "and",
        "does",
        "for",
        "how",
        "in",
        "is",
        "of",
        "remain",
        "rename",
        "the",
        "to",
        "what",
        "when",
        "why",
        "with",
        "work",
        "working",
    }
)
_TEST_CATEGORIES = frozenset({"Tests"})
_CONFIG_FILENAMES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "tsconfig.json",
    }
)


def _report_terms(query: str) -> list[str]:
    terms = []
    for term in query_words(query):
        if len(term) < 2 or term in _REPORT_STOP_WORDS or term in terms:
            continue
        terms.append(term)
    return terms


def _report_term_variants(term: str) -> set[str]:
    variants = {term}
    if term.endswith("ing") and len(term) > 5:
        stem = term[:-3]
        variants.add(stem)
        if len(stem) >= 2 and stem[-1] == stem[-2]:
            variants.add(stem[:-1])
        variants.add(f"{stem}e")
    elif term.endswith("ed") and len(term) > 4:
        stem = term[:-2]
        variants.add(stem)
        variants.add(f"{stem}e")
    elif term.endswith("es") and len(term) > 4:
        variants.add(term[:-2])
    elif term.endswith("s") and len(term) > 3:
        variants.add(term[:-1])
    return variants


def _row_term_matches(
    row: sqlite3.Row, terms: list[str], fields: tuple[str, ...]
) -> set[str]:
    text = " ".join(str(row[field] or "") for field in fields)
    words = set(query_words(text.replace("_", " ")))
    return {term for term in terms if _report_term_variants(term) & words}


def _row_matches_terms(row: sqlite3.Row, terms: list[str], fields: tuple[str, ...]) -> int:
    return len(_row_term_matches(row, terms, fields))


def _report_confidence(
    fact_count: int,
    relevant_file_count: int,
    matched_term_count: int,
    term_count: int,
) -> str:
    coverage = matched_term_count / term_count if term_count else 0.0
    if coverage == 1.0 and fact_count >= 5 and relevant_file_count >= 3:
        return "high"
    if coverage >= 0.6 and (fact_count >= 2 or relevant_file_count >= 2):
        return "medium"
    return "low"


def task_report(repo: Path, query: str, kind: str, *, limit: int = 20) -> dict:
    if kind not in {"investigate", "impact", "change", "debug"}:
        raise ValueError(f"Unsupported task report kind: {kind}")
    build_index(repo)
    terms = _report_terms(query)
    connection, _database_identity_value = _connect(repo)
    try:
        symbols = connection.execute(
            "SELECT name, kind, path, line, end_line, parent, signature FROM symbols"
        ).fetchall()
        references = connection.execute(
            "SELECT name, path, line, context, caller FROM refs"
        ).fetchall()
        relationships = connection.execute(
            "SELECT source, target, kind, path, line, confidence FROM relationships"
        ).fetchall()
        entities = connection.execute(
            "SELECT framework, kind, name, target, path, line, end_line, confidence, attributes "
            "FROM framework_entities"
        ).fetchall()
        files = connection.execute("SELECT path, category FROM files").fetchall()
    finally:
        connection.close()

    if not terms:
        matched_symbols = []
        matched_references = []
        matched_relationships = []
        matched_entities = []
        matched_files = []
    else:
        matched_symbols = sorted(
            (
                (_row_matches_terms(row, terms, ("name", "parent", "signature", "path")), row)
                for row in symbols
            ),
            key=lambda item: (
                -item[0], item[1]["path"], item[1]["line"], item[1]["name"],
                item[1]["kind"], item[1]["parent"] or "", item[1]["signature"] or "",
            ),
        )
        matched_symbols = [row for score, row in matched_symbols if score][:limit]
        matched_references = sorted(
            (
                (_row_matches_terms(row, terms, ("name", "context", "caller", "path")), row)
                for row in references
            ),
            key=lambda item: (
                -item[0], item[1]["path"], item[1]["line"], item[1]["name"],
                item[1]["caller"] or "", item[1]["context"],
            ),
        )
        matched_references = [row for score, row in matched_references if score][:limit]
        matched_relationships = sorted(
            (
                (_row_matches_terms(row, terms, ("source", "target", "kind", "path")), row)
                for row in relationships
            ),
            key=lambda item: (
                -item[0], item[1]["path"], item[1]["line"], item[1]["source"],
                item[1]["target"], item[1]["kind"], item[1]["confidence"],
            ),
        )
        matched_relationships = [row for score, row in matched_relationships if score][:limit]
        matched_entities = sorted(
            (
                (_row_matches_terms(row, terms, ("name", "target", "kind", "path")), row)
                for row in entities
            ),
            key=lambda item: (
                -item[0], item[1]["path"], item[1]["line"], item[1]["name"],
                item[1]["target"] or "", item[1]["framework"], item[1]["kind"],
                item[1]["confidence"], item[1]["attributes"],
            ),
        )
        matched_entities = [row for score, row in matched_entities if score][:limit]
        matched_files = sorted(
            (
                (_row_matches_terms(row, terms, ("path", "category")), row)
                for row in files
            ),
            key=lambda item: (-item[0], item[1]["path"]),
        )
        matched_files = [row for score, row in matched_files if score][:limit]

    definitions = [dict(row) for row in matched_symbols]
    reference_items = [dict(row) for row in matched_references]
    relationship_items = [dict(row) for row in matched_relationships]
    related_names = {item["name"] for item in definitions + reference_items}
    if related_names:
        connected_rows = [
            row
            for row in relationships
            if row["source"] in related_names or row["target"] in related_names
        ]
        connected_rows.sort(
            key=lambda row: (
                row["path"], row["line"], row["source"], row["target"],
                row["kind"], row["confidence"],
            )
        )
        known_relationships = {
            (item["source"], item["target"], item["kind"], item["path"], item["line"])
            for item in relationship_items
        }
        for row in connected_rows:
            identity = (
                row["source"],
                row["target"],
                row["kind"],
                row["path"],
                row["line"],
            )
            if identity in known_relationships:
                continue
            relationship_items.append(dict(row))
            known_relationships.add(identity)
            if len(relationship_items) >= limit:
                break
    entity_items = []
    for row in matched_entities:
        item = dict(row)
        item["attributes"] = json.loads(item["attributes"])
        entity_items.append(item)

    relevant_paths = {}
    for item in definitions + reference_items + relationship_items + entity_items:
        relevant_paths.setdefault(item["path"], {"path": item["path"], "reasons": set()})
        if item in definitions:
            relevant_paths[item["path"]]["reasons"].add("definition")
        elif item in reference_items:
            relevant_paths[item["path"]]["reasons"].add("reference")
        elif item in relationship_items:
            relevant_paths[item["path"]]["reasons"].add("relationship")
        else:
            relevant_paths[item["path"]]["reasons"].add("framework_entity")
    for row in matched_files:
        relevant_paths.setdefault(row["path"], {"path": row["path"], "reasons": set()})
        relevant_paths[row["path"]]["reasons"].add("path_match")
    relevant_path_items = [
        {"path": item["path"], "reasons": sorted(item["reasons"])}
        for item in sorted(relevant_paths.values(), key=lambda item: item["path"])
    ]

    ordered_files = sorted(files, key=lambda row: (row["path"], row["category"]))
    test_paths = [
        {"path": row["path"], "reason": "existing_test"}
        for row in ordered_files
        if row["category"] in _TEST_CATEGORIES
        and (
            row["path"] in relevant_paths
            or any(term in row["path"].lower() for term in terms)
            or any(
                reference["path"] == row["path"] for reference in reference_items
            )
        )
    ][:limit]
    config_paths = [
        {"path": row["path"], "reason": "repository_configuration"}
        for row in ordered_files
        if Path(row["path"]).name.lower() in _CONFIG_FILENAMES
    ][:limit]

    facts = []
    for item in definitions[:8]:
        facts.append(
            {
                "statement": f"{item['name']} is defined as a {item['kind']}.",
                "evidence": {"path": item["path"], "line": item["line"]},
                "confidence": "high",
            }
        )
    for item in reference_items[:8]:
        facts.append(
            {
                "statement": f"{item['name']} is referenced here.",
                "evidence": {"path": item["path"], "line": item["line"]},
                "confidence": "high",
            }
        )
    for item in entity_items[:4]:
        facts.append(
            {
                "statement": f"{item['name']} is a {item['framework']} {item['kind']}.",
                "evidence": {"path": item["path"], "line": item["line"]},
                "confidence": item["confidence"],
            }
        )
    facts = facts[:limit]

    inferences = []
    if relationship_items:
        item = relationship_items[0]
        if item["kind"] == "calls":
            statement = (
                f"{item['source']} may call {item['target']} according to a "
                "statically indexed call relationship."
            )
        else:
            statement = (
                f"{item['source']} has a statically indexed {item['kind']} "
                f"relationship to {item['target']}; this does not establish "
                "runtime reachability."
            )
        inferences.append(
            {
                "statement": statement,
                "confidence": item["confidence"],
                "evidence": [{"path": item["path"], "line": item["line"]}],
            }
        )
    elif definitions and reference_items:
        inferences.append(
            {
                "statement": (
                    f"{reference_items[0]['path']} likely participates in the behavior "
                    f"defined by {definitions[0]['name']}."
                ),
                "confidence": "low",
                "evidence": [
                    {"path": definitions[0]["path"], "line": definitions[0]["line"]},
                    {
                        "path": reference_items[0]["path"],
                        "line": reference_items[0]["line"],
                    },
                ],
            }
        )

    seeds = []
    for item in definitions:
        if item["name"] not in seeds:
            seeds.append(item["name"])
    for item in entity_items:
        target = item.get("target") or item["name"]
        if target not in seeds:
            seeds.append(target)
    execution_paths = []
    for seed in seeds[:5]:
        nodes, edges = trace_symbol(repo, seed, max_depth=4)
        if nodes or edges:
            execution_paths.append({"entry": seed, "nodes": nodes, "edges": edges})

    matched_terms: set[str] = set()
    for row in matched_symbols:
        matched_terms.update(
            _row_term_matches(row, terms, ("name", "parent", "signature", "path"))
        )
    for row in matched_references:
        matched_terms.update(
            _row_term_matches(row, terms, ("name", "caller", "context", "path"))
        )
    for row in matched_relationships:
        matched_terms.update(
            _row_term_matches(row, terms, ("source", "target", "kind", "path"))
        )
    for row in matched_entities:
        matched_terms.update(
            _row_term_matches(
                row, terms, ("name", "target", "kind", "framework", "path")
            )
        )
    for row in matched_files:
        matched_terms.update(_row_term_matches(row, terms, ("path", "category")))
    confidence = _report_confidence(
        len(facts), len(relevant_path_items), len(matched_terms), len(terms)
    )
    unresolved_questions = []
    if not facts:
        unresolved_questions.append(
            "No indexed repository evidence matched the significant query terms. Which concrete symbol, route, or file should anchor the investigation?"
        )
    if facts and not execution_paths:
        unresolved_questions.append(
            "No statically resolvable execution path was found; runtime wiring may be involved."
        )
    if not test_paths:
        unresolved_questions.append(
            "No directly relevant existing tests were identified. Which behavior is expected to remain invariant?"
        )

    risks = []
    if reference_items:
        risks.append(
            {
                "risk": "Changing a matched definition may affect its indexed references.",
                "basis": "observed_references",
                "paths": sorted({item["path"] for item in reference_items}),
            }
        )
    if not execution_paths and facts:
        risks.append(
            {
                "risk": "Static evidence is incomplete for runtime or dynamic dispatch.",
                "basis": "missing_static_path",
                "paths": sorted(relevant_paths),
            }
        )
    if not test_paths:
        risks.append(
            {
                "risk": "No directly matching regression test was found.",
                "basis": "missing_test_evidence",
                "paths": [],
            }
        )

    claims = [
        claim(
            item["statement"],
            status="observed",
            confidence=item["confidence"],
            supporting_evidence=[item["evidence"]],
        )
        for item in facts
    ]
    claims.extend(
        claim(
            item["statement"],
            status="inferred",
            confidence=item["confidence"],
            supporting_evidence=item["evidence"],
            unresolved_uncertainty=[
                "Static evidence does not establish runtime behavior.",
                *unresolved_questions,
            ],
        )
        for item in inferences
    )
    if not claims:
        claims.append(
            claim(
                f"No indexed repository evidence matched the task query: {query}",
                status="inferred",
                confidence="unknown",
                unresolved_uncertainty=unresolved_questions,
            )
        )

    report = {
        "kind": kind,
        "query": query,
        "summary": (
            f"Found {len(facts)} observed facts across {len(relevant_path_items)} relevant paths."
            if facts
            else "No repository evidence matched the significant query terms."
        ),
        "confidence": confidence,
        "facts": facts,
        "inferences": inferences,
        "claims": claims,
        "unresolved_questions": unresolved_questions,
        "relevant_paths": relevant_path_items,
        "execution_paths": execution_paths,
        "definitions": definitions,
        "references": reference_items,
        "framework_entities": entity_items,
        "data_models_and_configuration": [
            *[
                item
                for item in entity_items
                if item["kind"] in {"model", "schema", "repository"}
            ],
            *config_paths,
        ],
        "tests": test_paths,
        "risks": risks,
    }
    if kind == "impact":
        report["affected_areas"] = [
            {
                "path": item["path"],
                "reasons": item["reasons"],
                "confidence": "high" if "definition" in item["reasons"] else "medium",
            }
            for item in relevant_path_items
        ]
    elif kind == "change":
        report["implementation_patterns"] = [
            {
                "pattern": "Follow the observed definition and reference structure.",
                "evidence": {"path": item["path"], "line": item["line"]},
            }
            for item in definitions[:5]
        ]
    elif kind == "debug":
        report["hypotheses"] = [
            {
                "rank": rank,
                "hypothesis": f"Inspect {item['name']} at the observed definition before assuming a runtime cause.",
                "confidence": "medium",
                "evidence": {"path": item["path"], "line": item["line"]},
            }
            for rank, item in enumerate(definitions[:5], start=1)
        ]
    return report


def trace_symbol(repo: Path, name: str, max_depth: int = 6) -> tuple[list[dict], list[dict]]:
    build_index(repo)
    if max_depth < 0:
        return [], []
    connection, _database_identity_value = _connect(repo)
    nodes: dict[tuple[object, ...], dict] = {}
    edges: list[dict] = []
    queue: deque[tuple[str, int, str | None, int | None]] = deque()
    visited: set[tuple[str, str | None, int | None]] = set()
    processed_definitions: set[tuple[str, str, int]] = set()
    try:
        routes = connection.execute(
            "SELECT framework, kind, name, target, path, line, end_line, confidence, attributes "
            "FROM framework_entities WHERE kind='route' AND lower(name)=lower(?) "
            "ORDER BY path, line, end_line, kind, name, framework, target, confidence, attributes",
            (name,),
        ).fetchall()
        if routes:
            for route_index, row in enumerate(routes):
                route = dict(row)
                route["attributes"] = _decode_framework_attributes(
                    route["attributes"]
                )
                registration_confidence = (
                    f"{route['confidence']}{_ROUTE_IDENTITY_SEPARATOR}"
                    + _route_registration_identity(
                        framework=route["framework"],
                        kind=route["kind"],
                        name=route["name"],
                        target=route["target"],
                        path=route["path"],
                        line=route["line"],
                        end_line=route["end_line"],
                        confidence=route["confidence"],
                        attributes=route["attributes"],
                    )
                )
                nodes[
                    (route["name"], route["path"], route["line"], route_index)
                ] = route
                registrations = connection.execute(
                    "SELECT target FROM relationships "
                    "WHERE kind='handles' AND lower(source)=lower(?) AND path=? AND line=? "
                    "AND confidence=? "
                    "ORDER BY target",
                    (
                        route["name"],
                        route["path"],
                        route["line"],
                        registration_confidence,
                    ),
                ).fetchall()
                targets = [registration["target"] for registration in registrations]
                matching_targets = [
                    target for target in targets if target == route["target"]
                ]
                if len(matching_targets) != 1:
                    target = route["target"] or "<unknown>"
                    edges.append(
                        {
                            "from": route["name"],
                            "to": target,
                            "kind": "handles",
                            "path": route["path"],
                            "from_line": route["line"],
                            "resolved": False,
                            "reason": (
                                "handler_relationship_not_found"
                                if not matching_targets
                                else "handler_relationship_ambiguous"
                            ),
                        }
                    )
                    continue
                target = matching_targets[0]
                if route["framework"] == "django":
                    handler_kind = route["attributes"].get(
                        "handler_kind", "function"
                    )
                    definitions = connection.execute(
                        "SELECT name, kind, path, line, end_line, parent, signature "
                        "FROM symbols WHERE lower(name)=lower(?) AND kind=? "
                        "AND parent IS NULL ORDER BY path, line, end_line",
                        (target, handler_kind),
                    ).fetchall()
                    view_parts = route["attributes"].get("view", "").split(".")
                    if view_parts and view_parts[-1] == "as_view":
                        view_parts.pop()
                    if view_parts and view_parts[-1].lower() == target.lower():
                        view_parts.pop()
                    if view_parts:
                        definitions = [
                            definition
                            for definition in definitions
                            if tuple(
                                Path(definition["path"]).with_suffix("").parts[
                                    -len(view_parts) :
                                ]
                            )
                            == tuple(view_parts)
                        ]
                elif (
                    route["framework"] == "express"
                    and "." not in route["attributes"].get("handler", "")
                ):
                    definitions = connection.execute(
                        "SELECT name, kind, path, line, end_line, parent, signature "
                        "FROM symbols WHERE lower(name)=lower(?) AND path=? "
                        "AND kind='function' AND parent IS NULL "
                        "ORDER BY line, end_line",
                        (target, route["path"]),
                    ).fetchall()
                else:
                    handler_kind = route["attributes"].get(
                        "handler_kind", "function"
                    )
                    definitions = connection.execute(
                        "SELECT name, kind, path, line, end_line, parent, signature "
                        "FROM symbols WHERE lower(name)=lower(?) AND path=? "
                        "AND kind=? AND (parent IS NULL OR ? != 'function') "
                        "AND line BETWEEN ? AND ? "
                        "ORDER BY line, end_line",
                        (
                            target,
                            route["path"],
                            handler_kind,
                            handler_kind,
                            route["line"],
                            route["end_line"],
                        ),
                    ).fetchall()
                resolved = len(definitions) == 1
                edge = {
                    "from": route["name"],
                    "to": target,
                    "kind": "handles",
                    "path": route["path"],
                    "from_line": route["line"],
                    "resolved": resolved,
                }
                if not resolved:
                    edge["reason"] = (
                        "handler_not_found"
                        if not definitions
                        else "handler_ambiguous"
                    )
                edges.append(edge)
                if resolved:
                    definition = definitions[0]
                    queue.append(
                        (
                            definition["name"],
                            1,
                            definition["path"],
                            definition["line"],
                        )
                    )
        else:
            queue.append((name, 0, None, None))

        while queue:
            current, depth, path, line = queue.popleft()
            visit_key = (current.lower(), path, line)
            if visit_key in visited or depth > max_depth:
                continue
            visited.add(visit_key)
            if path is None:
                definitions = connection.execute(
                    "SELECT name, kind, path, line, end_line, parent, signature "
                    "FROM symbols WHERE lower(name)=lower(?) ORDER BY path, line",
                    (current,),
                ).fetchall()
            else:
                definitions = connection.execute(
                    "SELECT name, kind, path, line, end_line, parent, signature "
                    "FROM symbols WHERE lower(name)=lower(?) AND path=? AND line=?",
                    (current, path, line),
                ).fetchall()
            for row in definitions:
                node = dict(row)
                definition_key = (
                    node["name"].lower(),
                    node["path"],
                    node["line"],
                )
                if definition_key in processed_definitions:
                    continue
                processed_definitions.add(definition_key)
                nodes[(node["name"], node["path"], node["line"])] = node
                calls = connection.execute(
                    "SELECT target AS name, MIN(line) AS first_line FROM relationships "
                    "WHERE path=? AND source=? AND kind='calls' AND line BETWEEN ? AND ? "
                    "GROUP BY target ORDER BY first_line, target",
                    (node["path"], node["name"], node["line"], node["end_line"]),
                ).fetchall()
                for call in calls:
                    target = call["name"]
                    candidates = connection.execute(
                        "SELECT name, path, line FROM symbols "
                        "WHERE lower(name)=lower(?) ORDER BY path, line",
                        (target,),
                    ).fetchall()
                    same_file = [
                        candidate
                        for candidate in candidates
                        if candidate["path"] == node["path"]
                    ]
                    if len(same_file) == 1:
                        definition = same_file[0]
                    elif not same_file and len(candidates) == 1:
                        definition = candidates[0]
                    else:
                        continue
                    edges.append(
                        {
                            "from": node["name"],
                            "to": target,
                            "kind": "calls",
                            "path": node["path"],
                            "from_line": node["line"],
                            "resolved": True,
                        }
                    )
                    queue.append(
                        (
                            target,
                            depth + 1,
                            definition["path"],
                            definition["line"],
                        )
                    )
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
    detections = list(detections)
    claims = [
        claim(
            f"{item.split(':', 1)[0].removesuffix(' project')} is detected as a repository technology.",
            status="observed",
            confidence="high",
            supporting_evidence=[
                claim_evidence(
                    path,
                    kind="manifest",
                    detail=f"Supports the {item} detection.",
                )
                for result in analyzer_results
                for path in result.evidence
                if result.analyzer.lower() in item.lower()
            ],
            unresolved_uncertainty=(
                ["No analyzer evidence was available for this detection."]
                if not any(
                    result.analyzer.lower() in item.lower() and result.evidence
                    for result in analyzer_results
                )
                else []
            ),
        )
        for item in detections
        if item != "Framework: Unknown"
    ]
    claims.extend(
        claim(
            f"{item.analyzer} analyzer detected repository support.",
            status="observed",
            confidence=item.confidence,
            supporting_evidence=[
                claim_evidence(path, kind="analyzer_detection")
                for path in item.evidence
            ],
        )
        for item in analyzer_results
    )
    return {
        "name": project.get("name") if isinstance(project, dict) else None,
        "description": project.get("description") if isinstance(project, dict) else None,
        "detections": detections,
        "claims": claims,
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
