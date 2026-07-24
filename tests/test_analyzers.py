from pathlib import Path
from types import SimpleNamespace

import contextforge.analyzers as analyzer_module
from contextforge.analyzers import (
    Analyzer,
    GenericLexicalAnalyzer,
    JavaScriptAnalyzer,
    PythonAnalyzer,
    Reference,
    Repository,
    SourceFile,
    Symbol,
    TypeScriptAnalyzer,
    analyzer_registry,
    analyzer_for_path,
)
from contextforge import intelligence
from contextforge.intelligence import (
    build_index,
    find_relationships,
    find_symbols,
    repository_brief,
    trace_symbol,
)
from contextforge.scanner import CODE_EXTENSIONS


def test_python_analyzer_implements_protocol_and_preserves_ast_entities():
    source = SourceFile(
        path="services.py",
        content=(
            "from storage import save_user\n\n"
            "class UserService:\n"
            "    async def create(self, email):\n"
            "        return save_user(email)\n"
        ),
    )
    analyzer = PythonAnalyzer()

    assert isinstance(analyzer, Analyzer)
    assert analyzer_for_path(Path(source.path)) is analyzer_for_path(Path("other.py"))

    symbols = analyzer.extract_symbols(source)
    references = analyzer.extract_references(source)

    assert [(item.name, item.kind, item.parent) for item in symbols] == [
        ("UserService", "class", None),
        ("create", "method", "UserService"),
    ]
    assert {item.name for item in references} == {"save_user"}
    call = next(
        item for item in references
        if item.name == "save_user" and item.caller is not None
    )
    assert call.caller == "create"


def test_intelligence_delegates_entity_extraction_to_registered_analyzer(monkeypatch):
    expected_symbol = Symbol("Widget", "class", "widget.ts", 1, 1)
    expected_reference = Reference("render", "widget.ts", 2, "render()")
    received = []

    class StubAnalyzer:
        def extract_symbols(self, source_file):
            received.append(source_file)
            return [expected_symbol]

        def extract_references(self, source_file):
            assert source_file is received[0]
            return [expected_reference]

    monkeypatch.setattr(
        intelligence,
        "analyzer_for_path",
        lambda _path: StubAnalyzer(),
        raising=False,
    )

    symbols, references = intelligence.extract_entities("widget.ts", "render()")

    assert intelligence.Symbol is Symbol
    assert intelligence.Reference is Reference
    assert symbols == [expected_symbol]
    assert references == [expected_reference]
    assert received == [SourceFile("widget.ts", "render()")]


def test_unknown_language_uses_generic_lexical_fallback():
    analyzer = analyzer_for_path(Path("worker.go"))
    source = SourceFile(
        "worker.go",
        "func process() {\n    persist()\n}\n",
    )

    assert isinstance(analyzer, GenericLexicalAnalyzer)
    assert [item.name for item in analyzer.extract_symbols(source)] == ["process"]
    assert [item.name for item in analyzer.extract_references(source)] == ["persist"]


def test_javascript_tree_sitter_analyzer_extracts_native_symbols_and_calls():
    analyzer = analyzer_for_path(Path("service.js"))
    source = SourceFile(
        "service.js",
        "import { save } from './db.js';\n"
        "export class Service {\n"
        "  run(value) { return save(value); }\n"
        "}\n"
        "export const handler = async (request) => new Service().run(request);\n",
    )

    assert isinstance(analyzer, JavaScriptAnalyzer)
    assert [(item.name, item.kind, item.parent) for item in analyzer.extract_symbols(source)] == [
        ("Service", "class", None),
        ("run", "method", "Service"),
        ("handler", "function", None),
    ]
    assert {item.name for item in analyzer.extract_references(source)} >= {
        "save",
        "Service",
        "run",
    }


def test_typescript_tree_sitter_analyzer_extracts_interfaces_and_typed_methods():
    analyzer = analyzer_for_path(Path("service.ts"))
    source = SourceFile(
        "service.ts",
        "interface Repository { save(value: string): void }\n"
        "export class UserService extends BaseService {\n"
        "  create(value: string): void { persist(value); }\n"
        "}\n",
    )

    assert isinstance(analyzer, TypeScriptAnalyzer)
    assert [(item.name, item.kind, item.parent) for item in analyzer.extract_symbols(source)] == [
        ("Repository", "interface", None),
        ("save", "method", "Repository"),
        ("UserService", "class", None),
        ("create", "method", "UserService"),
    ]
    assert "persist" in {item.name for item in analyzer.extract_references(source)}


def test_python_analyzer_extracts_import_call_and_inheritance_relationships():
    source = SourceFile(
        "services.py",
        "from storage.repository import save_user\n"
        "from . import local_service\n"
        "from ..shared import validate\n"
        "class UserService(BaseService):\n"
        "    def create(self, email):\n"
        "        return save_user(email)\n",
    )

    relationships = PythonAnalyzer().extract_relationships(source)

    assert {(item.source, item.target, item.kind) for item in relationships} == {
        ("services.py", "storage.repository", "imports"),
        ("services.py", ".", "imports"),
        ("services.py", "..shared", "imports"),
        ("UserService", "BaseService", "inherits"),
        ("create", "save_user", "calls"),
    }


def test_typescript_analyzer_extracts_import_export_call_and_inheritance_relationships():
    source = SourceFile(
        "service.ts",
        "import { persist } from './repository';\n"
        "export class UserService extends BaseService {\n"
        "  create(value: string): void { persist(value); }\n"
        "}\n",
    )

    relationships = TypeScriptAnalyzer().extract_relationships(source)

    assert {(item.source, item.target, item.kind) for item in relationships} == {
        ("service.ts", "./repository", "imports"),
        ("service.ts", "UserService", "exports"),
        ("UserService", "BaseService", "inherits"),
        ("create", "persist", "calls"),
    }


def test_javascript_analyzer_extracts_named_and_reexport_relationships():
    source = SourceFile(
        "exports.js",
        "const helper = () => 1;\n"
        "export { helper as run };\n"
        "export { save } from './store.js';\n"
        "export * from './all.js';\n"
        "export default function named() {}\n"
        "export * as utilities from './utilities.js';\n",
    )

    relationships = JavaScriptAnalyzer().extract_relationships(source)

    assert {(item.source, item.target, item.kind) for item in relationships} == {
        ("exports.js", "run", "exports"),
        ("exports.js", "save", "exports"),
        ("exports.js", "./store.js", "imports"),
        ("exports.js", "*", "exports"),
        ("exports.js", "./all.js", "imports"),
        ("exports.js", "default", "exports"),
        ("exports.js", "utilities", "exports"),
        ("exports.js", "./utilities.js", "imports"),
    }


def test_commonjs_analyzer_extracts_require_and_module_exports():
    source = SourceFile(
        "module.cjs",
        "const dependency = require('./dependency.cjs');\n"
        "module.exports = dependency;\n"
        "exports.handler = handler;\n",
    )

    relationships = JavaScriptAnalyzer().extract_relationships(source)

    assert {(item.source, item.target, item.kind) for item in relationships} >= {
        ("module.cjs", "./dependency.cjs", "imports"),
        ("module.cjs", "default", "exports"),
        ("module.cjs", "handler", "exports"),
    }


def test_typescript_abstract_and_interface_heritage_is_indexed():
    source = SourceFile(
        "contracts.ts",
        "export abstract class Service extends Base implements Runnable {\n"
        "  abstract run(): void;\n"
        "}\n"
        "interface Child extends Parent, Other {}\n",
    )
    analyzer = TypeScriptAnalyzer()

    assert [(item.name, item.kind, item.parent) for item in analyzer.extract_symbols(source)] == [
        ("Service", "class", None),
        ("run", "method", "Service"),
        ("Child", "interface", None),
    ]
    assert {(item.source, item.target, item.kind) for item in analyzer.extract_relationships(source)} >= {
        ("Service", "Base", "inherits"),
        ("Service", "Runnable", "implements"),
        ("Child", "Parent", "inherits"),
        ("Child", "Other", "inherits"),
    }


def test_index_schema_v2_persists_and_replaces_relationships(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "service.py"
    source.write_text(
        "from storage import persist\n"
        "def create():\n"
        "    return persist()\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    first = build_index(repository)

    assert intelligence.INDEX_SCHEMA_VERSION == 2
    assert first["relationships"] == 2
    assert {(item["source"], item["target"], item["kind"]) for item in find_relationships(repository)} == {
        ("service.py", "storage", "imports"),
        ("create", "persist", "calls"),
    }

    source.write_text("def create():\n    return None\n", encoding="utf-8")
    second = build_index(repository)

    assert second["relationships"] == 0
    assert find_relationships(repository) == []


def test_source_file_preserves_exact_bounded_snapshot_for_native_parsers():
    snapshot = b"// invalid byte: \xff\nexport function run() { persist(); }\n"
    source = SourceFile("service.js", snapshot)

    symbols = JavaScriptAnalyzer().extract_symbols(source)

    assert source.snapshot is snapshot
    assert [(item.name, item.line) for item in symbols] == [("run", 2)]


def test_scanner_extensions_cover_every_native_javascript_registration():
    assert {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"} <= CODE_EXTENSIONS


def test_analyzers_detect_projects_and_discover_entry_points_from_safe_snapshots():
    repository = Repository(
        Path("/untrusted/repository"),
        {
            "pyproject.toml": SourceFile(
                "pyproject.toml",
                "[project]\nname='demo'\n[project.scripts]\ncf-demo='demo.cli:main'\n",
            ),
            "package.json": SourceFile(
                "package.json",
                '{"name":"web","main":"dist/index.js","scripts":{"dev":"next dev"}}',
            ),
        },
        ("src/index.ts",),
    )
    analyzers = analyzer_registry()

    detections = {item.name: item.detect(repository) for item in analyzers}
    entry_points = [
        item
        for analyzer in analyzers
        for item in analyzer.discover_entry_points(repository)
    ]

    assert [item.name for item in analyzers] == [
        "python",
        "javascript",
        "typescript",
        "generic",
    ]
    assert detections["python"].detected is True
    assert detections["javascript"].detected is True
    assert detections["typescript"].detected is True
    assert {(item.name, item.target, item.path) for item in entry_points} >= {
        ("cf-demo", "demo.cli:main", "pyproject.toml"),
        ("main", "dist/index.js", "package.json"),
        ("dev", "next dev", "package.json"),
    }


def test_repository_brief_preserves_v1_payload_while_adding_analyzer_evidence(
    tmp_path,
    monkeypatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "pyproject.toml").write_text(
        "[project]\nname='demo'\n[project.scripts]\ncf-demo='demo.cli:main'\n",
        encoding="utf-8",
    )
    (repository / "demo.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    result = repository_brief(repository, ["Python project"], {})

    assert result["detections"] == ["Python project"]
    assert result["entry_points"] == [
        {
            "command": "cf-demo",
            "target": "demo.cli:main",
            "module": "demo.cli",
            "symbol": "main",
        }
    ]
    assert result["analyzers"] == [
        {
            "name": "python",
            "confidence": "high",
            "evidence": ["pyproject.toml"],
        }
    ]


def test_analyzer_fingerprint_mismatch_atomically_invalidates_incremental_index(
    tmp_path,
    monkeypatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "service.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))
    build_index(repository)
    database = intelligence.index_path(repository)

    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE metadata SET value = 'obsolete' WHERE key = 'analyzer_fingerprint'"
        )

    rebuilt = build_index(repository)

    assert rebuilt["indexed"] == 1
    with sqlite3.connect(database) as connection:
        fingerprint = connection.execute(
            "SELECT value FROM metadata WHERE key = 'analyzer_fingerprint'"
        ).fetchone()[0]
    assert fingerprint == intelligence.ANALYZER_FINGERPRINT


def test_typescript_analysis_flows_through_secure_incremental_index(
    tmp_path,
    monkeypatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "service.ts").write_text(
        "import { persist } from './repository';\n"
        "export class UserService {\n"
        "  create(): void { persist(); }\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    stats = build_index(repository)

    assert stats["symbols"] == 2
    assert stats["relationships"] >= 3
    assert [item["name"] for item in find_symbols(repository, "UserService")] == [
        "UserService"
    ]
    assert {(item["source"], item["target"], item["kind"]) for item in find_relationships(repository)} >= {
        ("service.ts", "./repository", "imports"),
        ("service.ts", "UserService", "exports"),
        ("create", "persist", "calls"),
    }


def test_tree_sitter_error_tree_uses_conservative_lexical_fallback(monkeypatch):
    class ErrorParser:
        def __init__(self, _language):
            pass

        def parse(self, _snapshot):
            return SimpleNamespace(root_node=SimpleNamespace(has_error=True))

    monkeypatch.setattr(analyzer_module, "Parser", ErrorParser)
    source = SourceFile(
        "service.js",
        "function recover(value) { return persist(value); }\n",
    )
    analyzer = JavaScriptAnalyzer()

    assert [item.name for item in analyzer.extract_symbols(source)] == ["recover"]
    assert [item.name for item in analyzer.extract_references(source)] == ["persist"]
    assert analyzer.extract_relationships(source) == []


def test_tree_sitter_parser_exception_uses_conservative_lexical_fallback(monkeypatch):
    class FailingParser:
        def __init__(self, _language):
            pass

        def parse(self, _snapshot):
            raise ValueError("parser rejected source")

    monkeypatch.setattr(analyzer_module, "Parser", FailingParser)
    source = SourceFile(
        "service.ts",
        "function recover(value) { return persist(value); }\n",
    )
    analyzer = TypeScriptAnalyzer()

    assert [item.name for item in analyzer.extract_symbols(source)] == ["recover"]
    assert [item.name for item in analyzer.extract_references(source)] == ["persist"]
    assert analyzer.extract_relationships(source) == []


def test_trace_disambiguates_same_named_methods_by_symbol_range(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "service.py").write_text(
        "class A:\n"
        "    def run(self):\n"
        "        foo()\n"
        "class B:\n"
        "    def run(self):\n"
        "        bar()\n"
        "def foo(): pass\n"
        "def bar(): pass\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    _nodes, edges = trace_symbol(repository, "run")

    assert {(edge["to"], edge["from_line"]) for edge in edges} == {
        ("foo", 2),
        ("bar", 5),
    }
