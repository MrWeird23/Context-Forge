from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from tree_sitter import Language, Node, Parser
import tree_sitter_javascript
import tree_sitter_typescript


@dataclass(frozen=True, init=False)
class SourceFile:
    path: str
    snapshot: bytes

    def __init__(self, path: str, content: str | bytes):
        object.__setattr__(self, "path", path)
        object.__setattr__(
            self,
            "snapshot",
            content if isinstance(content, bytes) else content.encode("utf-8"),
        )

    @property
    def content(self) -> str:
        return self.snapshot.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Repository:
    root: Path
    manifests: dict[str, SourceFile]
    paths: tuple[str, ...] = ()

    def manifest(self, name: str) -> SourceFile | None:
        return self.manifests.get(name)


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


@dataclass(frozen=True)
class Relationship:
    source: str
    target: str
    kind: str
    path: str
    line: int
    confidence: str = "high"


@dataclass(frozen=True)
class EntryPoint:
    name: str
    target: str
    path: str
    line: int | None = None


@dataclass(frozen=True)
class DetectionResult:
    analyzer: str
    detected: bool
    confidence: str
    evidence: tuple[str, ...] = ()


@runtime_checkable
class Analyzer(Protocol):
    name: str

    def detect(self, repository: Repository) -> DetectionResult: ...

    def extract_symbols(self, source_file: SourceFile) -> list[Symbol]: ...

    def extract_references(self, source_file: SourceFile) -> list[Reference]: ...

    def extract_relationships(self, source_file: SourceFile) -> list[Relationship]: ...

    def discover_entry_points(self, repository: Repository) -> list[EntryPoint]: ...


def _toml_document(source_file: SourceFile | None) -> dict:
    if source_file is None:
        return {}
    try:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10
            from importlib import import_module

            tomllib = import_module("tomli")
        document = tomllib.loads(source_file.content)
        return document if isinstance(document, dict) else {}
    except Exception:
        return {}


def _json_document(source_file: SourceFile | None) -> dict:
    if source_file is None:
        return {}
    try:
        document = json.loads(source_file.content)
        return document if isinstance(document, dict) else {}
    except (UnicodeError, json.JSONDecodeError):
        return {}


def _source_line(lines: list[str], line: int) -> str:
    return lines[line - 1].strip()[:240] if 0 < line <= len(lines) else ""


def _python_entities(source_file: SourceFile) -> tuple[tuple[Symbol, ...], tuple[Reference, ...]]:
    try:
        tree = ast.parse(source_file.content)
    except SyntaxError:
        return (), ()
    lines = source_file.content.splitlines()
    symbols: list[Symbol] = []
    references: list[Reference] = []
    scope: list[str] = []

    class Visitor(ast.NodeVisitor):
        @staticmethod
        def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
            try:
                return ast.unparse(node.args)
            except Exception:
                return ""

        def visit_ClassDef(self, node: ast.ClassDef):
            symbols.append(
                Symbol(
                    node.name,
                    "class",
                    source_file.path,
                    node.lineno,
                    getattr(node, "end_lineno", node.lineno),
                    scope[-1] if scope else None,
                )
            )
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            symbols.append(
                Symbol(
                    node.name,
                    "method" if scope else "function",
                    source_file.path,
                    node.lineno,
                    getattr(node, "end_lineno", node.lineno),
                    scope[-1] if scope else None,
                    self._signature(node),
                )
            )
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef):
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
            self._visit_function(node)

        def visit_Call(self, node: ast.Call):
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                name = None
            if name:
                references.append(
                    Reference(
                        name,
                        source_file.path,
                        node.lineno,
                        _source_line(lines, node.lineno),
                        scope[-1] if scope else None,
                    )
                )
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom):
            for alias in node.names:
                references.append(
                    Reference(
                        alias.name,
                        source_file.path,
                        node.lineno,
                        _source_line(lines, node.lineno),
                        scope[-1] if scope else None,
                    )
                )

    Visitor().visit(tree)
    return tuple(symbols), tuple(references)


def _python_relationships(source_file: SourceFile) -> tuple[Relationship, ...]:
    try:
        tree = ast.parse(source_file.content)
    except SyntaxError:
        return ()
    relationships: list[Relationship] = []
    scope: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import):
            for alias in node.names:
                relationships.append(
                    Relationship(
                        source_file.path,
                        alias.name,
                        "imports",
                        source_file.path,
                        node.lineno,
                    )
                )

        def visit_ImportFrom(self, node: ast.ImportFrom):
            target = "." * node.level + (node.module or "")
            if target:
                relationships.append(
                    Relationship(
                        source_file.path,
                        target,
                        "imports",
                        source_file.path,
                        node.lineno,
                    )
                )

        def visit_ClassDef(self, node: ast.ClassDef):
            for base in node.bases:
                relationships.append(
                    Relationship(
                        node.name,
                        ast.unparse(base),
                        "inherits",
                        source_file.path,
                        node.lineno,
                    )
                )
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef):
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
            self._visit_function(node)

        def visit_Call(self, node: ast.Call):
            if isinstance(node.func, ast.Name):
                target = node.func.id
            elif isinstance(node.func, ast.Attribute):
                target = node.func.attr
            else:
                target = ""
            if target:
                relationships.append(
                    Relationship(
                        scope[-1] if scope else source_file.path,
                        target,
                        "calls",
                        source_file.path,
                        node.lineno,
                    )
                )
            self.generic_visit(node)

    Visitor().visit(tree)
    return tuple(relationships)


class PythonAnalyzer:
    name = "python"

    def detect(self, repository: Repository) -> DetectionResult:
        evidence = tuple(
            name for name in ("pyproject.toml", "setup.py", "requirements.txt")
            if name in repository.manifests
        )
        return DetectionResult(self.name, bool(evidence), "high" if evidence else "low", evidence)

    def extract_symbols(self, source_file: SourceFile) -> list[Symbol]:
        return list(_python_entities(source_file)[0])

    def extract_references(self, source_file: SourceFile) -> list[Reference]:
        return list(_python_entities(source_file)[1])

    def extract_relationships(self, source_file: SourceFile) -> list[Relationship]:
        return list(_python_relationships(source_file))

    def discover_entry_points(self, repository: Repository) -> list[EntryPoint]:
        project = _toml_document(repository.manifest("pyproject.toml")).get(
            "project", {}
        )
        scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
        if not isinstance(scripts, dict):
            return []
        return [
            EntryPoint(str(command), str(target), "pyproject.toml")
            for command, target in sorted(scripts.items())
        ]


class GenericLexicalAnalyzer:
    name = "generic"
    _definition_patterns = (
        ("class", re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")),
        (
            "function",
            re.compile(
                r"\b(?:function|def|fn|func)\s+([A-Za-z_$][\w$]*)\s*\("
            ),
        ),
        (
            "function",
            re.compile(
                r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
                r"(?:async\s*)?\([^)]*\)\s*=>"
            ),
        ),
    )
    _call_pattern = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
    _keywords = {
        "if", "for", "while", "switch", "catch", "return",
        "function", "def", "fn", "func", "class",
    }

    def detect(self, repository: Repository) -> DetectionResult:
        return DetectionResult(self.name, False, "low")

    def extract_symbols(self, source_file: SourceFile) -> list[Symbol]:
        symbols: list[Symbol] = []
        for number, line in enumerate(source_file.content.splitlines(), 1):
            for kind, pattern in self._definition_patterns:
                match = pattern.search(line)
                if match:
                    symbols.append(
                        Symbol(
                            match.group(1),
                            kind,
                            source_file.path,
                            number,
                            number,
                            signature=line.strip()[:240],
                        )
                    )
                    break
        return symbols

    def extract_references(self, source_file: SourceFile) -> list[Reference]:
        references: list[Reference] = []
        for number, line in enumerate(source_file.content.splitlines(), 1):
            defined = {
                match.group(1)
                for _kind, pattern in self._definition_patterns
                if (match := pattern.search(line))
            }
            for match in self._call_pattern.finditer(line):
                name = match.group(1)
                if name not in self._keywords and name not in defined:
                    references.append(
                        Reference(
                            name,
                            source_file.path,
                            number,
                            line.strip()[:240],
                        )
                    )
        return references

    def extract_relationships(self, source_file: SourceFile) -> list[Relationship]:
        return []

    def discover_entry_points(self, repository: Repository) -> list[EntryPoint]:
        return []


def _node_text(snapshot: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return snapshot[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _call_target(snapshot: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    if node.type in {"identifier", "type_identifier"}:
        return _node_text(snapshot, node)
    if node.type in {"member_expression", "subscript_expression"}:
        return _node_text(snapshot, node.child_by_field_name("property"))
    return _node_text(snapshot, node).split(".")[-1]


def _tree_sitter_entities(
    source_file: SourceFile,
    language_name: str,
) -> tuple[tuple[Symbol, ...], tuple[Reference, ...]]:
    snapshot = source_file.snapshot
    try:
        if language_name == "javascript":
            language = Language(tree_sitter_javascript.language())
        elif language_name == "tsx":
            language = Language(tree_sitter_typescript.language_tsx())
        else:
            language = Language(tree_sitter_typescript.language_typescript())
        tree = Parser(language).parse(snapshot)
    except Exception:
        fallback = GenericLexicalAnalyzer()
        return (
            tuple(fallback.extract_symbols(source_file)),
            tuple(fallback.extract_references(source_file)),
        )
    if tree.root_node.has_error:
        fallback = GenericLexicalAnalyzer()
        return (
            tuple(fallback.extract_symbols(source_file)),
            tuple(fallback.extract_references(source_file)),
        )
    lines = source_file.content.splitlines()
    symbols: list[Symbol] = []
    references: list[Reference] = []

    def symbol(node: Node, name_node: Node | None, kind: str, parent: str | None) -> str:
        name = _node_text(snapshot, name_node)
        if name:
            symbols.append(
                Symbol(
                    name,
                    kind,
                    source_file.path,
                    node.start_point.row + 1,
                    node.end_point.row + 1,
                    parent,
                    _source_line(lines, node.start_point.row + 1),
                )
            )
        return name

    def visit(node: Node, scope: str | None = None) -> None:
        if node.type in {"class_declaration", "abstract_class_declaration", "class"}:
            name = symbol(node, node.child_by_field_name("name"), "class", scope)
            for child in node.children:
                visit(child, name or scope)
            return
        if node.type == "interface_declaration":
            name = symbol(node, node.child_by_field_name("name"), "interface", scope)
            for child in node.children:
                visit(child, name or scope)
            return
        if node.type in {
            "method_definition",
            "method_signature",
            "abstract_method_signature",
        }:
            name = symbol(node, node.child_by_field_name("name"), "method", scope)
            for child in node.children:
                visit(child, name or scope)
            return
        if node.type in {"function_declaration", "generator_function_declaration"}:
            name = symbol(node, node.child_by_field_name("name"), "function", scope)
            for child in node.children:
                visit(child, name or scope)
            return
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in {"arrow_function", "function_expression"}:
                name = symbol(node, node.child_by_field_name("name"), "function", scope)
                for child in node.children:
                    visit(child, name or scope)
                return
        if node.type == "call_expression":
            name = _call_target(snapshot, node.child_by_field_name("function"))
            if name:
                line = node.start_point.row + 1
                references.append(
                    Reference(
                        name,
                        source_file.path,
                        line,
                        _source_line(lines, line),
                        scope,
                    )
                )
        elif node.type == "new_expression":
            name = _call_target(snapshot, node.child_by_field_name("constructor"))
            if name:
                line = node.start_point.row + 1
                references.append(
                    Reference(
                        name,
                        source_file.path,
                        line,
                        _source_line(lines, line),
                        scope,
                    )
                )
        for child in node.children:
            visit(child, scope)

    visit(tree.root_node)
    return tuple(symbols), tuple(references)


def _tree_sitter_relationships(
    source_file: SourceFile,
    language_name: str,
) -> tuple[Relationship, ...]:
    snapshot = source_file.snapshot
    try:
        if language_name == "javascript":
            language = Language(tree_sitter_javascript.language())
        elif language_name == "tsx":
            language = Language(tree_sitter_typescript.language_tsx())
        else:
            language = Language(tree_sitter_typescript.language_typescript())
        tree = Parser(language).parse(snapshot)
    except Exception:
        return ()
    if tree.root_node.has_error:
        return ()
    relationships: list[Relationship] = []

    def add(source: str, target: str, kind: str, node: Node) -> None:
        if target:
            relationships.append(
                Relationship(
                    source,
                    target,
                    kind,
                    source_file.path,
                    node.start_point.row + 1,
                )
            )

    def declaration_names(node: Node | None) -> list[str]:
        if node is None:
            return []
        if node.type in {"lexical_declaration", "variable_declaration"}:
            return [
                name
                for child in node.named_children
                if child.type == "variable_declarator"
                if (name := _node_text(snapshot, child.child_by_field_name("name")))
            ]
        name = _node_text(snapshot, node.child_by_field_name("name"))
        return [name] if name else []

    def visit(node: Node, scope: str | None = None) -> None:
        if node.type == "import_statement":
            target = _node_text(snapshot, node.child_by_field_name("source"))
            add(source_file.path, target.strip("'\""), "imports", node)
        if node.type == "export_statement":
            statement = _node_text(snapshot, node).lstrip()
            exported_names = (
                ["default"]
                if statement.startswith("export default")
                else declaration_names(node.child_by_field_name("declaration"))
            )
            for name in exported_names:
                add(source_file.path, name, "exports", node)
            for child in node.named_children:
                if child.type == "export_clause":
                    for specifier in child.named_children:
                        public_name = _node_text(
                            snapshot,
                            specifier.child_by_field_name("alias")
                            or specifier.child_by_field_name("name"),
                        )
                        add(source_file.path, public_name, "exports", specifier)
                        if public_name:
                            exported_names.append(public_name)
                elif child.type == "namespace_export" and child.named_children:
                    public_name = _node_text(snapshot, child.named_children[0])
                    add(source_file.path, public_name, "exports", child)
                    if public_name:
                        exported_names.append(public_name)
            target = _node_text(snapshot, node.child_by_field_name("source"))
            if target:
                add(source_file.path, target.strip("'\""), "imports", node)
            if not exported_names and statement.startswith("export *"):
                add(source_file.path, "*", "exports", node)
        if node.type in {"class_declaration", "abstract_class_declaration", "class"}:
            name = _node_text(snapshot, node.child_by_field_name("name"))
            for child in node.named_children:
                if child.type == "class_heritage":
                    for clause in child.named_children:
                        if clause.type == "extends_clause":
                            add(
                                name,
                                _node_text(
                                    snapshot,
                                    clause.child_by_field_name("value"),
                                ),
                                "inherits",
                                clause,
                            )
                        elif clause.type == "implements_clause":
                            for implemented in clause.named_children:
                                add(
                                    name,
                                    _node_text(snapshot, implemented),
                                    "implements",
                                    implemented,
                                )
            for child in node.children:
                visit(child, name or scope)
            return
        if node.type == "interface_declaration":
            name = _node_text(snapshot, node.child_by_field_name("name"))
            for child in node.named_children:
                if child.type == "extends_type_clause":
                    for parent in child.named_children:
                        add(
                            name,
                            _node_text(snapshot, parent),
                            "inherits",
                            parent,
                        )
            for child in node.children:
                visit(child, name or scope)
            return
        if node.type in {
            "method_definition",
            "method_signature",
            "abstract_method_signature",
        }:
            name = _node_text(snapshot, node.child_by_field_name("name"))
            for child in node.children:
                visit(child, name or scope)
            return
        if node.type in {"function_declaration", "generator_function_declaration"}:
            name = _node_text(snapshot, node.child_by_field_name("name"))
            for child in node.children:
                visit(child, name or scope)
            return
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in {"arrow_function", "function_expression"}:
                name = _node_text(snapshot, node.child_by_field_name("name"))
                for child in node.children:
                    visit(child, name or scope)
                return
        if node.type == "call_expression":
            target = _call_target(snapshot, node.child_by_field_name("function"))
            if target == "require":
                arguments = node.child_by_field_name("arguments")
                dependency = (
                    _node_text(snapshot, arguments.named_children[0]).strip("'\"")
                    if arguments is not None and arguments.named_children
                    else ""
                )
                add(source_file.path, dependency, "imports", node)
            else:
                add(scope or source_file.path, target, "calls", node)
        elif node.type == "new_expression":
            add(
                scope or source_file.path,
                _call_target(snapshot, node.child_by_field_name("constructor")),
                "calls",
                node,
            )
        elif node.type == "assignment_expression":
            left = _node_text(snapshot, node.child_by_field_name("left"))
            if left == "module.exports":
                add(source_file.path, "default", "exports", node)
            elif left.startswith("module.exports."):
                add(source_file.path, left.removeprefix("module.exports."), "exports", node)
            elif left.startswith("exports."):
                add(source_file.path, left.removeprefix("exports."), "exports", node)
        for child in node.children:
            visit(child, scope)

    visit(tree.root_node)
    return tuple(relationships)


class JavaScriptAnalyzer:
    name = "javascript"

    def detect(self, repository: Repository) -> DetectionResult:
        evidence = ("package.json",) if "package.json" in repository.manifests else ()
        return DetectionResult(self.name, bool(evidence), "high" if evidence else "low", evidence)

    def extract_symbols(self, source_file: SourceFile) -> list[Symbol]:
        return list(_tree_sitter_entities(source_file, "javascript")[0])

    def extract_references(self, source_file: SourceFile) -> list[Reference]:
        return list(_tree_sitter_entities(source_file, "javascript")[1])

    def extract_relationships(self, source_file: SourceFile) -> list[Relationship]:
        return list(_tree_sitter_relationships(source_file, "javascript"))

    def discover_entry_points(self, repository: Repository) -> list[EntryPoint]:
        package = _json_document(repository.manifest("package.json"))
        entry_points: list[EntryPoint] = []
        for name in ("main", "module"):
            target = package.get(name)
            if isinstance(target, str):
                entry_points.append(EntryPoint(name, target, "package.json"))
        binaries = package.get("bin", {})
        if isinstance(binaries, str):
            entry_points.append(
                EntryPoint(str(package.get("name", "bin")), binaries, "package.json")
            )
        elif isinstance(binaries, dict):
            entry_points.extend(
                EntryPoint(str(name), str(target), "package.json")
                for name, target in sorted(binaries.items())
            )
        scripts = package.get("scripts", {})
        if isinstance(scripts, dict):
            entry_points.extend(
                EntryPoint(str(name), str(target), "package.json")
                for name, target in sorted(scripts.items())
            )
        return entry_points


class TypeScriptAnalyzer(JavaScriptAnalyzer):
    name = "typescript"

    def detect(self, repository: Repository) -> DetectionResult:
        evidence = []
        if "tsconfig.json" in repository.manifests:
            evidence.append("tsconfig.json")
        if any(
            Path(path).suffix in {".ts", ".tsx", ".mts", ".cts"}
            for path in repository.paths
        ):
            evidence.append("TypeScript source files")
        return DetectionResult(
            self.name,
            bool(evidence),
            "high" if evidence else "low",
            tuple(evidence),
        )

    def discover_entry_points(self, repository: Repository) -> list[EntryPoint]:
        return []

    @staticmethod
    def _language(source_file: SourceFile) -> str:
        return "tsx" if Path(source_file.path).suffix == ".tsx" else "typescript"

    def extract_symbols(self, source_file: SourceFile) -> list[Symbol]:
        return list(_tree_sitter_entities(source_file, self._language(source_file))[0])

    def extract_references(self, source_file: SourceFile) -> list[Reference]:
        return list(_tree_sitter_entities(source_file, self._language(source_file))[1])

    def extract_relationships(self, source_file: SourceFile) -> list[Relationship]:
        return list(
            _tree_sitter_relationships(source_file, self._language(source_file))
        )


_PYTHON_ANALYZER = PythonAnalyzer()
_GENERIC_ANALYZER = GenericLexicalAnalyzer()
_JAVASCRIPT_ANALYZER = JavaScriptAnalyzer()
_TYPESCRIPT_ANALYZER = TypeScriptAnalyzer()


def analyzer_registry() -> tuple[Analyzer, ...]:
    return (
        _PYTHON_ANALYZER,
        _JAVASCRIPT_ANALYZER,
        _TYPESCRIPT_ANALYZER,
        _GENERIC_ANALYZER,
    )


def analyzer_for_path(path: Path) -> Analyzer:
    if path.suffix == ".py":
        return _PYTHON_ANALYZER
    if path.suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        return _JAVASCRIPT_ANALYZER
    if path.suffix in {".ts", ".tsx", ".mts", ".cts"}:
        return _TYPESCRIPT_ANALYZER
    return _GENERIC_ANALYZER
