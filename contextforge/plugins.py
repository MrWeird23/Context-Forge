from __future__ import annotations

import importlib.metadata
from importlib.metadata import EntryPoint
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping, TypeVar, cast

from .analyzers import Analyzer, DetectionResult
from .compatibility import PLUGIN_ENTRY_POINT_GROUP, PLUGIN_PROTOCOL_VERSION
from .frameworks import FrameworkAnalyzer

_DISABLED_VALUES = {"1", "true", "yes", "on"}
_CORE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
}
_CORE_ANALYZER_NAMES = {"python", "javascript", "typescript", "generic"}
_CORE_FRAMEWORK_ANALYZER_NAMES = {
    "fastapi",
    "flask",
    "django",
    "express",
    "nestjs",
    "sqlalchemy",
    "celery",
    "react",
    "nextjs",
    "angular",
    "prisma",
    "typeorm",
}


@dataclass(frozen=True)
class AnalyzerCapability:
    analyzer: Analyzer
    extensions: tuple[str, ...]


@dataclass(frozen=True)
class FrameworkCapability:
    analyzer: FrameworkAnalyzer


@dataclass(frozen=True)
class Plugin:
    name: str
    version: str
    protocol_version: str
    analyzers: tuple[AnalyzerCapability, ...] = ()
    framework_analyzers: tuple[FrameworkCapability, ...] = ()
    priority: int = 100


@dataclass(frozen=True)
class PluginDiagnostic:
    code: str
    message: str
    entry_point: str | None = None
    plugin: str | None = None


T = TypeVar("T")


class _IsolatedAnalyzer:
    def __init__(self, analyzer: Analyzer, registry: PluginRegistry, plugin: str):
        self._analyzer = analyzer
        self._registry = registry
        self._plugin = plugin
        self.name = analyzer.name

    def detect(self, repository):
        return self._call(
            "detect",
            DetectionResult(self.name, False, "low", ()),
            repository,
        )

    def extract_symbols(self, source_file):
        return self._call("extract_symbols", [], source_file)

    def extract_references(self, source_file):
        return self._call("extract_references", [], source_file)

    def extract_relationships(self, source_file):
        return self._call("extract_relationships", [], source_file)

    def discover_entry_points(self, repository):
        return self._call("discover_entry_points", [], repository)

    def _call(self, method: str, fallback: T, *arguments) -> T:
        try:
            return getattr(self._analyzer, method)(*arguments)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._registry.add_diagnostic(
                PluginDiagnostic(
                    "runtime-failed",
                    f"Plugin analyzer {self.name!r} failed during {method}; results were omitted",
                    plugin=self._plugin,
                )
            )
            return fallback


class _IsolatedFrameworkAnalyzer:
    def __init__(self, analyzer: FrameworkAnalyzer, registry: PluginRegistry, plugin: str):
        self._analyzer = analyzer
        self._registry = registry
        self._plugin = plugin
        self.name = analyzer.name

    def extract_entities(self, source_file):
        try:
            return self._analyzer.extract_entities(source_file)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._registry.add_diagnostic(
                PluginDiagnostic(
                    "runtime-failed",
                    f"Plugin framework analyzer {self.name!r} failed; results were omitted",
                    plugin=self._plugin,
                )
            )
            return []


@dataclass
class PluginRegistry:
    plugins: tuple[Plugin, ...] = ()
    analyzer_extensions: dict[str, Analyzer] = field(default_factory=dict)
    analyzers: tuple[Analyzer, ...] = ()
    framework_analyzers: tuple[FrameworkAnalyzer, ...] = ()
    _diagnostics: list[PluginDiagnostic] = field(default_factory=list)

    @property
    def diagnostics(self) -> tuple[PluginDiagnostic, ...]:
        return tuple(self._diagnostics)

    def add_diagnostic(self, diagnostic: PluginDiagnostic) -> None:
        if diagnostic not in self._diagnostics:
            self._diagnostics.append(diagnostic)

    def ordered_diagnostics(self) -> tuple[PluginDiagnostic, ...]:
        return tuple(
            sorted(
                self._diagnostics,
                key=lambda diagnostic: (
                    diagnostic.plugin or "",
                    diagnostic.entry_point or "",
                    diagnostic.code,
                    diagnostic.message,
                ),
            )
        )

    def analyzer_for_path(self, path: Path) -> Analyzer | None:
        return self.analyzer_extensions.get(path.suffix.lower())


def _valid_analyzer(analyzer: object) -> bool:
    required = (
        "name",
        "detect",
        "extract_symbols",
        "extract_references",
        "extract_relationships",
        "discover_entry_points",
    )
    return (
        isinstance(getattr(analyzer, "name", None), str)
        and bool(analyzer.name)
        and all(callable(getattr(analyzer, method, None)) for method in required[1:])
    )


def _valid_framework_analyzer(analyzer: object) -> bool:
    return (
        isinstance(getattr(analyzer, "name", None), str)
        and bool(analyzer.name)
        and callable(getattr(analyzer, "extract_entities", None))
    )


def _normalize_extension(extension: object) -> str | None:
    if not isinstance(extension, str) or not extension.startswith("."):
        return None
    normalized = extension.lower()
    if len(normalized) < 2 or any(character in normalized for character in "/\\\x00"):
        return None
    return normalized


def _validate_plugin(candidate: object) -> str | None:
    if not isinstance(candidate, Plugin):
        return "Entry point did not provide a contextforge.plugins.Plugin instance"
    if not isinstance(candidate.name, str) or not isinstance(candidate.version, str):
        return "Plugin name and version must be strings"
    if not candidate.name or not candidate.version:
        return "Plugin name and version must be non-empty"
    if (
        not isinstance(candidate.priority, int)
        or isinstance(candidate.priority, bool)
        or candidate.priority < 0
    ):
        return "Plugin priority must be a non-negative integer"
    if not isinstance(candidate.analyzers, tuple):
        return "Plugin analyzers must be a tuple"
    if not isinstance(candidate.framework_analyzers, tuple):
        return "Plugin framework analyzers must be a tuple"
    if any(
        not isinstance(capability, AnalyzerCapability)
        or not _valid_analyzer(capability.analyzer)
        or not isinstance(capability.extensions, tuple)
        for capability in candidate.analyzers
    ):
        return "Plugin contains an invalid analyzer capability"
    if any(
        not isinstance(capability, FrameworkCapability)
        or not _valid_framework_analyzer(capability.analyzer)
        for capability in candidate.framework_analyzers
    ):
        return "Plugin contains an invalid framework capability"
    return None


def _disabled() -> bool:
    return os.environ.get("CONTEXTFORGE_DISABLE_PLUGINS", "").strip().lower() in _DISABLED_VALUES


@lru_cache(maxsize=1)
def plugin_registry() -> PluginRegistry:
    registry = PluginRegistry()
    if _disabled():
        return registry

    discovered: list[tuple[int, str, str, Plugin]] = []
    try:
        available = importlib.metadata.entry_points()
        if hasattr(available, "select"):
            entry_points = available.select(group=PLUGIN_ENTRY_POINT_GROUP)
        elif hasattr(available, "get"):
            legacy = cast(Mapping[str, tuple[EntryPoint, ...]], available)
            entry_points = legacy.get(PLUGIN_ENTRY_POINT_GROUP, ())
        else:
            entry_points = (
                entry_point
                for entry_point in available
                if getattr(entry_point, "group", None) == PLUGIN_ENTRY_POINT_GROUP
            )
    except Exception:
        registry.add_diagnostic(
            PluginDiagnostic(
                "discovery-failed",
                "Plugin entry-point discovery failed",
            )
        )
        return registry
    for entry_point in sorted(entry_points, key=lambda item: (item.name, item.value)):
        try:
            candidate = entry_point.load()
            if callable(candidate) and not isinstance(candidate, Plugin):
                candidate = candidate()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            registry.add_diagnostic(
                PluginDiagnostic(
                    "load-failed",
                    "Plugin entry point could not be loaded",
                    entry_point=entry_point.name,
                )
            )
            continue

        if isinstance(candidate, Plugin) and candidate.protocol_version != PLUGIN_PROTOCOL_VERSION:
            registry.add_diagnostic(
                PluginDiagnostic(
                    "incompatible-protocol",
                    f"Plugin requires protocol {candidate.protocol_version}; ContextForge supports {PLUGIN_PROTOCOL_VERSION}",
                    entry_point=entry_point.name,
                    plugin=candidate.name,
                )
            )
            continue

        validation_error = _validate_plugin(candidate)
        if validation_error is not None:
            registry.add_diagnostic(
                PluginDiagnostic(
                    "invalid-plugin",
                    validation_error,
                    entry_point=entry_point.name,
                    plugin=getattr(candidate, "name", None),
                )
            )
            continue

        assert isinstance(candidate, Plugin)
        discovered.append((candidate.priority, candidate.name, entry_point.name, candidate))

    accepted: list[Plugin] = []
    names: set[str] = set()
    analyzer_names: set[str] = set()
    framework_names: set[str] = set()
    extension_map: dict[str, Analyzer] = {}
    analyzers: list[Analyzer] = []
    frameworks: list[FrameworkAnalyzer] = []

    for _, _, entry_point_name, plugin in sorted(discovered):
        if plugin.name in names:
            registry.add_diagnostic(
                PluginDiagnostic(
                    "duplicate-plugin",
                    f"Duplicate plugin name {plugin.name!r}; lower-priority entry point was ignored",
                    entry_point=entry_point_name,
                    plugin=plugin.name,
                )
            )
            continue
        names.add(plugin.name)
        accepted.append(plugin)

        for capability in plugin.analyzers:
            if (
                capability.analyzer.name in _CORE_ANALYZER_NAMES
                or capability.analyzer.name in analyzer_names
            ):
                registry.add_diagnostic(
                    PluginDiagnostic(
                        "analyzer-conflict",
                        f"Duplicate analyzer name {capability.analyzer.name!r}; capability was ignored",
                        entry_point=entry_point_name,
                        plugin=plugin.name,
                    )
                )
                continue
            analyzer_names.add(capability.analyzer.name)
            isolated = _IsolatedAnalyzer(capability.analyzer, registry, plugin.name)
            for raw_extension in capability.extensions:
                extension = _normalize_extension(raw_extension)
                if extension is None:
                    registry.add_diagnostic(
                        PluginDiagnostic(
                            "invalid-extension",
                            "Analyzer extension must begin with a dot and contain no path separators",
                            entry_point=entry_point_name,
                            plugin=plugin.name,
                        )
                    )
                    continue
                if extension in _CORE_EXTENSIONS or extension in extension_map:
                    registry.add_diagnostic(
                        PluginDiagnostic(
                            "extension-conflict",
                            f"Extension {extension!r} is already claimed; plugin capability was not selected for it",
                            entry_point=entry_point_name,
                            plugin=plugin.name,
                        )
                    )
                    continue
                extension_map[extension] = isolated
            analyzers.append(isolated)

        for capability in plugin.framework_analyzers:
            if (
                capability.analyzer.name in _CORE_FRAMEWORK_ANALYZER_NAMES
                or capability.analyzer.name in framework_names
            ):
                registry.add_diagnostic(
                    PluginDiagnostic(
                        "framework-conflict",
                        f"Duplicate framework analyzer name {capability.analyzer.name!r}; capability was ignored",
                        entry_point=entry_point_name,
                        plugin=plugin.name,
                    )
                )
                continue
            framework_names.add(capability.analyzer.name)
            frameworks.append(
                _IsolatedFrameworkAnalyzer(capability.analyzer, registry, plugin.name)
            )

    registry.plugins = tuple(accepted)
    registry.analyzer_extensions = extension_map
    registry.analyzers = tuple(analyzers)
    registry.framework_analyzers = tuple(frameworks)
    registry._diagnostics = list(registry.ordered_diagnostics())
    return registry


def reset_plugin_cache() -> None:
    plugin_registry.cache_clear()
