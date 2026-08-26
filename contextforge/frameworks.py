from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from tree_sitter import Node, Tree

from .analyzers import SourceFile, _javascript_tree


@dataclass(frozen=True)
class FrameworkEntity:
    framework: str
    kind: str
    name: str
    target: str | None
    path: str
    line: int
    end_line: int
    confidence: str = "high"
    attributes: tuple[tuple[str, str], ...] = ()


@runtime_checkable
class FrameworkAnalyzer(Protocol):
    name: str

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]: ...


@dataclass(frozen=True)
class _ConstructorBinding:
    kind: str


@dataclass(frozen=True)
class _ApplicationBinding:
    prefix: str


_Binding = _ConstructorBinding | _ApplicationBinding

_CONTROL_FLOW_BOUNDARIES = (
    ast.AsyncFor,
    ast.AsyncWith,
    ast.For,
    ast.If,
    ast.Match,
    ast.Try,
    ast.While,
    ast.With,
) + tuple(
    node_type
    for node_type in (getattr(ast, "TryStar", None),)
    if node_type is not None
)


def _python_target_names(target: ast.expr) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, (ast.List, ast.Tuple)):
        return tuple(
            name for item in target.elts for name in _python_target_names(item)
        )
    if isinstance(target, ast.Starred):
        return _python_target_names(target.value)
    return ()


def _python_assigned_names(statement: ast.stmt) -> set[str]:
    if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return {statement.name}
    if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = (
            statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        )
        return {
            name for target in targets for name in _python_target_names(target)
        }
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        return {alias.asname or alias.name.split(".", 1)[0] for alias in statement.names}
    if isinstance(statement, ast.Delete):
        return {
            name for target in statement.targets for name in _python_target_names(target)
        }
    return set()


def _python_dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _python_dotted_name(node.value)
        return f"{owner}.{node.attr}" if owner is not None else None
    return None


class _ExpressionBindingCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.names.update(FastAPIAnalyzer._target_names(node.target))
        self.visit(node.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)


def _python_expression_binding_names(expression: ast.expr) -> set[str]:
    collector = _ExpressionBindingCollector()
    collector.visit(expression)
    return collector.names


def _python_evaluated_expressions(statement: ast.stmt) -> list[ast.expr]:
    expressions: list[ast.expr] = []
    if isinstance(statement, ast.Expr):
        expressions.append(statement.value)
    elif isinstance(statement, (ast.Assign, ast.AugAssign)):
        expressions.append(statement.value)
    elif isinstance(statement, ast.AnnAssign):
        expressions.append(statement.annotation)
        if statement.value is not None:
            expressions.append(statement.value)
    elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        arguments = [
            *statement.args.posonlyargs,
            *statement.args.args,
            *statement.args.kwonlyargs,
        ]
        if statement.args.vararg is not None:
            arguments.append(statement.args.vararg)
        if statement.args.kwarg is not None:
            arguments.append(statement.args.kwarg)
        expressions.extend(statement.decorator_list)
        expressions.extend(statement.args.defaults)
        expressions.extend(
            default
            for default in statement.args.kw_defaults
            if default is not None
        )
        expressions.extend(
            argument.annotation
            for argument in arguments
            if argument.annotation is not None
        )
        if statement.returns is not None:
            expressions.append(statement.returns)
    elif isinstance(statement, ast.ClassDef):
        expressions.extend(statement.decorator_list)
        expressions.extend(statement.bases)
        expressions.extend(keyword.value for keyword in statement.keywords)
    return expressions


def _python_evaluated_binding_names(statement: ast.stmt) -> set[str]:
    names: set[str] = set()
    for expression in _python_evaluated_expressions(statement):
        names.update(_python_expression_binding_names(expression))
    return names


class _CalledFunctionEffectCollector(ast.NodeVisitor):
    def __init__(self, effects: dict[str, set[str]]) -> None:
        self.effects = effects
        self.names: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.names.update(self.effects.get(node.func.id, ()))
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name):
                self.names.update(self.effects.get(decorator.id, ()))
        for expression in _python_evaluated_expressions(node):
            self.visit(expression)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name):
                self.names.update(self.effects.get(decorator.id, ()))
        for expression in _python_evaluated_expressions(node):
            self.visit(expression)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in _python_evaluated_expressions(node):
            self.visit(expression)
        for statement in node.body:
            self.visit(statement)


def _python_called_function_effects(
    statement: ast.stmt, effects: dict[str, set[str]]
) -> set[str]:
    collector = _CalledFunctionEffectCollector(effects)
    collector.visit(statement)
    return collector.names


class _CalledFunctionNameCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.names.add(node.func.id)
        elif isinstance(node.func, ast.Lambda):
            self.visit(node.func.body)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.update(
            decorator.id
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Name)
        )
        for expression in _python_evaluated_expressions(node):
            self.visit(expression)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.update(
            decorator.id
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Name)
        )
        for expression in _python_evaluated_expressions(node):
            self.visit(expression)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in _python_evaluated_expressions(node):
            self.visit(expression)
        for statement in node.body:
            self.visit(statement)


def _python_function_effect_summaries(
    statements: list[ast.stmt],
) -> dict[str, set[str]]:
    definitions = {
        statement.name: statement
        for statement in statements
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    summaries: dict[str, set[str]] = {}
    calls: dict[str, set[str]] = {}
    for name, definition in definitions.items():
        globals_used = _ClassGlobalCollector()
        called = _CalledFunctionNameCollector()
        for statement in definition.body:
            globals_used.visit(statement)
            called.visit(statement)
        summaries[name] = set(globals_used.names)
        calls[name] = called.names.intersection(definitions)
        if called.names - set(definitions) or any(
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            for statement in definition.body
        ):
            summaries[name].add("*")
        elif calls[name]:
            summaries[name].add("*")
    changed = True
    while changed:
        changed = False
        for name, called_names in calls.items():
            propagated = set().union(
                *(summaries[called] for called in called_names), set()
            )
            if not propagated.issubset(summaries[name]):
                summaries[name].update(propagated)
                changed = True
    return summaries


def _python_function_alias_effects(
    statement: ast.stmt, effects: dict[str, set[str]]
) -> dict[str, set[str]]:
    def expression_effects(expression: ast.expr) -> set[str] | None:
        if isinstance(expression, ast.Name):
            known = effects.get(expression.id)
            return None if known is None else set(known)
        if isinstance(expression, ast.IfExp):
            body = expression_effects(expression.body)
            alternate = expression_effects(expression.orelse)
            if body is None and alternate is None:
                return None
            return (body or {"*"}) | (alternate or {"*"})
        if isinstance(expression, ast.Lambda):
            called = _CalledFunctionNameCollector()
            called.visit(expression.body)
            resolved = set().union(
                *(effects.get(name, {"*"}) for name in called.names), set()
            )
            return resolved
        return None

    def bind(target: ast.expr, value: ast.expr) -> dict[str, set[str]]:
        if isinstance(target, ast.Name):
            resolved = expression_effects(value)
            return {} if resolved is None else {target.id: resolved}
        if (
            isinstance(target, (ast.Tuple, ast.List))
            and isinstance(value, (ast.Tuple, ast.List))
            and len(target.elts) == len(value.elts)
        ):
            aliases: dict[str, set[str]] = {}
            for child_target, child_value in zip(target.elts, value.elts):
                aliases.update(bind(child_target, child_value))
            return aliases
        return {}

    if isinstance(statement, ast.Assign):
        direct_aliases: dict[str, set[str]] = {}
        for target in statement.targets:
            direct_aliases.update(bind(target, statement.value))
        if direct_aliases:
            return direct_aliases
    if isinstance(statement, ast.AnnAssign) and statement.value is not None:
        direct_aliases = bind(statement.target, statement.value)
        if direct_aliases:
            return direct_aliases
    aliases: dict[str, set[str]] = {}

    class NamedAliasCollector(ast.NodeVisitor):
        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            aliases.update(bind(node.target, node.value))
            self.generic_visit(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

    NamedAliasCollector().visit(statement)
    return aliases


def _python_called_function_names(statement: ast.stmt) -> set[str]:
    collector = _CalledFunctionNameCollector()
    collector.visit(statement)
    return collector.names


def _advance_python_function_effects(
    statement: ast.stmt,
    definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    effects: dict[str, set[str]],
    first_binding_lines: dict[str, int],
) -> set[str]:
    called_effects = _python_called_function_effects(statement, effects)
    aliases = _python_function_alias_effects(statement, effects)
    if (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.NamedExpr)
    ):
        called_effects.update(set().union(*aliases.values(), set()))
    if any(
        first_binding_lines[name] > statement.lineno
        for name in _python_called_function_names(statement)
        if name in first_binding_lines
    ):
        called_effects.add("*")
    assigned = _python_assigned_names(statement)
    assigned.update(_python_evaluated_binding_names(statement))
    assigned.update(called_effects)
    if "*" in assigned:
        effects.clear()
    for name in assigned:
        effects.pop(name, None)
        if not (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement.name == name
        ):
            definitions.pop(name, None)
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        definitions[statement.name] = statement
        preserved_aliases = {
            name: effect
            for name, effect in effects.items()
            if name not in definitions
        }
        summaries = _python_function_effect_summaries(list(definitions.values()))
        effects.clear()
        effects.update({**preserved_aliases, **summaries})
    else:
        effects.update(aliases)
    return called_effects


class _ClassGlobalCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Global(self, node: ast.Global) -> None:
        self.names.update(node.names)

    def visit_FunctionDef(self, _node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, _node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, _node: ast.Lambda) -> None:
        return


class FastAPIAnalyzer:
    name = "fastapi"
    _http_methods = frozenset(
        {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
    )

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        try:
            tree = ast.parse(source_file.content)
            compile(tree, source_file.path, "exec")
        except SyntaxError:
            return []

        state: dict[str, _Binding] = {}
        first_binding_lines: dict[str, int] = {}
        for statement in tree.body:
            for name in _python_assigned_names(statement):
                first_binding_lines.setdefault(name, statement.lineno)
        function_definitions: dict[
            str, ast.FunctionDef | ast.AsyncFunctionDef
        ] = {}
        function_effects: dict[str, set[str]] = {}
        entities: list[FrameworkEntity] = []
        self._analyze_block(
            source_file,
            tree.body,
            state,
            entities,
            function_definitions,
            function_effects,
            first_binding_lines,
            collect_routes=True,
        )
        return entities

    def _analyze_block(
        self,
        source_file: SourceFile,
        statements: list[ast.stmt],
        state: dict[str, _Binding],
        entities: list[FrameworkEntity],
        function_definitions: dict[
            str, ast.FunctionDef | ast.AsyncFunctionDef
        ],
        function_effects: dict[str, set[str]],
        first_binding_lines: dict[str, int],
        *,
        collect_routes: bool,
    ) -> None:
        for node in statements:
            called_effects = _advance_python_function_effects(
                node, function_definitions, function_effects, first_binding_lines
            )
            if "*" in called_effects:
                state.clear()
            else:
                for name in called_effects:
                    state.pop(name, None)
            if isinstance(node, ast.ImportFrom):
                if any(alias.name == "*" for alias in node.names):
                    state.clear()
                    continue
                for alias in node.names:
                    name = alias.asname or alias.name
                    state.pop(name, None)
                    if node.level == 0 and node.module == "fastapi" and alias.name in {
                        "APIRouter",
                        "Depends",
                        "FastAPI",
                    }:
                        state[name] = _ConstructorBinding(alias.name)
                continue

            if isinstance(node, ast.Import):
                for alias in node.names:
                    state.pop(alias.asname or alias.name.split(".")[0], None)
                continue

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._apply_expression_bindings(
                    self._function_header_expressions(node), state
                )
                if collect_routes:
                    entities.extend(
                        self._extract_function_routes(source_file, node, state)
                    )
                state.pop(node.name, None)
                continue

            if isinstance(node, ast.ClassDef):
                self._apply_expression_bindings(
                    [
                        *node.decorator_list,
                        *node.bases,
                        *(item.value for item in node.keywords),
                    ],
                    state,
                )
                globals_used = _ClassGlobalCollector()
                for statement in node.body:
                    globals_used.visit(statement)
                for name in globals_used.names:
                    state.pop(name, None)
                state.pop(node.name, None)
                continue

            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                value = node.value
                if value is not None:
                    self._apply_expression_bindings([value], state)
                binding = self._evaluate_binding(value, state)
                direct_targets = all(isinstance(target, ast.Name) for target in targets)
                for target in targets:
                    for name in self._target_names(target):
                        state.pop(name, None)
                        if direct_targets and binding is not None:
                            state[name] = binding
                continue

            if isinstance(node, ast.AugAssign):
                self._apply_expression_bindings([node.value], state)
                for name in self._target_names(node.target):
                    state.pop(name, None)
                continue

            if isinstance(node, ast.Delete):
                for target in node.targets:
                    for name in self._target_names(target):
                        state.pop(name, None)
                continue

            if isinstance(node, _CONTROL_FLOW_BOUNDARIES):
                state.clear()
                continue

            self._apply_expression_bindings([node], state)


    def _apply_expression_bindings(
        self, nodes: list[ast.AST], state: dict[str, _Binding]
    ) -> None:
        collector = _ExpressionBindingCollector()
        for node in nodes:
            collector.visit(node)
        for name in collector.names:
            state.pop(name, None)

    def _evaluate_binding(
        self, value: ast.expr | None, state: dict[str, _Binding]
    ) -> _Binding | None:
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and isinstance(state.get(value.func.id), _ConstructorBinding)
        ):
            return None
        constructor = state[value.func.id]
        assert isinstance(constructor, _ConstructorBinding)
        if value.args:
            return None
        if constructor.kind == "FastAPI":
            if any(keyword.arg is None for keyword in value.keywords):
                return None
            return _ApplicationBinding("")
        if constructor.kind != "APIRouter":
            return None
        prefix = self._router_prefix(value)
        return _ApplicationBinding(prefix) if prefix is not None else None

    def _extract_function_routes(
        self,
        source_file: SourceFile,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        state: dict[str, _Binding],
    ) -> list[FrameworkEntity]:
        entities: list[FrameworkEntity] = []
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and isinstance(
                    state.get(decorator.func.value.id), _ApplicationBinding
                )
                and len(decorator.args) == 1
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
                and not any(
                    keyword.arg in {None, "path"}
                    for keyword in decorator.keywords
                )
            ):
                continue
            application_name = decorator.func.value.id
            if decorator.func.attr == "middleware":
                if decorator.keywords or decorator.args[0].value != "http":
                    continue
                entities.append(
                    FrameworkEntity(
                        framework=self.name,
                        kind="middleware",
                        name=node.name,
                        target=application_name,
                        path=source_file.path,
                        line=decorator.lineno,
                        end_line=getattr(node, "end_lineno", node.lineno),
                        attributes=(
                            ("application", application_name),
                            ("type", decorator.args[0].value),
                        ),
                    )
                )
                continue
            if decorator.func.attr in self._http_methods:
                methods = (decorator.func.attr.upper(),)
            elif decorator.func.attr == "api_route":
                methods = self._api_route_methods(decorator)
            else:
                methods = ()
            application = state[decorator.func.value.id]
            assert isinstance(application, _ApplicationBinding)
            route = self._join_route(
                application.prefix, decorator.args[0].value
            )
            entities.extend(
                FrameworkEntity(
                    framework=self.name,
                    kind="route",
                    name=f"{method} {route}",
                    target=node.name,
                    path=source_file.path,
                    line=decorator.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    attributes=(("method", method), ("route", route)),
                )
                for method in methods
            )
            dependencies = self._route_dependencies(decorator, state)
            entities.extend(
                FrameworkEntity(
                    framework=self.name,
                    kind="authorization",
                    name=f"{method} {route}",
                    target=dependency,
                    path=source_file.path,
                    line=decorator.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    attributes=(
                        ("dependency", dependency),
                        ("handler", node.name),
                        ("method", method),
                        ("route", route),
                    ),
                )
                for method in methods
                for dependency in dependencies
            )
        return entities

    @staticmethod
    def _route_dependencies(
        decorator: ast.Call, state: dict[str, _Binding]
    ) -> tuple[str, ...]:
        values = [
            keyword.value
            for keyword in decorator.keywords
            if keyword.arg == "dependencies"
        ]
        if not values:
            return ()
        if len(values) != 1 or not isinstance(values[0], (ast.List, ast.Tuple)):
            return ()
        dependencies: list[str] = []
        for item in values[0].elts:
            binding = state.get(item.func.id) if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) else None
            if not (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and isinstance(binding, _ConstructorBinding)
                and binding.kind == "Depends"
                and len(item.args) == 1
                and isinstance(item.args[0], ast.Name)
                and not item.keywords
            ):
                return ()
            dependencies.append(item.args[0].id)
        return tuple(dependencies)

    @staticmethod
    def _function_header_expressions(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> list[ast.expr]:
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg:
            arguments.append(node.args.vararg)
        if node.args.kwarg:
            arguments.append(node.args.kwarg)
        expressions = [
            *node.decorator_list,
            *node.args.defaults,
            *(default for default in node.args.kw_defaults if default is not None),
            *(
                argument.annotation
                for argument in arguments
                if argument.annotation is not None
            ),
        ]
        if node.returns:
            expressions.append(node.returns)
        return expressions

    @staticmethod
    def _router_prefix(call: ast.Call) -> str | None:
        prefix = ""
        prefix_seen = False
        for keyword in call.keywords:
            if keyword.arg is None:
                unpacked = FastAPIAnalyzer._literal_dict(keyword.value)
                if unpacked is None or set(unpacked) - {"prefix"}:
                    return None
                if "prefix" not in unpacked:
                    continue
                if prefix_seen:
                    return None
                unpacked_prefix = unpacked["prefix"]
                if not (
                    isinstance(unpacked_prefix, ast.Constant)
                    and isinstance(unpacked_prefix.value, str)
                ):
                    return None
                prefix = unpacked_prefix.value
                prefix_seen = True
                continue
            if keyword.arg != "prefix":
                return None
            if prefix_seen:
                return None
            if isinstance(keyword.value, ast.Constant) and isinstance(
                keyword.value.value, str
            ):
                prefix = keyword.value.value
                prefix_seen = True
                continue
            return None
        if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
            return None
        return prefix

    @staticmethod
    def _literal_dict(node: ast.expr) -> dict[str, ast.expr] | None:
        if not isinstance(node, ast.Dict):
            return None
        result: dict[str, ast.expr] = {}
        for key, value in zip(node.keys, node.values):
            if not (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
            ):
                return None
            result[key.value] = value
        return result

    @classmethod
    def _target_names(cls, target: ast.expr) -> tuple[str, ...]:
        if isinstance(target, ast.Name):
            return (target.id,)
        if isinstance(target, (ast.List, ast.Tuple)):
            return tuple(
                name for item in target.elts for name in cls._target_names(item)
            )
        if isinstance(target, ast.Starred):
            return cls._target_names(target.value)
        return ()


    @staticmethod
    def _join_route(prefix: str, route: str) -> str:
        if not prefix:
            return route
        if not route:
            return prefix
        return f"{prefix.rstrip('/')}/{route.lstrip('/')}"

    @staticmethod
    def _api_route_methods(decorator: ast.Call) -> tuple[str, ...]:
        if any(keyword.arg is None for keyword in decorator.keywords):
            return ()
        methods = next(
            (
                keyword.value
                for keyword in decorator.keywords
                if keyword.arg == "methods"
            ),
            None,
        )
        if not isinstance(methods, (ast.List, ast.Tuple, ast.Set)):
            return ()
        values = []
        seen: set[str] = set()
        for item in methods.elts:
            if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
                return ()
            method = item.value.upper()
            if method not in seen:
                values.append(method)
                seen.add(method)
        return tuple(values)


class FlaskAnalyzer:
    name = "flask"
    _http_methods = frozenset({"delete", "get", "patch", "post", "put"})

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        try:
            tree = ast.parse(source_file.content)
            compile(tree, source_file.path, "exec")
        except SyntaxError:
            return []

        constructors: set[str] = set()
        applications: set[str] = set()
        first_binding_lines: dict[str, int] = {}
        for statement in tree.body:
            for name in _python_assigned_names(statement):
                first_binding_lines.setdefault(name, statement.lineno)
        function_definitions: dict[
            str, ast.FunctionDef | ast.AsyncFunctionDef
        ] = {}
        function_effects: dict[str, set[str]] = {}
        entities: list[FrameworkEntity] = []
        for node in tree.body:
            called_effects = _advance_python_function_effects(
                node, function_definitions, function_effects, first_binding_lines
            )
            if "*" in called_effects:
                constructors.clear()
                applications.clear()
            else:
                self._invalidate(called_effects, constructors, applications)
            if isinstance(node, ast.ImportFrom):
                if any(alias.name == "*" for alias in node.names):
                    constructors.clear()
                    applications.clear()
                    continue
                for alias in node.names:
                    bound_name = alias.asname or alias.name
                    constructors.discard(bound_name)
                    applications.discard(bound_name)
                    if (
                        node.level == 0
                        and node.module == "flask"
                        and alias.name == "Flask"
                    ):
                        constructors.add(bound_name)
                continue

            if isinstance(node, ast.Import):
                self._invalidate(
                    (alias.asname or alias.name.split(".")[0] for alias in node.names),
                    constructors,
                    applications,
                )
                continue

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._invalidate_expressions(
                    FastAPIAnalyzer._function_header_expressions(node),
                    constructors,
                    applications,
                )
                entities.extend(
                    self._extract_function_routes(source_file, node, applications)
                )
                self._invalidate((node.name,), constructors, applications)
                continue

            if isinstance(node, ast.ClassDef):
                self._invalidate_expressions(
                    [
                        *node.decorator_list,
                        *node.bases,
                        *(keyword.value for keyword in node.keywords),
                    ],
                    constructors,
                    applications,
                )
                globals_used = _ClassGlobalCollector()
                for statement in node.body:
                    globals_used.visit(statement)
                self._invalidate(
                    (*globals_used.names, node.name),
                    constructors,
                    applications,
                )
                continue

            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                value = node.value
                if value is not None:
                    self._invalidate_expressions(
                        [value], constructors, applications
                    )
                is_application = (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in constructors
                    and self._valid_application_call(value)
                )
                direct_targets = all(isinstance(target, ast.Name) for target in targets)
                for target in targets:
                    names = FastAPIAnalyzer._target_names(target)
                    self._invalidate(names, constructors, applications)
                    if direct_targets and is_application:
                        applications.update(names)
                continue

            if isinstance(node, ast.AugAssign):
                self._invalidate_expressions(
                    [node.value], constructors, applications
                )
                self._invalidate(
                    FastAPIAnalyzer._target_names(node.target),
                    constructors,
                    applications,
                )
                continue

            if isinstance(node, ast.Delete):
                for target in node.targets:
                    self._invalidate(
                        FastAPIAnalyzer._target_names(target),
                        constructors,
                        applications,
                    )
                continue

            if isinstance(node, _CONTROL_FLOW_BOUNDARIES):
                constructors.clear()
                applications.clear()
                continue

            self._invalidate_expressions([node], constructors, applications)
        return entities

    def _extract_function_routes(
        self,
        source_file: SourceFile,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        applications: set[str],
    ) -> list[FrameworkEntity]:
        entities: list[FrameworkEntity] = []
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Attribute)
                and isinstance(decorator.value, ast.Name)
                and decorator.value.id in applications
                and decorator.attr
                in {"after_request", "before_request", "teardown_request"}
            ):
                entities.append(
                    FrameworkEntity(
                        framework=self.name,
                        kind="middleware",
                        name=node.name,
                        target=decorator.value.id,
                        path=source_file.path,
                        line=decorator.lineno,
                        end_line=getattr(node, "end_lineno", node.lineno),
                        attributes=(
                            ("application", decorator.value.id),
                            ("phase", decorator.attr),
                        ),
                    )
                )
                continue
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id in applications
                and len(decorator.args) == 1
                and not any(keyword.arg in {None, "rule"} for keyword in decorator.keywords)
            ):
                continue
            route_evidence = self._static_string(decorator.args[0])
            if route_evidence is None or not route_evidence[0].startswith("/"):
                continue
            route, confidence = route_evidence
            if decorator.func.attr in self._http_methods:
                methods = (
                    ()
                    if any(
                        keyword.arg == "methods" for keyword in decorator.keywords
                    )
                    else (decorator.func.attr.upper(),)
                )
            elif decorator.func.attr == "route":
                methods = self._route_methods(decorator)
            else:
                methods = ()
            entities.extend(
                FrameworkEntity(
                    framework=self.name,
                    kind="route",
                    name=f"{method} {route}",
                    target=node.name,
                    path=source_file.path,
                    line=decorator.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    confidence=confidence,
                    attributes=(("method", method), ("route", route)),
                )
                for method in methods
            )
        return entities

    @staticmethod
    def _route_methods(decorator: ast.Call) -> tuple[str, ...]:
        method_values = [
            keyword.value for keyword in decorator.keywords if keyword.arg == "methods"
        ]
        if not method_values:
            return ("GET",)
        if len(method_values) != 1:
            return ()
        value = method_values[0]
        if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            return ()
        methods: list[str] = []
        seen: set[str] = set()
        for item in value.elts:
            if not (
                isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and item.value
            ):
                return ()
            method = item.value.upper()
            if method not in seen:
                methods.append(method)
                seen.add(method)
        return tuple(methods)

    @staticmethod
    def _valid_application_call(call: ast.Call) -> bool:
        if any(isinstance(argument, ast.Starred) for argument in call.args) or any(
            keyword.arg is None for keyword in call.keywords
        ):
            return False
        import_names = [
            keyword for keyword in call.keywords if keyword.arg == "import_name"
        ]
        if len(call.args) == 1:
            return not import_names
        return not call.args and len(import_names) == 1

    @classmethod
    def _static_string(cls, node: ast.expr) -> tuple[str, str] | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, "high"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = cls._static_string(node.left)
            right = cls._static_string(node.right)
            if left is not None and right is not None:
                return left[0] + right[0], "medium"
        return None

    @staticmethod
    def _invalidate(
        names, constructors: set[str], applications: set[str]
    ) -> None:
        for name in names:
            constructors.discard(name)
            applications.discard(name)

    @classmethod
    def _invalidate_expressions(
        cls,
        nodes: list[ast.AST],
        constructors: set[str],
        applications: set[str],
    ) -> None:
        collector = _ExpressionBindingCollector()
        for node in nodes:
            collector.visit(node)
        cls._invalidate(collector.names, constructors, applications)


class DjangoAnalyzer:
    name = "django"

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        try:
            tree = ast.parse(source_file.content)
            compile(tree, source_file.path, "exec")
        except SyntaxError:
            return []

        route_functions: dict[str, str] = {}
        first_binding_lines: dict[str, int] = {}
        for statement in tree.body:
            for name in _python_assigned_names(statement):
                first_binding_lines.setdefault(name, statement.lineno)
        function_definitions: dict[
            str, ast.FunctionDef | ast.AsyncFunctionDef
        ] = {}
        function_effects: dict[str, set[str]] = {}
        entities: list[FrameworkEntity] = []
        for node in tree.body:
            called_effects = _advance_python_function_effects(
                node, function_definitions, function_effects, first_binding_lines
            )
            if "*" in called_effects:
                route_functions.clear()
            else:
                for name in called_effects:
                    route_functions.pop(name, None)
            if isinstance(node, ast.ImportFrom):
                if any(alias.name == "*" for alias in node.names):
                    route_functions.clear()
                    continue
                for alias in node.names:
                    bound_name = alias.asname or alias.name
                    route_functions.pop(bound_name, None)
                    if (
                        node.level == 0
                        and node.module == "django.urls"
                        and alias.name in {"path", "re_path"}
                    ):
                        route_functions[bound_name] = alias.name
                continue

            if isinstance(node, ast.Import):
                for alias in node.names:
                    route_functions.pop(
                        alias.asname or alias.name.split(".")[0], None
                    )
                continue

            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                value = node.value
                self._invalidate_expressions([value] if value is not None else [], route_functions)
                if (
                    value is not None
                    and any(
                        isinstance(target, ast.Name)
                        and target.id == "urlpatterns"
                        for target in targets
                    )
                ):
                    entities.extend(
                        self._extract_patterns(source_file, value, route_functions)
                    )
                for target in targets:
                    for name in FastAPIAnalyzer._target_names(target):
                        route_functions.pop(name, None)
                continue

            if isinstance(node, ast.AugAssign):
                self._invalidate_expressions([node.value], route_functions)
                if (
                    isinstance(node.op, ast.Add)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == "urlpatterns"
                ):
                    entities.extend(
                        self._extract_patterns(source_file, node.value, route_functions)
                    )
                for name in FastAPIAnalyzer._target_names(node.target):
                    route_functions.pop(name, None)
                continue

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._invalidate_expressions(
                    FastAPIAnalyzer._function_header_expressions(node), route_functions
                )
                route_functions.pop(node.name, None)
                continue

            if isinstance(node, ast.ClassDef):
                self._invalidate_expressions(
                    [
                        *node.decorator_list,
                        *node.bases,
                        *(keyword.value for keyword in node.keywords),
                    ],
                    route_functions,
                )
                globals_used = _ClassGlobalCollector()
                for statement in node.body:
                    globals_used.visit(statement)
                for name in globals_used.names:
                    route_functions.pop(name, None)
                route_functions.pop(node.name, None)
                continue

            if isinstance(node, ast.Delete):
                for target in node.targets:
                    for name in FastAPIAnalyzer._target_names(target):
                        route_functions.pop(name, None)
                continue

            if isinstance(node, _CONTROL_FLOW_BOUNDARIES):
                route_functions.clear()
                continue

            self._invalidate_expressions([node], route_functions)
        return entities

    def _extract_patterns(
        self,
        source_file: SourceFile,
        value: ast.expr,
        route_functions: dict[str, str],
    ) -> list[FrameworkEntity]:
        if not isinstance(value, (ast.List, ast.Tuple)):
            return []
        entities: list[FrameworkEntity] = []
        for item in value.elts:
            if not (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id in route_functions
                and 2 <= len(item.args) <= 4
                and self._valid_pattern_call(item)
            ):
                continue
            route_evidence = FlaskAnalyzer._static_string(item.args[0])
            view_evidence = self._view_target(item.args[1])
            if route_evidence is None or view_evidence is None:
                continue
            raw_route, confidence = route_evidence
            route_kind = route_functions[item.func.id]
            route = (
                f"/{raw_route.lstrip('/')}" if route_kind == "path" else raw_route
            )
            target, view, handler_kind = view_evidence
            entities.append(
                FrameworkEntity(
                    framework=self.name,
                    kind="route",
                    name=f"ANY {route}",
                    target=target,
                    path=source_file.path,
                    line=item.lineno,
                    end_line=getattr(item, "end_lineno", item.lineno),
                    confidence=confidence,
                    attributes=(
                        ("handler_kind", handler_kind),
                        ("method", "ANY"),
                        ("route", route),
                        ("route_kind", route_kind),
                        ("view", view),
                    ),
                )
            )
        return entities

    @staticmethod
    def _valid_pattern_call(call: ast.Call) -> bool:
        parameter_order = ("route", "view", "kwargs", "name")
        if len(call.args) > len(parameter_order) or any(
            isinstance(argument, ast.Starred) for argument in call.args
        ):
            return False
        occupied = parameter_order[: len(call.args)]
        return all(
            keyword.arg in {"kwargs", "name"} and keyword.arg not in occupied
            for keyword in call.keywords
        )

    @classmethod
    def _view_target(cls, node: ast.expr) -> tuple[str, str, str] | None:
        if isinstance(node, ast.Call):
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "as_view"
                and not node.args
                and not any(keyword.arg is None for keyword in node.keywords)
            ):
                return None
            class_name = cls._dotted_name(node.func.value)
            if class_name is None:
                return None
            return class_name.rsplit(".", 1)[-1], f"{class_name}.as_view", "class"
        view_name = cls._dotted_name(node)
        if view_name is None:
            return None
        return view_name.rsplit(".", 1)[-1], view_name, "function"

    @classmethod
    def _dotted_name(cls, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = cls._dotted_name(node.value)
            if prefix is not None:
                return f"{prefix}.{node.attr}"
        return None

    @staticmethod
    def _invalidate_expressions(
        nodes: list[ast.AST], route_functions: dict[str, str]
    ) -> None:
        collector = _ExpressionBindingCollector()
        for node in nodes:
            collector.visit(node)
        for name in collector.names:
            route_functions.pop(name, None)


def _javascript_text(snapshot: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return snapshot[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _javascript_string(snapshot: bytes, node: Node | None) -> str | None:
    if node is None or node.type != "string" or any(
        child.type != "string_fragment" for child in node.named_children
    ):
        return None
    try:
        value = ast.literal_eval(_javascript_text(snapshot, node))
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _javascript_dotted_name(snapshot: bytes, node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type in {"identifier", "property_identifier", "type_identifier"}:
        return _javascript_text(snapshot, node)
    if node.type != "member_expression":
        return None
    owner = _javascript_dotted_name(snapshot, node.child_by_field_name("object"))
    property_name = _javascript_dotted_name(
        snapshot, node.child_by_field_name("property")
    )
    if owner is None or property_name is None:
        return None
    return f"{owner}.{property_name}"


def _javascript_assignment_roots(
    snapshot: bytes,
    node: Node,
    function_effects: dict[str, set[str]] | None = None,
    *,
    execute_root_function: bool = False,
) -> set[str]:
    roots: set[str] = set()
    known_effects = function_effects or {}
    function_nodes = {
        "arrow_function",
        "function_declaration",
        "function_expression",
        "generator_function",
        "generator_function_declaration",
    }

    def target_roots(target: Node | None) -> set[str]:
        if target is None:
            return set()
        dotted = _javascript_dotted_name(snapshot, target)
        if dotted is not None:
            return {dotted.split(".", 1)[0]}
        if target.type == "subscript_expression":
            return target_roots(target.child_by_field_name("object"))
        if target.type == "pair_pattern":
            return target_roots(target.child_by_field_name("value"))
        if target.type in {
            "shorthand_property_identifier_pattern",
            "shorthand_property_identifier",
        }:
            return {_javascript_text(snapshot, target)}
        found: set[str] = set()
        for child in target.named_children:
            found.update(target_roots(child))
        return found

    def immediately_invoked(current: Node) -> bool:
        expression = current
        parent = current.parent
        while parent is not None:
            if parent.type == "parenthesized_expression":
                expression = parent
                parent = parent.parent
                continue
            if (
                parent.type == "sequence_expression"
                and parent.named_children
                and parent.named_children[-1] == expression
            ):
                expression = parent
                parent = parent.parent
                continue
            break
        if (
            parent is not None
            and parent.type == "member_expression"
            and parent.child_by_field_name("object") == expression
            and _javascript_dotted_name(
                snapshot, parent.child_by_field_name("property")
            )
            in {"call", "apply"}
        ):
            expression = parent
            parent = parent.parent
        return (
            parent is not None
            and parent.type == "call_expression"
            and parent.child_by_field_name("function") == expression
        )

    def called_binding(function: Node | None) -> str | None:
        if function is None:
            return None
        current: Node = function
        while current.type == "parenthesized_expression":
            if not current.named_children:
                return None
            current = current.named_children[0]
        if current.type == "sequence_expression":
            if not current.named_children:
                return None
            return called_binding(current.named_children[-1])
        if current.type == "identifier":
            return _javascript_text(snapshot, current)
        if current.type == "member_expression" and _javascript_dotted_name(
            snapshot, current.child_by_field_name("property")
        ) in {"call", "apply"}:
            return called_binding(current.child_by_field_name("object"))
        return None

    def visit(current: Node) -> None:
        if (
            current.type in function_nodes
            and not (current == node and execute_root_function)
            and not immediately_invoked(current)
        ):
            if execute_root_function:
                roots.add("*")
            return
        if current.type == "method_definition":
            for child in current.named_children:
                if child.type == "decorator" or child == current.child_by_field_name(
                    "name"
                ):
                    visit(child)
            return
        if current.type in {"public_field_definition", "field_definition"}:
            for child in current.named_children:
                if child.type == "decorator" or child == current.child_by_field_name(
                    "name"
                ):
                    visit(child)
            if any(child.type == "static" for child in current.children):
                value = current.child_by_field_name("value")
                if value is not None:
                    visit(value)
            return
        if current.type in {
            "assignment_expression",
            "augmented_assignment_expression",
        }:
            left = current.child_by_field_name("left")
            right = current.child_by_field_name("right")
            roots.update(target_roots(left))
            if left is not None and left.type == "identifier" and right is not None:
                if right.type == "identifier":
                    roots.update(
                        known_effects.get(_javascript_text(snapshot, right), set())
                    )
                elif right.type in function_nodes:
                    roots.update(
                        _javascript_assignment_roots(
                            snapshot,
                            right,
                            known_effects,
                            execute_root_function=True,
                        )
                    )
        elif current.type == "update_expression":
            roots.update(target_roots(current.child_by_field_name("argument")))
        elif current.type == "unary_expression" and _javascript_text(
            snapshot, current
        ).lstrip().startswith("delete "):
            argument = current.child_by_field_name("argument")
            if argument is None and current.named_children:
                argument = current.named_children[-1]
            roots.update(target_roots(argument))
        elif current.type == "variable_declarator":
            name = current.child_by_field_name("name")
            if name is not None and name.type != "identifier":
                roots.update(target_roots(name))
        elif current.type == "call_expression":
            function = called_binding(current.child_by_field_name("function"))
            if function is not None:
                roots.update(known_effects.get(function, set()))
        for child in current.named_children:
            visit(child)

    visit(node)
    return roots


def _javascript_function_effects(snapshot: bytes, tree: Tree) -> dict[str, set[str]]:
    declarations: dict[str, Node] = {}
    for statement in tree.root_node.named_children:
        if statement.type in {
            "function_declaration",
            "generator_function_declaration",
        }:
            candidates = ((statement.child_by_field_name("name"), statement),)
        elif statement.type in {"lexical_declaration", "variable_declaration"}:
            candidates = tuple(
                (
                    declaration.child_by_field_name("name"),
                    declaration.child_by_field_name("value"),
                )
                for declaration in statement.named_children
                if declaration.type == "variable_declarator"
            )
        else:
            continue
        for name_node, value in candidates:
            if (
                name_node is None
                or name_node.type != "identifier"
                or value is None
                or value.type not in {
                    "arrow_function",
                    "function_expression",
                    "generator_function",
                    "identifier",
                    "function_declaration",
                    "generator_function_declaration",
                }
            ):
                continue
            name = _javascript_text(snapshot, name_node)
            declarations[name] = value

    effects = {name: set() for name in declarations}
    for _iteration in range(len(declarations) + 1):
        changed = False
        for name, declaration in declarations.items():
            if declaration.type == "identifier":
                discovered = set(
                    effects.get(_javascript_text(snapshot, declaration), set())
                )
            else:
                discovered = _javascript_assignment_roots(
                    snapshot,
                    declaration,
                    effects,
                    execute_root_function=True,
                )
            if discovered != effects[name]:
                effects[name] = discovered
                changed = True
        if not changed:
            break
    return effects


class ExpressAnalyzer:
    name = "express"
    _http_methods = FastAPIAnalyzer._http_methods
    _control_flow = frozenset(
        {
            "do_statement",
            "for_in_statement",
            "for_statement",
            "if_statement",
            "switch_statement",
            "try_statement",
            "while_statement",
            "with_statement",
        }
    )

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        if Path(source_file.path).suffix.lower() not in {
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".ts",
            ".tsx",
        }:
            return []
        parsed = _javascript_tree(source_file)
        if parsed is None:
            return []
        tree, snapshot = parsed
        constructors: set[str] = set()
        applications: set[str] = set()
        function_effects = _javascript_function_effects(snapshot, tree)
        entities: list[FrameworkEntity] = []

        for statement in tree.root_node.named_children:
            for name in _javascript_assignment_roots(
                snapshot, statement, function_effects
            ):
                if name == "*":
                    constructors.clear()
                    applications.clear()
                    function_effects.clear()
                    continue
                constructors.discard(name)
                applications.discard(name)
                function_effects.pop(name, None)
            if statement.type == "import_statement":
                self._record_import(snapshot, statement, constructors)
                continue
            if statement.type in {"lexical_declaration", "variable_declaration"}:
                self._record_declaration(
                    snapshot, statement, constructors, applications
                )
                continue
            if statement.type == "expression_statement" and statement.named_children:
                expression = statement.named_children[0]
                if expression.type == "assignment_expression":
                    target = _javascript_dotted_name(
                        snapshot, expression.child_by_field_name("left")
                    )
                    if target is not None and "." not in target:
                        constructors.discard(target)
                        applications.discard(target)
                    continue
                if expression.type == "call_expression":
                    entity = self._route_entity(
                        source_file, snapshot, expression, applications
                    )
                    if entity is not None:
                        entities.append(entity)
                    else:
                        entities.extend(
                            self._middleware_entities(
                                source_file, snapshot, expression, applications
                            )
                        )
                    continue
            if statement.type in {
                "class_declaration",
                "function_declaration",
                "generator_function_declaration",
            }:
                name = _javascript_text(
                    snapshot, statement.child_by_field_name("name")
                )
                constructors.discard(name)
                applications.discard(name)
                if statement.type == "class_declaration":
                    constructors.clear()
                    applications.clear()
                continue
            if statement.type in self._control_flow:
                constructors.clear()
                applications.clear()

        return entities

    @staticmethod
    def _record_import(
        snapshot: bytes, statement: Node, constructors: set[str]
    ) -> None:
        if _javascript_string(
            snapshot, statement.child_by_field_name("source")
        ) != "express":
            return
        clause = next(
            (child for child in statement.named_children if child.type == "import_clause"),
            None,
        )
        if clause is None:
            return
        default_import = next(
            (child for child in clause.named_children if child.type == "identifier"),
            None,
        )
        if default_import is not None:
            constructors.add(_javascript_text(snapshot, default_import))

    @staticmethod
    def _record_declaration(
        snapshot: bytes,
        statement: Node,
        constructors: set[str],
        applications: set[str],
    ) -> None:
        for declaration in statement.named_children:
            if declaration.type != "variable_declarator":
                continue
            name_node = declaration.child_by_field_name("name")
            if name_node is None or name_node.type != "identifier":
                continue
            name = _javascript_text(snapshot, name_node)
            constructors.discard(name)
            applications.discard(name)
            value = declaration.child_by_field_name("value")
            if value is None or value.type != "call_expression":
                continue
            function = _javascript_dotted_name(
                snapshot, value.child_by_field_name("function")
            )
            arguments = value.child_by_field_name("arguments")
            if arguments is None:
                continue
            if function == "require" and len(arguments.named_children) == 1:
                if _javascript_string(snapshot, arguments.named_children[0]) == "express":
                    constructors.add(name)
                continue
            if arguments.named_children:
                continue
            if function in constructors or (
                function is not None
                and function.endswith(".Router")
                and function.rsplit(".", 1)[0] in constructors
            ):
                applications.add(name)

    def _route_entity(
        self,
        source_file: SourceFile,
        snapshot: bytes,
        call: Node,
        applications: set[str],
    ) -> FrameworkEntity | None:
        function = call.child_by_field_name("function")
        if function is None or function.type != "member_expression":
            return None
        owner = _javascript_dotted_name(
            snapshot, function.child_by_field_name("object")
        )
        method = _javascript_text(
            snapshot, function.child_by_field_name("property")
        ).lower()
        if owner not in applications or method not in self._http_methods:
            return None
        arguments = call.child_by_field_name("arguments")
        if arguments is None or len(arguments.named_children) < 2:
            return None
        route = _javascript_string(snapshot, arguments.named_children[0])
        if route is None or not route.startswith("/"):
            return None
        handlers = [
            _javascript_dotted_name(snapshot, argument)
            for argument in arguments.named_children[1:]
        ]
        if any(handler is None for handler in handlers):
            return None
        handler = handlers[-1]
        assert handler is not None
        middleware = tuple(item for item in handlers[:-1] if item is not None)
        return FrameworkEntity(
            framework=self.name,
            kind="route",
            name=f"{method.upper()} {route}",
            target=handler.rsplit(".", 1)[-1],
            path=source_file.path,
            line=call.start_point.row + 1,
            end_line=call.end_point.row + 1,
            attributes=(
                ("handler", handler),
                ("method", method.upper()),
                ("middleware", ",".join(middleware)),
                ("owner", owner),
                ("route", route),
            ),
        )

    def _middleware_entities(
        self,
        source_file: SourceFile,
        snapshot: bytes,
        call: Node,
        applications: set[str],
    ) -> list[FrameworkEntity]:
        function = call.child_by_field_name("function")
        if function is None or function.type != "member_expression":
            return []
        owner = _javascript_dotted_name(
            snapshot, function.child_by_field_name("object")
        )
        method = _javascript_text(
            snapshot, function.child_by_field_name("property")
        )
        if owner not in applications or method != "use":
            return []
        arguments = call.child_by_field_name("arguments")
        if arguments is None or not arguments.named_children:
            return []
        values = list(arguments.named_children)
        scope = _javascript_string(snapshot, values[0])
        if scope is None and len(values) != 1:
            return []
        handlers = values[1:] if scope is not None else values
        if not handlers:
            return []
        names = [_javascript_dotted_name(snapshot, handler) for handler in handlers]
        if any(name is None for name in names):
            return []
        assert owner is not None
        return [
            FrameworkEntity(
                framework=self.name,
                kind="middleware",
                name=name.rsplit(".", 1)[-1],
                target=owner,
                path=source_file.path,
                line=call.start_point.row + 1,
                end_line=call.end_point.row + 1,
                attributes=(
                    ("handler", name),
                    ("owner", owner),
                    ("scope", scope if scope is not None else "*"),
                ),
            )
            for name in names
            if name is not None
        ]


class NestJSAnalyzer:
    name = "nestjs"
    _route_decorators = {
        "All": "ANY",
        "Delete": "DELETE",
        "Get": "GET",
        "Head": "HEAD",
        "Options": "OPTIONS",
        "Patch": "PATCH",
        "Post": "POST",
        "Put": "PUT",
    }

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        parsed = _javascript_tree(source_file)
        if parsed is None or Path(source_file.path).suffix.lower() not in {
            ".ts",
            ".tsx",
            ".mts",
            ".cts",
        }:
            return []
        tree, snapshot = parsed
        decorators: dict[str, str] = {}
        function_effects = _javascript_function_effects(snapshot, tree)
        entities: list[FrameworkEntity] = []

        for statement in tree.root_node.named_children:
            for name in _javascript_assignment_roots(
                snapshot, statement, function_effects
            ):
                if name == "*":
                    decorators.clear()
                    function_effects.clear()
                    continue
                decorators.pop(name, None)
                function_effects.pop(name, None)
            if statement.type == "import_statement":
                self._record_import(snapshot, statement, decorators)
                continue
            class_node, class_decorators = self._class_evidence(statement)
            if class_node is not None:
                entities.extend(
                    self._class_routes(
                        source_file,
                        snapshot,
                        class_node,
                        class_decorators,
                        decorators,
                    )
                )
                class_name = _javascript_text(
                    snapshot, class_node.child_by_field_name("name")
                )
                decorators.pop(class_name, None)
                continue
            if statement.type in ExpressAnalyzer._control_flow:
                decorators.clear()
                continue
            for name in self._declared_names(snapshot, statement):
                decorators.pop(name, None)

        return entities

    @classmethod
    def _record_import(
        cls, snapshot: bytes, statement: Node, decorators: dict[str, str]
    ) -> None:
        if _javascript_string(
            snapshot, statement.child_by_field_name("source")
        ) != "@nestjs/common":
            return
        supported = {
            "Controller",
            "UseGuards",
            "UseInterceptors",
            *cls._route_decorators,
        }
        for node in cls._walk(statement):
            if node.type != "import_specifier":
                continue
            imported = _javascript_text(snapshot, node.child_by_field_name("name"))
            if imported not in supported:
                continue
            local = _javascript_text(
                snapshot,
                node.child_by_field_name("alias")
                or node.child_by_field_name("name"),
            )
            decorators[local] = imported

    @staticmethod
    def _walk(node: Node):
        yield node
        for child in node.named_children:
            yield from NestJSAnalyzer._walk(child)

    @staticmethod
    def _class_evidence(statement: Node) -> tuple[Node | None, tuple[Node, ...]]:
        if statement.type == "class_declaration":
            return statement, tuple(
                child for child in statement.named_children if child.type == "decorator"
            )
        if statement.type != "export_statement":
            return None, ()
        declaration = statement.child_by_field_name("declaration")
        if declaration is None or declaration.type != "class_declaration":
            return None, ()
        return declaration, tuple(
            child for child in statement.named_children if child.type == "decorator"
        )

    @staticmethod
    def _declared_names(snapshot: bytes, statement: Node) -> tuple[str, ...]:
        names: list[str] = []
        if statement.type in {"lexical_declaration", "variable_declaration"}:
            for declaration in statement.named_children:
                if declaration.type != "variable_declarator":
                    continue
                name = declaration.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    names.append(_javascript_text(snapshot, name))
        elif statement.type in {
            "function_declaration",
            "generator_function_declaration",
        }:
            names.append(
                _javascript_text(snapshot, statement.child_by_field_name("name"))
            )
        return tuple(names)

    def _class_routes(
        self,
        source_file: SourceFile,
        snapshot: bytes,
        class_node: Node,
        class_decorators: tuple[Node, ...],
        decorators: dict[str, str],
    ) -> list[FrameworkEntity]:
        prefixes: list[str] = []
        for decorator in class_decorators:
            local, call = self._decorator_binding(snapshot, decorator)
            if decorators.get(local) != "Controller":
                continue
            if call is None or (prefix := self._call_path(snapshot, call)) is None:
                return []
            prefixes.append(prefix)
        if len(prefixes) != 1:
            return []
        class_name = _javascript_text(
            snapshot, class_node.child_by_field_name("name")
        )
        body = class_node.child_by_field_name("body")
        if not class_name or body is None:
            return []
        entities: list[FrameworkEntity] = []
        pending: list[Node] = []
        for child in body.named_children:
            if child.type == "decorator":
                pending.append(child)
                continue
            if child.type == "method_definition":
                entities.extend(
                    self._method_routes(
                        source_file,
                        snapshot,
                        child,
                        tuple(pending),
                        class_name,
                        prefixes[0],
                        decorators,
                    )
                )
            pending.clear()
        return entities

    def _method_routes(
        self,
        source_file: SourceFile,
        snapshot: bytes,
        method_node: Node,
        method_decorators: tuple[Node, ...],
        class_name: str,
        prefix: str,
        decorators: dict[str, str],
    ) -> list[FrameworkEntity]:
        if any(
            child.type in {"*", "static", "get", "set"}
            for child in method_node.children
        ):
            return []
        evidence: list[tuple[Node, str, str]] = []
        guard_evidence: list[tuple[Node, str]] = []
        interceptor_evidence: list[tuple[Node, str]] = []
        for decorator in method_decorators:
            local, call = self._decorator_binding(snapshot, decorator)
            canonical = decorators.get(local)
            if canonical in {"UseGuards", "UseInterceptors"}:
                if call is None:
                    continue
                arguments = call.child_by_field_name("arguments")
                if arguments is None or not arguments.named_children:
                    continue
                guards = [
                    _javascript_text(snapshot, argument)
                    for argument in arguments.named_children
                    if argument.type == "identifier"
                ]
                if len(guards) != len(arguments.named_children):
                    continue
                boundary_evidence = (
                    guard_evidence
                    if canonical == "UseGuards"
                    else interceptor_evidence
                )
                boundary_evidence.extend((decorator, guard) for guard in guards)
                continue
            if canonical not in self._route_decorators:
                continue
            if call is None:
                return []
            route = self._call_path(snapshot, call)
            if route is None:
                return []
            evidence.append((decorator, self._route_decorators[canonical], route))
        if len(evidence) != 1:
            return []
        name_node = method_node.child_by_field_name("name")
        if name_node is None or name_node.type != "property_identifier":
            return []
        method_name = _javascript_text(snapshot, name_node)
        decorator, http_method, route_suffix = evidence[0]
        route = self._join_route(prefix, route_suffix)
        entities = [
            FrameworkEntity(
                framework=self.name,
                kind="route",
                name=f"{http_method} {route}",
                target=method_name,
                path=source_file.path,
                line=decorator.start_point.row + 1,
                end_line=method_node.end_point.row + 1,
                attributes=(
                    ("controller", class_name),
                    ("handler", f"{class_name}.{method_name}"),
                    ("handler_kind", "method"),
                    ("method", http_method),
                    ("route", route),
                ),
            )
        ]
        handler = f"{class_name}.{method_name}"
        entities.extend(
            FrameworkEntity(
                framework=self.name,
                kind="authorization",
                name=handler,
                target=guard,
                path=source_file.path,
                line=guard_decorator.start_point.row + 1,
                end_line=method_node.end_point.row + 1,
                attributes=(
                    ("controller", class_name),
                    ("guard", guard),
                    ("handler", handler),
                    ("route", route),
                ),
            )
            for guard_decorator, guard in guard_evidence
        )
        entities.extend(
            FrameworkEntity(
                framework=self.name,
                kind="middleware",
                name=handler,
                target=interceptor,
                path=source_file.path,
                line=interceptor_decorator.start_point.row + 1,
                end_line=method_node.end_point.row + 1,
                attributes=(
                    ("controller", class_name),
                    ("handler", handler),
                    ("interceptor", interceptor),
                    ("route", route),
                ),
            )
            for interceptor_decorator, interceptor in interceptor_evidence
        )
        return entities

    @staticmethod
    def _decorator_binding(
        snapshot: bytes, decorator: Node
    ) -> tuple[str, Node | None]:
        call = next(
            (
                child
                for child in decorator.named_children
                if child.type == "call_expression"
            ),
            None,
        )
        if call is not None:
            function = call.child_by_field_name("function")
            if function is None or function.type != "identifier":
                return "", None
            return _javascript_text(snapshot, function), call
        identifier = next(
            (
                child
                for child in decorator.named_children
                if child.type == "identifier"
            ),
            None,
        )
        return _javascript_text(snapshot, identifier), None

    @staticmethod
    def _call_path(snapshot: bytes, call: Node) -> str | None:
        arguments = call.child_by_field_name("arguments")
        if arguments is None or len(arguments.named_children) > 1:
            return None
        if not arguments.named_children:
            return ""
        return _javascript_string(snapshot, arguments.named_children[0])

    @staticmethod
    def _join_route(prefix: str, suffix: str) -> str:
        parts = [part.strip("/") for part in (prefix, suffix) if part.strip("/")]
        return f"/{'/'.join(parts)}" if parts else "/"


class SQLAlchemyAnalyzer:
    name = "sqlalchemy"

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        if Path(source_file.path).suffix != ".py":
            return []
        try:
            tree = ast.parse(source_file.content)
            compile(tree, source_file.path, "exec")
        except (SyntaxError, ValueError, TypeError):
            return []

        constructors: set[str] = set()
        base_factories: set[str] = set()
        session_types: set[str] = set()
        declarative_bases: set[str] = set()
        first_module_binding_lines: dict[str, int] = {}
        for module_statement in tree.body:
            for name in _python_assigned_names(module_statement):
                first_module_binding_lines.setdefault(name, module_statement.lineno)
        function_definitions: dict[
            str, ast.FunctionDef | ast.AsyncFunctionDef
        ] = {}
        function_effects: dict[str, set[str]] = {}
        entities: list[FrameworkEntity] = []
        for statement in tree.body:
            if isinstance(statement, _CONTROL_FLOW_BOUNDARIES):
                constructors.clear()
                base_factories.clear()
                session_types.clear()
                declarative_bases.clear()
                function_effects.clear()
                continue
            if any(
                first_module_binding_lines[name] > statement.lineno
                for name in _python_called_function_names(statement)
                if name in first_module_binding_lines
            ):
                constructors.clear()
                base_factories.clear()
                session_types.clear()
                declarative_bases.clear()
            assigned = _python_assigned_names(statement)
            assigned.update(_python_evaluated_binding_names(statement))
            assigned.update(
                _python_called_function_effects(statement, function_effects)
            )
            alias_effects = _python_function_alias_effects(statement, function_effects)
            if "*" in set().union(*alias_effects.values(), set()) or (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.NamedExpr)
            ):
                assigned.update(set().union(*alias_effects.values(), set()))
            if isinstance(statement, ast.ClassDef):
                globals_used = _ClassGlobalCollector()
                for child in statement.body:
                    globals_used.visit(child)
                assigned.update(globals_used.names)
            for name in assigned:
                function_effects.pop(name, None)
            for bindings in (constructors, base_factories, session_types):
                if "*" in assigned:
                    bindings.clear()
                rebound = {
                    name
                    for name in bindings
                    if name.split(".", 1)[0] in assigned
                }
                bindings.difference_update(rebound)
            if "*" in assigned:
                declarative_bases.clear()
            else:
                declarative_bases.difference_update(assigned)
            if alias_effects:
                function_effects.update(alias_effects)
                continue
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.name != "sqlalchemy.orm":
                        continue
                    module = alias.asname or alias.name
                    constructors.add(f"{module}.DeclarativeBase")
                    base_factories.add(f"{module}.declarative_base")
                    session_types.add(f"{module}.Session")
                continue
            if isinstance(statement, ast.ImportFrom):
                if statement.level == 0 and statement.module == "sqlalchemy.orm":
                    for alias in statement.names:
                        if alias.name == "DeclarativeBase":
                            constructors.add(alias.asname or alias.name)
                        elif alias.name == "declarative_base":
                            base_factories.add(alias.asname or alias.name)
                        elif alias.name == "Session":
                            session_types.add(alias.asname or alias.name)
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_definitions[statement.name] = statement
                aliases = {
                    name: effects
                    for name, effects in function_effects.items()
                    if name not in function_definitions
                }
                summaries = _python_function_effect_summaries(
                    list(function_definitions.values())
                )
                function_effects = {**aliases, **summaries}
                continue
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Call)
                and _python_dotted_name(statement.value.func) in base_factories
                and not statement.value.args
                and not statement.value.keywords
            ):
                declarative_bases.add(statement.targets[0].id)
                continue
            if not isinstance(statement, ast.ClassDef):
                continue
            session = self._repository_session(statement, session_types)
            if session is not None:
                entities.append(
                    FrameworkEntity(
                        framework=self.name,
                        kind="repository",
                        name=statement.name,
                        target=session,
                        path=source_file.path,
                        line=statement.lineno,
                        end_line=statement.end_lineno or statement.lineno,
                        attributes=(("session", session),),
                    )
                )
            base_names = {
                name
                for base in statement.bases
                if (name := _python_dotted_name(base)) is not None
            }
            if (
                len(statement.bases) == 1
                and not statement.keywords
                and len(base_names & constructors) == 1
            ):
                declarative_bases.add(statement.name)
                continue
            if len(statement.bases) != 1 or statement.keywords:
                continue
            matching_bases = sorted(base_names & declarative_bases)
            if len(matching_bases) != 1:
                continue
            table = self._literal_table(statement)
            if table is None:
                continue
            declarative_bases.add(statement.name)
            entities.append(
                FrameworkEntity(
                    framework=self.name,
                    kind="model",
                    name=statement.name,
                    target=table,
                    path=source_file.path,
                    line=statement.lineno,
                    end_line=statement.end_lineno or statement.lineno,
                    attributes=(("base", matching_bases[0]), ("table", table)),
                )
            )
        return entities

    @staticmethod
    def _literal_table(statement: ast.ClassDef) -> str | None:
        values = []
        for item in statement.body:
            if not isinstance(item, (ast.Assign, ast.AnnAssign)):
                continue
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            if not any(
                isinstance(target, ast.Name) and target.id == "__tablename__"
                for target in targets
            ):
                continue
            value = item.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                values.append(value.value)
            else:
                return None
        return values[0] if len(values) == 1 else None

    @staticmethod
    def _repository_session(
        statement: ast.ClassDef, session_types: set[str]
    ) -> str | None:
        initializers = [
            item
            for item in statement.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "__init__"
        ]
        if len(initializers) != 1:
            return None
        matches = [
            name
            for argument in initializers[0].args.args
            if argument.annotation is not None
            and (name := _python_dotted_name(argument.annotation)) in session_types
        ]
        return matches[0] if len(matches) == 1 else None



class CeleryAnalyzer:
    name = "celery"

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        if Path(source_file.path).suffix != ".py":
            return []
        try:
            tree = ast.parse(source_file.content)
            compile(tree, source_file.path, "exec")
        except (SyntaxError, ValueError, TypeError):
            return []

        constructors: set[str] = set()
        applications: set[str] = set()
        shared_decorators: set[str] = set()
        first_module_binding_lines: dict[str, int] = {}
        for module_statement in tree.body:
            for name in _python_assigned_names(module_statement):
                first_module_binding_lines.setdefault(name, module_statement.lineno)
        function_definitions: dict[
            str, ast.FunctionDef | ast.AsyncFunctionDef
        ] = {}
        function_effects: dict[str, set[str]] = {}
        entities: list[FrameworkEntity] = []
        for statement in tree.body:
            if isinstance(statement, _CONTROL_FLOW_BOUNDARIES):
                constructors.clear()
                applications.clear()
                shared_decorators.clear()
                function_effects.clear()
                continue
            if any(
                first_module_binding_lines[name] > statement.lineno
                for name in _python_called_function_names(statement)
                if name in first_module_binding_lines
            ):
                constructors.clear()
                applications.clear()
                shared_decorators.clear()
            assigned = _python_assigned_names(statement)
            assigned.update(_python_evaluated_binding_names(statement))
            assigned.update(
                _python_called_function_effects(statement, function_effects)
            )
            alias_effects = _python_function_alias_effects(statement, function_effects)
            if "*" in set().union(*alias_effects.values(), set()) or (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.NamedExpr)
            ):
                assigned.update(set().union(*alias_effects.values(), set()))
            if isinstance(statement, ast.ClassDef):
                globals_used = _ClassGlobalCollector()
                for child in statement.body:
                    globals_used.visit(child)
                assigned.update(globals_used.names)
            for name in assigned:
                function_effects.pop(name, None)
            if "*" in assigned:
                constructors.clear()
                applications.clear()
                shared_decorators.clear()
            constructors.difference_update(assigned)
            applications.difference_update(assigned)
            shared_decorators.difference_update(assigned)
            if alias_effects:
                function_effects.update(alias_effects)
                continue
            if isinstance(statement, ast.ImportFrom):
                if statement.level == 0 and statement.module == "celery":
                    for alias in statement.names:
                        if alias.name == "Celery":
                            constructors.add(alias.asname or alias.name)
                        elif alias.name == "shared_task":
                            shared_decorators.add(alias.asname or alias.name)
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_definitions[statement.name] = statement
                aliases = {
                    name: effects
                    for name, effects in function_effects.items()
                    if name not in function_definitions
                }
                summaries = _python_function_effect_summaries(
                    list(function_definitions.values())
                )
                function_effects = {**aliases, **summaries}
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Call)
                and _python_dotted_name(statement.value.func) in constructors
                and len(statement.value.args) <= 1
                and (
                    not statement.value.args
                    or (
                        isinstance(statement.value.args[0], ast.Constant)
                        and isinstance(statement.value.args[0].value, str)
                    )
                )
                and not statement.value.keywords
            ):
                applications.add(statement.targets[0].id)
                continue
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            matches = []
            for decorator in statement.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if decorator.args or any(
                    keyword.arg is None for keyword in decorator.keywords
                ):
                    continue
                dotted = _python_dotted_name(decorator.func)
                if dotted in shared_decorators:
                    application = "shared"
                elif dotted is not None and dotted.endswith(".task"):
                    application = dotted.rsplit(".", 1)[0]
                    if application not in applications:
                        continue
                else:
                    continue
                task = statement.name
                for keyword in decorator.keywords:
                    if keyword.arg != "name":
                        continue
                    if not (
                        isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        task = ""
                    else:
                        task = keyword.value.value
                if task:
                    matches.append((application, task, decorator.lineno))
            if len(matches) != 1:
                continue
            application, task, line = matches[0]
            entities.append(
                FrameworkEntity(
                    framework=self.name,
                    kind="job",
                    name=statement.name,
                    target=task,
                    path=source_file.path,
                    line=line,
                    end_line=statement.end_lineno or statement.lineno,
                    attributes=(("application", application), ("task", task)),
                )
            )
        return entities


class ReactAnalyzer:
    name = "react"
    _function_nodes = {
        "arrow_function",
        "function_declaration",
        "function_expression",
        "generator_function",
        "generator_function_declaration",
    }

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        parsed = _javascript_tree(source_file)
        if parsed is None:
            return []
        tree, snapshot = parsed
        hooks: dict[str, str] = {}
        react_imported = False
        for statement in tree.root_node.named_children:
            if statement.type != "import_statement":
                continue
            if _javascript_string(
                snapshot, statement.child_by_field_name("source")
            ) != "react":
                continue
            react_imported = True
            self._collect_hooks(snapshot, statement, hooks)
        if not react_imported:
            return []

        function_effects = _javascript_function_effects(snapshot, tree)
        entities: list[FrameworkEntity] = []
        for statement in tree.root_node.named_children:
            if statement.type == "import_statement":
                continue
            for name in _javascript_assignment_roots(
                snapshot, statement, function_effects
            ):
                if name == "*":
                    hooks.clear()
                    function_effects.clear()
                    continue
                hooks.pop(name, None)
                function_effects.pop(name, None)
            declaration = (
                statement.child_by_field_name("declaration")
                if statement.type == "export_statement"
                else statement
            )
            if declaration is None:
                continue
            if declaration.type == "function_declaration":
                name_node = declaration.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _javascript_text(snapshot, name_node)
                if name[:1].isupper() and self._returns_jsx(declaration):
                    entities.append(
                        self._entity(source_file, declaration, "component", name, None)
                    )
                continue
            if declaration.type not in {"lexical_declaration", "variable_declaration"}:
                continue
            for declarator in declaration.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if (
                    name_node is None
                    or name_node.type != "identifier"
                    or value is None
                    or value.type != "arrow_function"
                ):
                    continue
                name = _javascript_text(snapshot, name_node)
                primitive = self._called_hook(snapshot, value, hooks)
                if name.startswith("use") and primitive is not None:
                    entities.append(
                        self._entity(
                            source_file,
                            declarator,
                            "hook",
                            name,
                            primitive,
                            (("primitive", primitive),),
                        )
                    )
        return entities

    @classmethod
    def _returns_jsx(cls, function: Node) -> bool:
        def visit(node: Node) -> bool:
            if node != function and node.type in cls._function_nodes:
                return False
            if node.type == "return_statement":
                return any(cls._contains_jsx(child) for child in node.named_children)
            return any(visit(child) for child in node.named_children)

        return visit(function)

    @classmethod
    def _contains_jsx(cls, node: Node) -> bool:
        return node.type.startswith("jsx_") or any(
            cls._contains_jsx(child) for child in node.named_children
        )

    @staticmethod
    def _collect_hooks(snapshot: bytes, node: Node, hooks: dict[str, str]) -> None:
        if node.type == "import_specifier":
            name_node = node.child_by_field_name("name")
            alias_node = node.child_by_field_name("alias")
            if name_node is not None:
                imported = _javascript_text(snapshot, name_node)
                if imported.startswith("use"):
                    local = (
                        _javascript_text(snapshot, alias_node)
                        if alias_node is not None
                        else imported
                    )
                    hooks[local] = imported
        for child in node.named_children:
            ReactAnalyzer._collect_hooks(snapshot, child, hooks)

    @staticmethod
    def _called_hook(
        snapshot: bytes, function: Node, hooks: dict[str, str]
    ) -> str | None:
        body = function.child_by_field_name("body")
        if body is None or body.type != "call_expression":
            return None
        callee = body.child_by_field_name("function")
        if callee is None or callee.type != "identifier":
            return None
        local = _javascript_text(snapshot, callee)
        parameters = function.child_by_field_name(
            "parameters"
        ) or function.child_by_field_name("parameter")
        if parameters is not None:
            pending = [parameters]
            while pending:
                parameter = pending.pop()
                if (
                    parameter.type == "identifier"
                    and _javascript_text(snapshot, parameter) == local
                ):
                    return None
                pending.extend(parameter.named_children)
        return hooks.get(local)

    def _entity(
        self,
        source_file: SourceFile,
        node: Node,
        kind: str,
        name: str,
        target: str | None,
        attributes: tuple[tuple[str, str], ...] = (),
    ) -> FrameworkEntity:
        return FrameworkEntity(
            framework=self.name,
            kind=kind,
            name=name,
            target=target,
            path=source_file.path,
            line=node.start_point.row + 1,
            end_line=node.end_point.row + 1,
            attributes=attributes,
        )


class NextJSAnalyzer:
    name = "nextjs"
    _http_methods = frozenset(
        {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
    )

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        route = self._app_route(source_file.path)
        parsed = _javascript_tree(source_file)
        if parsed is None:
            return []
        tree, snapshot = parsed
        in_app = "app" in Path(source_file.path).parts
        directives: set[str] = set()
        if in_app:
            for statement in tree.root_node.named_children:
                if statement.type != "expression_statement" or len(
                    statement.named_children
                ) != 1:
                    break
                directive = _javascript_string(snapshot, statement.named_children[0])
                if directive is None:
                    break
                directives.add(directive)
        if {"use client", "use server"}.issubset(directives):
            return []
        server_module = "use server" in directives
        if route is None and not server_module:
            return []
        binding_counts: dict[str, int] = {}
        export_counts: dict[str, int] = {}
        for statement in tree.root_node.named_children:
            bindings = self._declared_names(snapshot, statement)
            bindings.update(self._import_bindings(snapshot, statement))
            for name in bindings:
                binding_counts[name] = binding_counts.get(name, 0) + 1
            for name in self._export_names(snapshot, statement):
                export_counts[name] = export_counts.get(name, 0) + 1
        if any(count > 1 for count in binding_counts.values()) or any(
            count > 1 for count in export_counts.values()
        ):
            return []
        bound_names = set(binding_counts)
        for statement in tree.root_node.named_children:
            if statement.type != "export_statement" or statement.child_by_field_name(
                "source"
            ) is not None:
                continue
            if not self._local_export_names(snapshot, statement).issubset(bound_names):
                return []
        candidates: dict[str, list[Node]] = {}
        invalid_names: set[str] = set()
        invalidate_all = False
        function_effects = _javascript_function_effects(snapshot, tree)
        for statement in tree.root_node.named_children:
            declaration = (
                statement.child_by_field_name("declaration")
                if statement.type == "export_statement"
                and not any(child.type == "default" for child in statement.children)
                else None
            )
            if declaration is not None and declaration.type == "function_declaration":
                name_node = declaration.child_by_field_name("name")
                if name_node is not None:
                    name = _javascript_text(snapshot, name_node)
                    candidates.setdefault(name, []).append(declaration)
                    continue
            invalid_names.update(self._declared_names(snapshot, statement))
            for name in _javascript_assignment_roots(
                snapshot, statement, function_effects
            ):
                if name == "*":
                    invalidate_all = True
                else:
                    invalid_names.add(name)

        entities: list[FrameworkEntity] = []
        if invalidate_all:
            return entities
        for method, declarations in candidates.items():
            if len(declarations) != 1 or method in invalid_names:
                continue
            declaration = declarations[0]
            if route is not None and method in self._http_methods:
                entities.append(
                    FrameworkEntity(
                        framework=self.name,
                        kind="route",
                        name=f"{method} {route}",
                        target=method,
                        path=source_file.path,
                        line=declaration.start_point.row + 1,
                        end_line=declaration.end_point.row + 1,
                        attributes=(("method", method), ("route", route)),
                    )
                )
            if server_module:
                entities.append(
                    FrameworkEntity(
                        framework=self.name,
                        kind="action",
                        name=method,
                        target=None,
                        path=source_file.path,
                        line=declaration.start_point.row + 1,
                        end_line=declaration.end_point.row + 1,
                        attributes=(("directive", "use server"),),
                    )
                )
        return entities

    @staticmethod
    def _import_bindings(snapshot: bytes, statement: Node) -> set[str]:
        if statement.type != "import_statement":
            return set()
        clause = next(
            (child for child in statement.named_children if child.type == "import_clause"),
            None,
        )
        if clause is None:
            return set()
        names: set[str] = set()
        for child in clause.named_children:
            if child.type == "identifier":
                names.add(_javascript_text(snapshot, child))
            elif child.type == "named_imports":
                for specifier in child.named_children:
                    if specifier.type != "import_specifier":
                        continue
                    name = specifier.child_by_field_name("alias") or specifier.child_by_field_name("name")
                    if name is not None:
                        names.add(_javascript_text(snapshot, name))
            elif child.type == "namespace_import":
                name = next(
                    (item for item in child.named_children if item.type == "identifier"),
                    None,
                )
                if name is not None:
                    names.add(_javascript_text(snapshot, name))
        return names

    @staticmethod
    def _export_names(snapshot: bytes, statement: Node) -> list[str]:
        if statement.type != "export_statement":
            return []
        if any(child.type == "default" for child in statement.children):
            return ["default"]
        declaration = statement.child_by_field_name("declaration")
        if declaration is not None:
            name = declaration.child_by_field_name("name")
            return [_javascript_text(snapshot, name)] if name is not None else []
        names: list[str] = []
        for child in statement.named_children:
            if child.type == "export_clause":
                for specifier in child.named_children:
                    if specifier.type != "export_specifier":
                        continue
                    name = specifier.child_by_field_name("alias") or specifier.child_by_field_name("name")
                    if name is not None:
                        names.append(_javascript_text(snapshot, name))
            elif child.type == "namespace_export":
                name = next(
                    (item for item in child.named_children if item.type == "identifier"),
                    None,
                )
                if name is not None:
                    names.append(_javascript_text(snapshot, name))
        return names

    @staticmethod
    def _local_export_names(snapshot: bytes, statement: Node) -> set[str]:
        names: set[str] = set()
        for child in statement.named_children:
            if child.type != "export_clause":
                continue
            for specifier in child.named_children:
                if specifier.type != "export_specifier":
                    continue
                name = specifier.child_by_field_name("name")
                if name is not None:
                    names.add(_javascript_text(snapshot, name))
        return names

    @staticmethod
    def _declared_names(snapshot: bytes, statement: Node) -> set[str]:
        declaration = (
            statement.child_by_field_name("declaration")
            if statement.type == "export_statement"
            else statement
        )
        if declaration is None:
            return set()
        if declaration.type in {"function_declaration", "class_declaration"}:
            name = declaration.child_by_field_name("name")
            return {_javascript_text(snapshot, name)} if name is not None else set()
        if declaration.type not in {"lexical_declaration", "variable_declaration"}:
            return set()
        names: set[str] = set()
        for child in declaration.named_children:
            if child.type != "variable_declarator":
                continue
            target = child.child_by_field_name("name")
            if target is None:
                continue
            if target.type == "identifier":
                names.add(_javascript_text(snapshot, target))
            else:
                names.update(
                    _javascript_text(snapshot, item)
                    for item in target.named_children
                    if item.type in {
                        "identifier",
                        "shorthand_property_identifier_pattern",
                    }
                )
        return names

    @staticmethod
    def _app_route(path: str) -> str | None:
        source = Path(path)
        if source.stem != "route" or source.suffix.lower() not in {
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
        }:
            return None
        parts = source.parts
        try:
            app_index = parts.index("app")
        except ValueError:
            return None
        route_parts = []
        for part in parts[app_index + 1 : -1]:
            if (part.startswith("(") and part.endswith(")")) or part.startswith("@"):
                continue
            if part.startswith("[[...") and part.endswith("]]" ):
                route_parts.append(f"*{part[5:-2]}")
            elif part.startswith("[...") and part.endswith("]"):
                route_parts.append(f"*{part[4:-1]}")
            elif part.startswith("[") and part.endswith("]"):
                route_parts.append(f":{part[1:-1]}")
            else:
                route_parts.append(part)
        return f"/{'/'.join(route_parts)}" if route_parts else "/"


class AngularAnalyzer:
    name = "angular"

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        parsed = _javascript_tree(source_file)
        if parsed is None:
            return []
        tree, snapshot = parsed
        decorators: dict[str, str] = {}
        route_types: set[str] = set()
        for statement in tree.root_node.named_children:
            if statement.type != "import_statement":
                continue
            source = _javascript_string(
                snapshot, statement.child_by_field_name("source")
            )
            if source == "@angular/core":
                self._collect_decorators(snapshot, statement, decorators)
            elif source == "@angular/router":
                self._collect_route_types(snapshot, statement, route_types)
        if not decorators and not route_types:
            return []

        function_effects = _javascript_function_effects(snapshot, tree)
        entities: list[FrameworkEntity] = []
        for statement in tree.root_node.named_children:
            if statement.type == "import_statement":
                continue
            for name in _javascript_assignment_roots(
                snapshot, statement, function_effects
            ):
                if name == "*":
                    decorators.clear()
                    route_types.clear()
                    function_effects.clear()
                    continue
                decorators.pop(name, None)
                route_types.discard(name)
                function_effects.pop(name, None)
            declaration = (
                statement.child_by_field_name("declaration")
                if statement.type == "export_statement"
                else statement
            )
            if declaration is None:
                continue
            if declaration.type != "class_declaration":
                if route_types:
                    entities.extend(
                        self._route_entities(
                            source_file, snapshot, declaration, route_types
                        )
                    )
                continue
            name_node = declaration.child_by_field_name("name")
            if name_node is None:
                continue
            matches: list[tuple[str, str, Node]] = []
            decorator_nodes = (
                [child for child in statement.named_children if child.type == "decorator"]
                if statement.type == "export_statement"
                else [child for child in declaration.named_children if child.type == "decorator"]
            )
            for decorator in decorator_nodes:
                parsed_decorator = self._parse_decorator(
                    snapshot, decorator, decorators
                )
                if parsed_decorator is not None:
                    kind, target = parsed_decorator
                    matches.append((kind, target, decorator))
            if len(matches) != 1:
                continue
            kind, target, decorator = matches[0]
            attribute = "selector" if kind == "component" else "provided_in"
            entities.append(
                FrameworkEntity(
                    framework=self.name,
                    kind=kind,
                    name=_javascript_text(snapshot, name_node),
                    target=target,
                    path=source_file.path,
                    line=decorator.start_point.row + 1,
                    end_line=declaration.end_point.row + 1,
                    attributes=((attribute, target),),
                )
            )
        return entities

    @staticmethod
    def _collect_route_types(
        snapshot: bytes, node: Node, route_types: set[str]
    ) -> None:
        if node.type == "import_specifier":
            name_node = node.child_by_field_name("name")
            alias_node = node.child_by_field_name("alias")
            if name_node is not None and _javascript_text(snapshot, name_node) == "Routes":
                route_types.add(
                    _javascript_text(snapshot, alias_node)
                    if alias_node is not None
                    else "Routes"
                )
        for child in node.named_children:
            AngularAnalyzer._collect_route_types(snapshot, child, route_types)

    def _route_entities(
        self,
        source_file: SourceFile,
        snapshot: bytes,
        declaration: Node,
        route_types: set[str],
    ) -> list[FrameworkEntity]:
        if declaration.type not in {"lexical_declaration", "variable_declaration"}:
            return []
        entities: list[FrameworkEntity] = []
        for declarator in declaration.named_children:
            if declarator.type != "variable_declarator":
                continue
            annotation = declarator.child_by_field_name("type")
            value = declarator.child_by_field_name("value")
            if (
                annotation is None
                or len(annotation.named_children) != 1
                or _javascript_text(snapshot, annotation.named_children[0])
                not in route_types
                or value is None
                or value.type != "array"
            ):
                continue
            for route_object in value.named_children:
                parsed = self._component_route(snapshot, route_object)
                if parsed is None:
                    continue
                route, handler = parsed
                entities.append(
                    FrameworkEntity(
                        framework=self.name,
                        kind="route",
                        name=f"ANY {route}",
                        target=handler,
                        path=source_file.path,
                        line=route_object.start_point.row + 1,
                        end_line=route_object.end_point.row + 1,
                        attributes=(
                            ("handler", handler),
                            ("method", "ANY"),
                            ("route", route),
                        ),
                    )
                )
        return entities

    @staticmethod
    def _component_route(snapshot: bytes, node: Node) -> tuple[str, str] | None:
        if node.type != "object":
            return None
        paths: list[str] = []
        handlers: list[str] = []
        for pair in node.named_children:
            if pair.type != "pair":
                continue
            key = pair.child_by_field_name("key")
            value = pair.child_by_field_name("value")
            if key is None or value is None:
                continue
            property_name = _javascript_text(snapshot, key)
            if property_name == "path":
                literal = _javascript_string(snapshot, value)
                if literal is not None:
                    paths.append(literal)
            elif property_name == "component" and value.type == "identifier":
                handlers.append(_javascript_text(snapshot, value))
        if len(paths) != 1 or len(handlers) != 1:
            return None
        route = f"/{paths[0].strip('/')}" if paths[0] else "/"
        return route, handlers[0]

    @staticmethod
    def _collect_decorators(
        snapshot: bytes, node: Node, decorators: dict[str, str]
    ) -> None:
        if node.type == "import_specifier":
            name_node = node.child_by_field_name("name")
            alias_node = node.child_by_field_name("alias")
            if name_node is not None:
                imported = _javascript_text(snapshot, name_node)
                if imported in {"Component", "Injectable"}:
                    local = (
                        _javascript_text(snapshot, alias_node)
                        if alias_node is not None
                        else imported
                    )
                    decorators[local] = imported
        for child in node.named_children:
            AngularAnalyzer._collect_decorators(snapshot, child, decorators)

    @staticmethod
    def _parse_decorator(
        snapshot: bytes, decorator: Node, decorators: dict[str, str]
    ) -> tuple[str, str] | None:
        if len(decorator.named_children) != 1:
            return None
        call = decorator.named_children[0]
        if call.type != "call_expression":
            return None
        callee = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if callee is None or callee.type != "identifier" or arguments is None:
            return None
        imported = decorators.get(_javascript_text(snapshot, callee))
        if imported not in {"Component", "Injectable"}:
            return None
        values = [child for child in arguments.named_children]
        if len(values) != 1 or values[0].type != "object":
            return None
        metadata_key = "selector" if imported == "Component" else "providedIn"
        metadata = []
        for pair in values[0].named_children:
            if pair.type != "pair":
                continue
            key = pair.child_by_field_name("key")
            value = pair.child_by_field_name("value")
            if (
                key is not None
                and _javascript_text(snapshot, key) == metadata_key
                and (literal := _javascript_string(snapshot, value)) is not None
            ):
                metadata.append(literal)
        if len(metadata) != 1:
            return None
        kind = "component" if imported == "Component" else "service"
        return kind, metadata[0]


class PrismaAnalyzer:
    name = "prisma"
    _model_start = re.compile(r"^\s*model\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{\s*$")
    _field = re.compile(
        r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+"
        r"(?P<type>[A-Za-z_][A-Za-z0-9_]*)(?P<arity>\[\]|\?)?"
        r"(?P<attributes>\s+@.*)?\s*$"
    )
    _mapped_table = re.compile(
        r'^\s*@@map\(\s*("(?:\\.|[^"\\])*")\s*\)\s*$'
    )
    _constraint = re.compile(
        r"^@@(?P<kind>id|unique)\(\[\s*(?P<fields>"
        r"[A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)"
        r"\s*\]\)$"
    )
    _mapped_field = re.compile(r'@map\(\s*("(?:\\.|[^"\\])*")\s*\)')

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        if Path(source_file.path).suffix.lower() == ".prisma":
            return self._extract_schema_models(source_file)
        return self._extract_client_repositories(source_file)

    def _extract_schema_models(
        self, source_file: SourceFile
    ) -> list[FrameworkEntity]:
        content = self._without_comments(source_file.content)
        if content is None:
            return []
        lines = content.splitlines()
        blocks: list[tuple[str, int, int, list[str]]] = []
        model_names: set[str] = set()
        index = 0
        while index < len(lines):
            if not lines[index].strip():
                index += 1
                continue
            start = self._model_start.fullmatch(lines[index])
            if start is None:
                return []
            name = start.group(1)
            if name in model_names:
                return []
            model_names.add(name)
            start_line = index + 1
            index += 1
            body: list[str] = []
            while index < len(lines) and not re.fullmatch(r"\s*}\s*", lines[index]):
                body.append(lines[index])
                index += 1
            if index >= len(lines):
                return []
            blocks.append((name, start_line, index + 1, body))
            index += 1

        scalar_types = {
            "BigInt", "Boolean", "Bytes", "DateTime", "Decimal",
            "Float", "Int", "Json", "String",
        }
        validated: list[tuple[str, str, int, int]] = []
        physical_tables: set[str] = set()
        for name, start_line, end_line, body in blocks:
            mapped_tables: list[str] = []
            field_names: set[str] = set()
            physical_fields: set[str] = set()
            constraints: list[tuple[str, list[str]]] = []
            primary_keys = 0
            unique = False
            for line in body:
                mapping = self._mapped_table.fullmatch(line)
                if mapping is not None:
                    try:
                        decoded = json.loads(mapping.group(1))
                    except (json.JSONDecodeError, TypeError):
                        return []
                    if not isinstance(decoded, str) or not decoded:
                        return []
                    mapped_tables.append(decoded)
                elif not line.strip():
                    continue
                elif line.lstrip().startswith("@@"):
                    constraint = self._constraint.fullmatch(line.strip())
                    if constraint is not None:
                        fields = [
                            field.strip()
                            for field in constraint.group("fields").split(",")
                        ]
                        constraints.append((constraint.group("kind"), fields))
                        if constraint.group("kind") == "id":
                            primary_keys += 1
                        unique = True
                        continue
                    if line.strip().startswith(("@@id", "@@unique", "@@schema")):
                        return []
                    if not self._valid_attributes(line.strip(), block=True):
                        return []
                elif (field := self._field.fullmatch(line)) is not None:
                    field_name = field.group("name")
                    if field_name in field_names or field.group("type") not in scalar_types:
                        return []
                    field_names.add(field_name)
                    attributes = field.group("attributes") or ""
                    physical_name = field_name
                    if attributes:
                        attributes = attributes.strip()
                        if "@relation" in attributes or not self._valid_attributes(
                            attributes, block=False
                        ) or not self._valid_default(
                            attributes,
                            field.group("type"),
                            field.group("arity"),
                        ):
                            return []
                        if re.search(r"@(?:id|unique)(?=\s|$)", attributes):
                            unique = True
                        if re.search(r"@id(?=\s|$)", attributes):
                            primary_keys += 1
                        mapped_fields = self._mapped_field.findall(attributes)
                        if len(mapped_fields) > 1:
                            return []
                        if mapped_fields:
                            try:
                                physical_name = json.loads(mapped_fields[0])
                            except (json.JSONDecodeError, TypeError):
                                return []
                    if (
                        not isinstance(physical_name, str)
                        or not physical_name
                        or physical_name in physical_fields
                    ):
                        return []
                    physical_fields.add(physical_name)
                else:
                    return []
            if any(
                len(fields) != len(set(fields))
                or not set(fields).issubset(field_names)
                for _kind, fields in constraints
            ):
                return []
            if (
                not field_names
                or not unique
                or primary_keys > 1
                or len(mapped_tables) > 1
            ):
                return []
            table = mapped_tables[0] if mapped_tables else name
            if table in physical_tables:
                return []
            physical_tables.add(table)
            validated.append(
                (name, table, start_line, end_line)
            )
        entities = []
        for name, table, start_line, end_line in validated:
            entities.append(
                FrameworkEntity(
                    framework=self.name,
                    kind="model",
                    name=name,
                    target=table,
                    path=source_file.path,
                    line=start_line,
                    end_line=end_line,
                    attributes=(("model", name), ("table", table)),
                )
            )
        return entities

    @classmethod
    def _valid_default(
        cls, attributes: str, field_type: str, arity: str | None
    ) -> bool:
        starts = [
            match.end()
            for match in re.finditer(r"(?:^|\s)@default\(", attributes)
        ]
        if not starts:
            return True
        if len(starts) != 1 or arity == "[]":
            return False
        start = starts[0]
        depth = 1
        quoted = False
        escaped = False
        index = start
        while index < len(attributes):
            character = attributes[index]
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        if depth != 0:
            return False
        value = attributes[start:index].strip()
        functions = {
            "autoincrement()": {"BigInt", "Int"},
            "cuid()": {"String"},
            "now()": {"DateTime"},
            "ulid()": {"String"},
            "uuid()": {"String"},
        }
        if value in functions:
            return field_type in functions[value]
        if field_type in {"BigInt", "Int"}:
            return re.fullmatch(r"[+-]?\d+", value) is not None
        if field_type in {"Decimal", "Float"}:
            return re.fullmatch(
                r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value
            ) is not None
        if field_type == "Boolean":
            return value in {"false", "true"}
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return False
        if field_type == "String":
            return isinstance(decoded, str)
        if field_type == "DateTime" and isinstance(decoded, str):
            try:
                datetime.fromisoformat(decoded.replace("Z", "+00:00"))
            except ValueError:
                return False
            return True
        if field_type == "Json" and isinstance(decoded, str):
            try:
                json.loads(
                    decoded,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(value)
                    ),
                    object_pairs_hook=cls._unique_json_object,
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                return False
            return True
        return False

    @staticmethod
    def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    @staticmethod
    def _valid_attributes(value: str, *, block: bool) -> bool:
        allowed = (
            {"id", "unique", "index", "fulltext", "map", "ignore", "schema"}
            if block
            else {"id", "default", "unique", "relation", "map", "updatedAt", "ignore"}
        )
        marker = "@@" if block else "@"
        index = 0
        while index < len(value):
            while index < len(value) and value[index].isspace():
                index += 1
            if not value.startswith(marker, index):
                return False
            index += len(marker)
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", value[index:])
            if match is None:
                return False
            attribute = match.group(0)
            root = attribute.split(".", 1)[0]
            if root not in allowed and not (not block and root == "db"):
                return False
            index += len(attribute)
            has_arguments = index < len(value) and value[index] == "("
            requires_arguments = root in (
                {"id", "unique", "index", "fulltext", "map", "schema"}
                if block
                else {"default", "relation", "map"}
            )
            if requires_arguments and not has_arguments:
                return False
            if not block and root in {"id", "unique", "updatedAt", "ignore"} and has_arguments:
                return False
            if root == "db" and "." not in attribute:
                return False
            if has_arguments:
                depth = 0
                bracket_depth = 0
                quoted = False
                escaped = False
                valid_content = True
                has_content = False
                while index < len(value):
                    character = value[index]
                    if quoted:
                        if escaped:
                            escaped = False
                        elif character == "\\":
                            escaped = True
                        elif character == '"':
                            quoted = False
                    elif character == '"':
                        quoted = True
                        has_content = True
                    elif character == "(":
                        depth += 1
                    elif character == ")":
                        depth -= 1
                        if depth == 0:
                            index += 1
                            break
                    elif character == "[":
                        bracket_depth += 1
                    elif character == "]":
                        bracket_depth -= 1
                        if bracket_depth < 0:
                            valid_content = False
                    elif not (
                        character.isspace()
                        or character.isalnum()
                        or character in "_.,:+-?"
                    ):
                        valid_content = False
                    elif character.isalnum() or character == "_":
                        has_content = True
                    index += 1
                if (
                    depth != 0
                    or bracket_depth != 0
                    or quoted
                    or not valid_content
                    or (requires_arguments and not has_content)
                ):
                    return False
            if index < len(value) and not value[index].isspace():
                return False
        return True

    @staticmethod
    def _without_comments(content: str) -> str | None:
        masked = list(content)
        index = 0
        state = "code"
        while index < len(content):
            character = content[index]
            following = content[index + 1] if index + 1 < len(content) else ""
            if state == "code":
                if character == '"':
                    state = "string"
                elif character == "/" and following == "/":
                    masked[index] = masked[index + 1] = " "
                    state = "line_comment"
                    index += 1
                elif character == "/" and following == "*":
                    masked[index] = masked[index + 1] = " "
                    state = "block_comment"
                    index += 1
            elif state == "string":
                if character == "\\":
                    index += 1
                elif character == '"':
                    state = "code"
            elif state == "line_comment":
                if character == "\n":
                    state = "code"
                else:
                    masked[index] = " "
            elif state == "block_comment":
                if character == "/" and following == "*":
                    return None
                if character == "*" and following == "/":
                    masked[index] = masked[index + 1] = " "
                    state = "code"
                    index += 1
                elif character != "\n":
                    masked[index] = " "
            index += 1
        if state in {"block_comment", "string"}:
            return None
        return "".join(masked)

    def _extract_client_repositories(
        self, source_file: SourceFile
    ) -> list[FrameworkEntity]:
        parsed = _javascript_tree(source_file)
        if parsed is None:
            return []
        tree, snapshot = parsed
        constructors: set[str] = set()
        clients: set[str] = set()
        function_effects = _javascript_function_effects(snapshot, tree)
        entities: list[FrameworkEntity] = []
        for statement in tree.root_node.named_children:
            if statement.type == "import_statement":
                if _javascript_string(
                    snapshot, statement.child_by_field_name("source")
                ) == "@prisma/client":
                    self._collect_client_imports(snapshot, statement, constructors)
                continue
            for name in _javascript_assignment_roots(
                snapshot, statement, function_effects
            ):
                if name == "*":
                    constructors.clear()
                    clients.clear()
                    function_effects.clear()
                    continue
                constructors.discard(name)
                clients.discard(name)
                function_effects.pop(name, None)
            declaration = (
                statement.child_by_field_name("declaration")
                if statement.type == "export_statement"
                else statement
            )
            if declaration is None or declaration.type not in {
                "lexical_declaration",
                "variable_declaration",
            }:
                continue
            for declarator in declaration.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if name_node is None or name_node.type != "identifier":
                    continue
                name = _javascript_text(snapshot, name_node)
                constructors.discard(name)
                clients.discard(name)
                if value is None:
                    continue
                if value.type == "new_expression":
                    constructor = value.child_by_field_name("constructor")
                    if (
                        constructor is not None
                        and constructor.type == "identifier"
                        and _javascript_text(snapshot, constructor) in constructors
                    ):
                        clients.add(name)
                    continue
                if value.type != "member_expression":
                    continue
                client = value.child_by_field_name("object")
                model = value.child_by_field_name("property")
                if (
                    client is None
                    or client.type != "identifier"
                    or _javascript_text(snapshot, client) not in clients
                    or model is None
                    or model.type != "property_identifier"
                ):
                    continue
                client_name = _javascript_text(snapshot, client)
                model_name = _javascript_text(snapshot, model)
                entities.append(
                    FrameworkEntity(
                        framework=self.name,
                        kind="repository",
                        name=name,
                        target=model_name,
                        path=source_file.path,
                        line=declarator.start_point.row + 1,
                        end_line=declarator.end_point.row + 1,
                        attributes=(("client", client_name), ("model", model_name)),
                    )
                )
        return entities

    @staticmethod
    def _collect_client_imports(
        snapshot: bytes, node: Node, constructors: set[str]
    ) -> None:
        if node.type == "import_specifier":
            name_node = node.child_by_field_name("name")
            alias_node = node.child_by_field_name("alias")
            if (
                name_node is not None
                and _javascript_text(snapshot, name_node) == "PrismaClient"
            ):
                constructors.add(
                    _javascript_text(snapshot, alias_node)
                    if alias_node is not None
                    else "PrismaClient"
                )
        for child in node.named_children:
            PrismaAnalyzer._collect_client_imports(snapshot, child, constructors)


class TypeORMAnalyzer:
    name = "typeorm"

    def extract_entities(self, source_file: SourceFile) -> list[FrameworkEntity]:
        parsed = _javascript_tree(source_file)
        if parsed is None:
            return []
        tree, snapshot = parsed
        imports: dict[str, str] = {}
        for statement in tree.root_node.named_children:
            if statement.type != "import_statement" or _javascript_string(
                snapshot, statement.child_by_field_name("source")
            ) != "typeorm":
                continue
            self._collect_imports(snapshot, statement, imports)
        if not imports:
            return []

        function_effects = _javascript_function_effects(snapshot, tree)
        entities: list[FrameworkEntity] = []
        for statement in tree.root_node.named_children:
            if statement.type == "import_statement":
                continue
            for name in _javascript_assignment_roots(
                snapshot, statement, function_effects
            ):
                if name == "*":
                    imports.clear()
                    function_effects.clear()
                    continue
                imports.pop(name, None)
                function_effects.pop(name, None)
            declaration = (
                statement.child_by_field_name("declaration")
                if statement.type == "export_statement"
                else statement
            )
            if declaration is None or declaration.type != "class_declaration":
                continue
            name_node = declaration.child_by_field_name("name")
            if name_node is None:
                continue
            name = _javascript_text(snapshot, name_node)
            decorator_nodes = (
                [child for child in statement.named_children if child.type == "decorator"]
                if statement.type == "export_statement"
                else [child for child in declaration.named_children if child.type == "decorator"]
            )
            tables = [
                table
                for decorator in decorator_nodes
                if (table := self._entity_table(snapshot, decorator, imports, name))
                is not None
            ]
            if len(tables) == 1:
                entities.append(
                    FrameworkEntity(
                        framework=self.name,
                        kind="model",
                        name=name,
                        target=tables[0],
                        path=source_file.path,
                        line=declaration.start_point.row + 1,
                        end_line=declaration.end_point.row + 1,
                        attributes=(("entity", name), ("table", tables[0])),
                    )
                )
            repository = self._repository_model(snapshot, declaration, imports)
            if repository is not None:
                repository_name, model = repository
                entities.append(
                    FrameworkEntity(
                        framework=self.name,
                        kind="repository",
                        name=name,
                        target=model,
                        path=source_file.path,
                        line=declaration.start_point.row + 1,
                        end_line=declaration.end_point.row + 1,
                        attributes=(
                            ("model", model),
                            ("repository", repository_name),
                        ),
                    )
                )
        return entities

    @staticmethod
    def _collect_imports(snapshot: bytes, node: Node, imports: dict[str, str]) -> None:
        if node.type == "import_specifier":
            name_node = node.child_by_field_name("name")
            alias_node = node.child_by_field_name("alias")
            if name_node is not None:
                imported = _javascript_text(snapshot, name_node)
                if imported in {"Entity", "Repository"}:
                    local = (
                        _javascript_text(snapshot, alias_node)
                        if alias_node is not None
                        else imported
                    )
                    imports[local] = imported
        for child in node.named_children:
            TypeORMAnalyzer._collect_imports(snapshot, child, imports)

    @staticmethod
    def _entity_table(
        snapshot: bytes,
        decorator: Node,
        imports: dict[str, str],
        class_name: str,
    ) -> str | None:
        if len(decorator.named_children) != 1:
            return None
        call = decorator.named_children[0]
        if call.type != "call_expression":
            return None
        callee = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if (
            callee is None
            or callee.type != "identifier"
            or imports.get(_javascript_text(snapshot, callee)) != "Entity"
            or arguments is None
        ):
            return None
        values = list(arguments.named_children)
        if not values:
            return class_name
        if len(values) != 1:
            return None
        return _javascript_string(snapshot, values[0])

    @staticmethod
    def _repository_model(
        snapshot: bytes, declaration: Node, imports: dict[str, str]
    ) -> tuple[str, str] | None:
        heritage = [
            child for child in declaration.named_children if child.type == "class_heritage"
        ]
        if len(heritage) != 1:
            return None
        extensions = [
            child for child in heritage[0].named_children if child.type == "extends_clause"
        ]
        if len(extensions) != 1:
            return None
        base = extensions[0].child_by_field_name("value")
        arguments = extensions[0].child_by_field_name("type_arguments")
        if (
            base is None
            or base.type != "identifier"
            or imports.get(_javascript_text(snapshot, base)) != "Repository"
            or arguments is None
            or len(arguments.named_children) != 1
            or arguments.named_children[0].type != "type_identifier"
        ):
            return None
        return _javascript_text(snapshot, base), _javascript_text(
            snapshot, arguments.named_children[0]
        )


_FASTAPI_ANALYZER = FastAPIAnalyzer()
_FLASK_ANALYZER = FlaskAnalyzer()
_DJANGO_ANALYZER = DjangoAnalyzer()
_EXPRESS_ANALYZER = ExpressAnalyzer()
_NESTJS_ANALYZER = NestJSAnalyzer()
_SQLALCHEMY_ANALYZER = SQLAlchemyAnalyzer()
_CELERY_ANALYZER = CeleryAnalyzer()
_REACT_ANALYZER = ReactAnalyzer()
_NEXTJS_ANALYZER = NextJSAnalyzer()
_ANGULAR_ANALYZER = AngularAnalyzer()
_PRISMA_ANALYZER = PrismaAnalyzer()
_TYPEORM_ANALYZER = TypeORMAnalyzer()


def framework_analyzer_registry() -> tuple[FrameworkAnalyzer, ...]:
    from .plugins import plugin_registry

    return (
        _FASTAPI_ANALYZER,
        _FLASK_ANALYZER,
        _DJANGO_ANALYZER,
        _EXPRESS_ANALYZER,
        _NESTJS_ANALYZER,
        _SQLALCHEMY_ANALYZER,
        _CELERY_ANALYZER,
        _REACT_ANALYZER,
        _NEXTJS_ANALYZER,
        _ANGULAR_ANALYZER,
        _PRISMA_ANALYZER,
        _TYPEORM_ANALYZER,
        *plugin_registry().framework_analyzers,
    )


def extract_framework_entities(source_file: SourceFile) -> list[FrameworkEntity]:
    return [
        entity
        for analyzer in framework_analyzer_registry()
        for entity in analyzer.extract_entities(source_file)
    ]
