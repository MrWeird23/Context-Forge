import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from contextforge import detector
from contextforge import intelligence
from contextforge import search as repository_search


JSON_SCHEMA = Path(__file__).parents[1] / "docs" / "json-schema-1.0.json"


@pytest.fixture
def sample_repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv(
        "CONTEXTFORGE_CACHE_DIR",
        str(tmp_path.parent / f".contextforge-test-cache-{tmp_path.name}"),
    )
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
    environment = os.environ.copy()
    environment.setdefault(
        "CONTEXTFORGE_CACHE_DIR",
        str(repo.parent / f".contextforge-test-cache-{repo.name}"),
    )
    return subprocess.run(
        [sys.executable, "-m", "contextforge", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
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


def test_all_json_commands_validate_against_documented_schema(sample_repo: Path):
    validator = Draft202012Validator(json.loads(JSON_SCHEMA.read_text()))
    commands = [
        ("search", "create_user", "--format", "json"),
        ("index", "--format", "json"),
        ("symbol", "create_user", "--format", "json"),
        ("refs", "create_user", "--format", "json"),
        ("trace", "register", "--format", "json"),
        ("investigate", "create user", "--format", "json"),
        ("impact", "create user", "--format", "json"),
        ("change", "create user", "--format", "json"),
        ("debug", "create user", "--format", "json"),
        ("routes", "--format", "json"),
        ("brief", "--format", "json"),
        ("index", "--max-file-size", "1", "--format", "json"),
    ]

    for command in commands:
        result = run_cf(sample_repo, *command)
        payload = json.loads(result.stdout)
        validator.validate(payload)


def test_index_is_incremental_and_excludes_its_own_cache(sample_repo: Path):
    first = run_cf(sample_repo, "index", "--format", "json")
    second = run_cf(sample_repo, "index", "--format", "json")

    assert first.returncode == second.returncode == 0
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["indexed"] == 3
    assert second_payload["indexed"] == 0
    assert second_payload["unchanged"] == first_payload["total_files"]
    assert intelligence.index_path(sample_repo).exists()


def test_index_digest_and_entities_come_from_one_source_snapshot(sample_repo: Path, monkeypatch):
    snapshot = b"def snapshot_entity():\n    return 1\n"
    original_reader = intelligence.read_source_snapshot

    def controlled_snapshot(repo: Path, relative: Path, max_bytes: int) -> bytes:
        if relative == Path("app.py"):
            return snapshot
        return original_reader(repo, relative, max_bytes)

    monkeypatch.setattr(intelligence, "read_source_snapshot", controlled_snapshot)

    intelligence.build_index(sample_repo)

    database = intelligence.index_path(sample_repo)
    with closing(sqlite3.connect(database)) as connection:
        recorded_digest = connection.execute(
            "SELECT digest FROM files WHERE path = 'app.py'"
        ).fetchone()[0]
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM symbols WHERE path = 'app.py'")
        }

    assert recorded_digest == hashlib.sha256(snapshot).hexdigest()
    assert names == {"snapshot_entity"}


def test_index_uses_external_cache_keyed_by_canonical_repository_identity(
    sample_repo: Path, tmp_path_factory, monkeypatch
):
    cache_root = tmp_path_factory.mktemp("external-cache")
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(cache_root))
    alias = cache_root.parent / "repo-alias"
    alias.symlink_to(sample_repo, target_is_directory=True)

    canonical_path = intelligence.index_path(sample_repo)
    alias_path = intelligence.index_path(alias)

    assert canonical_path == alias_path
    assert canonical_path.parent.parent == cache_root
    assert sample_repo not in canonical_path.parents
    assert canonical_path.parent.name.startswith(f"{sample_repo.name}-")


def test_index_refuses_source_replaced_by_symlink_after_discovery(
    sample_repo: Path, tmp_path_factory, monkeypatch
):
    source = sample_repo / "app.py"
    outside = tmp_path_factory.mktemp("race-target") / "outside.py"
    outside.write_text("def stolen_secret():\n    return 'secret'\n")
    source.unlink()
    source.symlink_to(outside)
    monkeypatch.setattr(intelligence, "iter_code_files", lambda _repo: iter([source]))

    with pytest.raises(intelligence.SourceAccessError, match="symlink|regular file"):
        intelligence.build_index(sample_repo)


def test_search_refuses_source_replaced_by_symlink_after_discovery(
    sample_repo: Path, tmp_path_factory, monkeypatch
):
    source = sample_repo / "app.py"
    outside = tmp_path_factory.mktemp("search-race-target") / "outside.py"
    outside.write_text("RACE_SENTINEL = 'must-not-be-searched'\n")
    source.unlink()
    source.symlink_to(outside)
    monkeypatch.setattr(repository_search, "iter_code_files", lambda _repo: iter([source]))

    with pytest.raises(intelligence.SourceAccessError, match="symlink|regular file"):
        repository_search.structured_search(sample_repo, "must-not-be-searched")


def test_index_refuses_source_modified_while_snapshot_is_read(sample_repo: Path, monkeypatch):
    source = sample_repo / "app.py"
    original_read = intelligence.os.read
    modified = False

    def read_then_modify(descriptor: int, size: int) -> bytes:
        nonlocal modified
        chunk = original_read(descriptor, size)
        if chunk and not modified:
            modified = True
            with source.open("ab") as handle:
                handle.write(b"# changed during read\n")
        return chunk

    monkeypatch.setattr(intelligence.os, "read", read_then_modify)

    with pytest.raises(intelligence.SourceAccessError, match="changed while being read"):
        intelligence.read_source_snapshot(
            sample_repo,
            Path("app.py"),
            intelligence.DEFAULT_MAX_FILE_SIZE,
        )


def test_index_refuses_same_size_rewrite_with_restored_mtime(
    sample_repo: Path, monkeypatch
):
    source = sample_repo / "app.py"
    source.write_bytes(b"a" * 131072)
    original_mtime = source.stat().st_mtime_ns
    original_read = intelligence.os.read
    modified = False

    def read_then_rewrite(descriptor: int, size: int) -> bytes:
        nonlocal modified
        chunk = original_read(descriptor, size)
        if chunk and not modified:
            modified = True
            source.write_bytes(b"b" * 131072)
            os.utime(source, ns=(original_mtime, original_mtime))
        return chunk

    monkeypatch.setattr(intelligence.os, "read", read_then_rewrite)

    with pytest.raises(intelligence.SourceAccessError, match="changed while being read"):
        intelligence.read_source_snapshot(
            sample_repo,
            Path("app.py"),
            intelligence.DEFAULT_MAX_FILE_SIZE,
        )


def test_source_access_fails_closed_without_no_follow_support(sample_repo: Path, monkeypatch):
    monkeypatch.delattr(intelligence.os, "O_NOFOLLOW")

    with pytest.raises(intelligence.SourceAccessError, match="not supported"):
        intelligence.read_source_snapshot(
            sample_repo,
            Path("app.py"),
            intelligence.DEFAULT_MAX_FILE_SIZE,
        )


def test_incompatible_index_is_atomically_rebuilt(sample_repo: Path):
    database = intelligence.index_path(sample_repo)
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version = 999")
        connection.execute("CREATE TABLE obsolete(value TEXT)")

    intelligence.build_index(sample_repo)

    with closing(sqlite3.connect(database)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        obsolete = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='obsolete'"
        ).fetchone()
    assert version == intelligence.INDEX_SCHEMA_VERSION
    assert obsolete is None


def test_matching_version_with_malformed_schema_is_rebuilt(sample_repo: Path):
    database = intelligence.index_path(sample_repo)
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(intelligence.INDEX_USER_VERSION_SQL)
        connection.execute("CREATE TABLE wrong_shape(value TEXT)")

    intelligence.build_index(sample_repo)

    with closing(sqlite3.connect(database)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"files", "symbols", "refs"}.issubset(tables)
    assert "wrong_shape" not in tables


@pytest.mark.parametrize(
    "replacement",
    [
        "CREATE UNIQUE INDEX symbols_name ON symbols(name)",
        "CREATE INDEX symbols_name ON symbols(name) WHERE kind = 'function'",
    ],
)
def test_matching_version_with_incompatible_index_is_rebuilt(
    sample_repo: Path, replacement: str
):
    database = intelligence.index_path(sample_repo)
    database.parent.mkdir(parents=True)
    intelligence._initialize_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP INDEX symbols_name")
        connection.execute(replacement)
    (sample_repo / "duplicate.py").write_text("def register():\n    return None\n")

    intelligence.build_index(sample_repo)

    with closing(sqlite3.connect(database)) as connection:
        index = next(
            row
            for row in connection.execute("PRAGMA index_list(symbols)")
            if row[1] == "symbols_name"
        )
    assert index[2] == 0
    assert index[4] == 0


def test_matching_version_with_unexpected_unique_index_is_rebuilt(sample_repo: Path):
    database = intelligence.index_path(sample_repo)
    database.parent.mkdir(parents=True)
    intelligence._initialize_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE UNIQUE INDEX symbols_kind_unique ON symbols(kind)")

    intelligence.build_index(sample_repo)

    with closing(sqlite3.connect(database)) as connection:
        names = {
            row[1]
            for row in connection.execute("PRAGMA index_list(symbols)")
        }
    assert names == {"symbols_name"}


def test_matching_version_with_unexpected_trigger_is_rebuilt(sample_repo: Path):
    database = intelligence.index_path(sample_repo)
    database.parent.mkdir(parents=True)
    intelligence._initialize_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TRIGGER suppress_files BEFORE INSERT ON files "
            "BEGIN SELECT RAISE(IGNORE); END"
        )

    stats = intelligence.build_index(sample_repo)

    with closing(sqlite3.connect(database)) as connection:
        triggers = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
        ).fetchall()
        stored = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert triggers == []
    assert stored == stats["total_files"]


def test_matching_version_with_nocase_primary_key_is_rebuilt(sample_repo: Path):
    database = intelligence.index_path(sample_repo)
    database.parent.mkdir(parents=True)
    altered_schema = intelligence.INDEX_SCHEMA.replace(
        "path TEXT PRIMARY KEY", "path TEXT COLLATE NOCASE PRIMARY KEY"
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(altered_schema)
        connection.execute(intelligence.INDEX_USER_VERSION_SQL)

    intelligence.build_index(sample_repo)

    with closing(sqlite3.connect(database)) as connection:
        key_columns = [
            row
            for row in connection.execute(
                intelligence.FILES_PRIMARY_KEY_XINFO_SQL
            )
            if row[5] == 1
        ]
    assert [(row[2], row[3], row[4]) for row in key_columns] == [
        ("path", 0, "BINARY")
    ]


def test_matching_version_with_behavior_changing_check_is_rebuilt(sample_repo: Path):
    database = intelligence.index_path(sample_repo)
    database.parent.mkdir(parents=True)
    altered_schema = intelligence.INDEX_SCHEMA.replace(
        "path TEXT PRIMARY KEY,",
        "path TEXT PRIMARY KEY CHECK(path <> 'app.py'),",
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(altered_schema)
        connection.execute(intelligence.INDEX_USER_VERSION_SQL)

    stats = intelligence.build_index(sample_repo)

    with closing(sqlite3.connect(database)) as connection:
        files_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'files'"
        ).fetchone()[0]
        stored = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert "CHECK" not in files_sql.upper()
    assert stored == stats["total_files"]


def test_failed_schema_rebuild_preserves_existing_index(sample_repo: Path, monkeypatch):
    database = intelligence.index_path(sample_repo)
    database.parent.mkdir(parents=True)
    database.write_bytes(b"existing incompatible index")

    def fail_initialization(_database: Path) -> None:
        raise sqlite3.OperationalError("simulated initialization failure")

    monkeypatch.setattr(intelligence, "_initialize_database", fail_initialization)

    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        intelligence._rebuild_database(database)

    assert database.read_bytes() == b"existing incompatible index"


def test_schema_rebuild_refuses_live_wal_and_removes_closed_sidecars(sample_repo: Path):
    database = intelligence.index_path(sample_repo)
    database.parent.mkdir(parents=True)
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA user_version = 999")
    writer.execute("CREATE TABLE obsolete(value TEXT)")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO obsolete VALUES ('pending')")

    with pytest.raises(intelligence.CacheAccessError, match="active|locked"):
        intelligence._rebuild_database(database)

    writer.rollback()
    writer.close()
    intelligence.build_index(sample_repo)

    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == intelligence.INDEX_SCHEMA_VERSION


def test_schema_rebuild_refuses_active_rollback_journal(sample_repo: Path):
    database = intelligence.index_path(sample_repo)
    database.parent.mkdir(parents=True)
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA user_version = 999")
    writer.execute("CREATE TABLE obsolete(value TEXT)")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO obsolete VALUES ('pending')")
    assert Path(f"{database}-journal").exists()

    with pytest.raises(intelligence.CacheAccessError, match="active|locked|journal"):
        intelligence._rebuild_database(database)

    writer.rollback()
    writer.close()
    intelligence.build_index(sample_repo)


@pytest.mark.parametrize("replacement_kind", ["symlink", "regular"])
def test_index_rejects_database_replaced_before_sqlite_open(
    sample_repo: Path, tmp_path_factory, monkeypatch, replacement_kind: str
):
    intelligence.build_index(sample_repo)
    database = intelligence.index_path(sample_repo)
    target = tmp_path_factory.mktemp("database-race") / "redirected.sqlite"
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
    original_connect = intelligence.sqlite3.connect
    opens = 0

    def replace_before_connect(path, *args, **kwargs):
        nonlocal opens
        if Path(path) == database:
            opens += 1
            if opens == 2:
                database.unlink()
                if replacement_kind == "symlink":
                    database.symlink_to(target)
                else:
                    os.replace(target, database)
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(intelligence.sqlite3, "connect", replace_before_connect)

    with pytest.raises(intelligence.CacheAccessError, match="Unsafe cache path"):
        intelligence.build_index(sample_repo)
    redirected = target if replacement_kind == "symlink" else database
    with closing(sqlite3.connect(redirected)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert tables == {"marker"}


def test_metadata_readers_propagate_unsafe_source_access(
    sample_repo: Path, monkeypatch
):
    (sample_repo / "package.json").write_text("{}")

    def refuse_source(*_args, **_kwargs):
        raise intelligence.SourceAccessError("unsafe metadata source")

    monkeypatch.setattr(intelligence, "read_source_snapshot", refuse_source)
    with pytest.raises(intelligence.SourceAccessError, match="unsafe metadata"):
        intelligence.read_pyproject(sample_repo)

    monkeypatch.setattr(detector, "read_source_snapshot", refuse_source)
    with pytest.raises(intelligence.SourceAccessError, match="unsafe metadata"):
        detector.detect_project(sample_repo)


def test_index_file_limit_returns_structured_json_error(sample_repo: Path):
    result = run_cf(
        sample_repo,
        "index",
        "--max-file-size",
        "16",
        "--format",
        "json",
    )

    assert result.returncode == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["command"] == "index"
    assert payload["error"]["code"] == "file_size_limit_exceeded"
    assert payload["error"]["details"]["limit"] == 16
    assert payload["error"]["details"]["path"] in {
        "app.py",
        "services.py",
        "test_services.py",
        "pyproject.toml",
    }
    assert payload["error"]["details"]["observed"] > 16


def test_index_repository_limit_returns_structured_json_error(sample_repo: Path):
    result = run_cf(
        sample_repo,
        "index",
        "--max-repository-size",
        "1",
        "--format",
        "json",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "repository_size_limit_exceeded"
    assert payload["error"]["details"]["limit"] == 1
    assert payload["error"]["details"]["observed"] > 1


def test_invalid_json_command_arguments_return_structured_error(sample_repo: Path):
    result = run_cf(
        sample_repo,
        "index",
        "--max-file-size",
        "not-an-integer",
        "--format",
        "json",
    )

    assert result.returncode == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["command"] == "index"
    assert payload["error"]["code"] == "invalid_arguments"


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


def test_repository_cache_symlink_cannot_redirect_external_index(
    sample_repo: Path, tmp_path_factory
):
    outside = tmp_path_factory.mktemp("cache-target")
    (sample_repo / ".contextforge").symlink_to(outside, target_is_directory=True)

    result = run_cf(sample_repo, "index", "--format", "json")

    assert result.returncode == 0
    assert not (outside / "index.sqlite").exists()


def test_index_rejects_external_cache_identity_replaced_by_symlink(
    sample_repo: Path, tmp_path_factory, monkeypatch
):
    cache_root = tmp_path_factory.mktemp("cache-root")
    outside = tmp_path_factory.mktemp("cache-race-target")
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(cache_root))
    database = intelligence.index_path(sample_repo)
    database.parent.symlink_to(outside, target_is_directory=True)

    result = run_cf(sample_repo, "index", "--format", "json")

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "unsafe_cache_path"
    assert not (outside / "index.sqlite").exists()


def test_index_rejects_symlinked_cache_ancestor(
    sample_repo: Path, tmp_path_factory, monkeypatch
):
    base = tmp_path_factory.mktemp("cache-ancestor")
    outside = tmp_path_factory.mktemp("cache-ancestor-target")
    linked_ancestor = base / "redirect"
    linked_ancestor.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv(
        "CONTEXTFORGE_CACHE_DIR", str(linked_ancestor / "contextforge")
    )

    result = run_cf(sample_repo, "index", "--format", "json")

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "unsafe_cache_path"
    assert not any(outside.rglob("index.sqlite"))


def test_detector_rejects_non_mapping_package_metadata(tmp_path: Path):
    (tmp_path / "package.json").write_text("[]")

    with pytest.raises(intelligence.SourceAccessError, match="package.json"):
        detector.detect_project(tmp_path)


def test_detector_rejects_non_mapping_dependency_metadata(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"dependencies": []}')

    with pytest.raises(intelligence.SourceAccessError, match="dependencies"):
        detector.detect_project(tmp_path)
