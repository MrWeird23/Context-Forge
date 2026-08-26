from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from contextforge.analyzers import DetectionResult, EntryPoint, Repository, SourceFile


class DemoAnalyzer:
    name = "demo"

    def detect(self, repository):
        return DetectionResult(self.name, True, "high", ("demo.lock",))

    def extract_symbols(self, source_file):
        return []

    def extract_references(self, source_file):
        return []

    def extract_relationships(self, source_file):
        return []

    def discover_entry_points(self, repository):
        return [EntryPoint("demo", "demo:main", "demo.lock")]


class DemoFrameworkAnalyzer:
    name = "demo-framework"

    def extract_entities(self, source_file):
        return []


@dataclass
class FakeEntryPoint:
    name: str
    value: str
    plugin: object = None
    failure: Exception | None = None
    group: str = "contextforge.plugins"

    def load(self):
        if self.failure is not None:
            raise self.failure
        return self.plugin


class FakeEntryPoints(list):
    def select(self, **criteria):
        if criteria.get("group") == "contextforge.plugins":
            return FakeEntryPoints(self)
        return FakeEntryPoints()


@pytest.fixture(autouse=True)
def reset_plugins(monkeypatch):
    import contextforge.plugins as plugins

    monkeypatch.delenv("CONTEXTFORGE_DISABLE_PLUGINS", raising=False)
    monkeypatch.setattr(
        plugins.importlib.metadata,
        "entry_points",
        lambda: FakeEntryPoints(),
    )
    plugins.reset_plugin_cache()
    yield
    plugins.reset_plugin_cache()


def install_plugins(monkeypatch, *entry_points):
    import contextforge.plugins as plugins

    monkeypatch.setattr(
        plugins.importlib.metadata,
        "entry_points",
        lambda: FakeEntryPoints(entry_points),
    )
    plugins.reset_plugin_cache()


def make_plugin(name="demo", **overrides):
    from contextforge.plugins import AnalyzerCapability, FrameworkCapability, Plugin

    values = {
        "name": name,
        "version": "1.2.3",
        "protocol_version": "1.0",
        "analyzers": (AnalyzerCapability(DemoAnalyzer(), (".demo",)),),
        "framework_analyzers": (FrameworkCapability(DemoFrameworkAnalyzer()),),
    }
    values.update(overrides)
    return Plugin(**values)


def test_discovers_versioned_plugin_capabilities(monkeypatch):
    from contextforge.plugins import plugin_registry

    install_plugins(
        monkeypatch,
        FakeEntryPoint("demo-dist", "demo_plugin:plugin", make_plugin()),
    )

    registry = plugin_registry()

    assert [(item.name, item.version, item.protocol_version) for item in registry.plugins] == [
        ("demo", "1.2.3", "1.0")
    ]
    assert registry.analyzer_for_path(Path("example.demo")).name == "demo"
    assert [item.name for item in registry.framework_analyzers] == ["demo-framework"]
    assert registry.diagnostics == ()


def test_discovery_order_is_deterministic_and_duplicate_names_are_rejected(monkeypatch):
    from contextforge.plugins import plugin_registry

    install_plugins(
        monkeypatch,
        FakeEntryPoint("z-dist", "z:plugin", make_plugin("shared", priority=20)),
        FakeEntryPoint("a-dist", "a:plugin", make_plugin("shared", priority=10)),
        FakeEntryPoint("b-dist", "b:plugin", make_plugin("bravo", priority=10)),
    )

    registry = plugin_registry()

    assert [item.name for item in registry.plugins] == ["bravo", "shared"]
    assert registry.analyzer_for_path(Path("example.demo")).name == "demo"
    assert any(
        item.code == "duplicate-plugin" and item.entry_point == "z-dist"
        for item in registry.diagnostics
    )


def test_core_extension_wins_and_conflict_is_diagnosed(monkeypatch):
    from contextforge.analyzers import analyzer_for_path
    from contextforge.plugins import AnalyzerCapability, plugin_registry

    plugin = make_plugin(
        analyzers=(AnalyzerCapability(DemoAnalyzer(), (".py", ".demo")),),
    )
    install_plugins(monkeypatch, FakeEntryPoint("demo-dist", "demo:plugin", plugin))

    assert analyzer_for_path(Path("module.py")).name == "python"
    assert analyzer_for_path(Path("module.demo")).name == "demo"
    assert any(
        item.code == "extension-conflict" and ".py" in item.message
        for item in plugin_registry().diagnostics
    )


def test_core_analyzer_and_framework_names_cannot_be_replaced(monkeypatch):
    from contextforge.plugins import AnalyzerCapability, FrameworkCapability, plugin_registry

    analyzer = DemoAnalyzer()
    analyzer.name = "python"
    framework = DemoFrameworkAnalyzer()
    framework.name = "fastapi"
    plugin = make_plugin(
        analyzers=(AnalyzerCapability(analyzer, (".demo",)),),
        framework_analyzers=(FrameworkCapability(framework),),
    )
    install_plugins(monkeypatch, FakeEntryPoint("demo-dist", "demo:plugin", plugin))

    registry = plugin_registry()

    assert registry.analyzer_for_path(Path("module.demo")) is None
    assert registry.framework_analyzers == ()
    assert {item.code for item in registry.diagnostics} == {
        "analyzer-conflict",
        "framework-conflict",
    }


def test_analyzer_without_extensions_can_contribute_project_detection(monkeypatch):
    from contextforge.analyzers import analyzer_registry
    from contextforge.plugins import AnalyzerCapability

    plugin = make_plugin(
        analyzers=(AnalyzerCapability(DemoAnalyzer(), ()),),
        framework_analyzers=(),
    )
    install_plugins(monkeypatch, FakeEntryPoint("demo-dist", "demo:plugin", plugin))

    assert "demo" in {analyzer.name for analyzer in analyzer_registry()}


def test_incompatible_and_malformed_plugins_are_isolated(monkeypatch):
    from contextforge.plugins import AnalyzerCapability, plugin_registry

    install_plugins(
        monkeypatch,
        FakeEntryPoint("broken-import", "broken:plugin", failure=RuntimeError("secret token")),
        FakeEntryPoint(
            "future",
            "future:plugin",
            make_plugin("future", protocol_version="2.0"),
        ),
        FakeEntryPoint("malformed", "malformed:plugin", SimpleNamespace(name="bad")),
        FakeEntryPoint(
            "malformed-capabilities",
            "malformed_capabilities:plugin",
            make_plugin("bad-capabilities", analyzers=None),
        ),
        FakeEntryPoint(
            "malformed-extensions",
            "malformed_extensions:plugin",
            make_plugin(
                "bad-extensions",
                analyzers=(AnalyzerCapability(DemoAnalyzer(), None),),  # type: ignore[arg-type]
            ),
        ),
    )

    registry = plugin_registry()

    assert registry.plugins == ()
    assert {item.code for item in registry.diagnostics} == {
        "load-failed",
        "incompatible-protocol",
        "invalid-plugin",
    }
    assert all("secret token" not in item.message for item in registry.diagnostics)


def test_entry_point_discovery_failure_is_isolated(monkeypatch):
    import contextforge.plugins as plugins

    def fail_discovery():
        raise RuntimeError("sensitive metadata")

    monkeypatch.setattr(plugins.importlib.metadata, "entry_points", fail_discovery)
    plugins.reset_plugin_cache()

    registry = plugins.plugin_registry()

    assert registry.plugins == ()
    assert [(item.code, item.message) for item in registry.diagnostics] == [
        ("discovery-failed", "Plugin entry-point discovery failed")
    ]


def test_legacy_entry_point_collections_are_supported(monkeypatch):
    import contextforge.plugins as plugins

    entry_point = FakeEntryPoint("demo-dist", "demo:plugin", make_plugin())
    monkeypatch.setattr(
        plugins.importlib.metadata,
        "entry_points",
        lambda: {"contextforge.plugins": [entry_point]},
    )
    plugins.reset_plugin_cache()

    assert [plugin.name for plugin in plugins.plugin_registry().plugins] == ["demo"]


def test_base_exception_isolated_but_process_control_propagates(monkeypatch):
    from contextforge.plugins import AnalyzerCapability, plugin_registry

    analyzer = DemoAnalyzer()

    def memory_failure(_source_file):
        raise MemoryError("plugin exhausted memory")

    analyzer.extract_symbols = memory_failure
    plugin = make_plugin(analyzers=(AnalyzerCapability(analyzer, (".demo",)),))
    install_plugins(monkeypatch, FakeEntryPoint("demo-dist", "demo:plugin", plugin))

    registry = plugin_registry()
    selected = registry.analyzer_for_path(Path("module.demo"))

    assert selected is not None
    assert selected.extract_symbols(SourceFile(Path("module.demo"), "demo")) == []
    assert any(item.code == "runtime-failed" for item in registry.diagnostics)

    analyzer.extract_symbols = lambda _source_file: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        selected.extract_symbols(SourceFile(Path("module.demo"), "demo"))


def test_diagnostics_can_be_reported_deterministically(monkeypatch):
    from contextforge.plugins import AnalyzerCapability, plugin_registry

    first = DemoAnalyzer()
    first.name = "zeta"
    second = DemoAnalyzer()
    second.name = "alpha"
    first.extract_symbols = lambda _source_file: (_ for _ in ()).throw(ValueError())
    second.extract_symbols = lambda _source_file: (_ for _ in ()).throw(ValueError())
    install_plugins(
        monkeypatch,
        FakeEntryPoint(
            "zeta-dist",
            "zeta:plugin",
            make_plugin("zeta", analyzers=(AnalyzerCapability(first, (".zeta",)),)),
        ),
        FakeEntryPoint(
            "alpha-dist",
            "alpha:plugin",
            make_plugin("alpha", analyzers=(AnalyzerCapability(second, (".alpha",)),)),
        ),
    )

    registry = plugin_registry()
    registry.analyzer_for_path(Path("x.zeta")).extract_symbols(SourceFile(Path("x.zeta"), ""))
    registry.analyzer_for_path(Path("x.alpha")).extract_symbols(SourceFile(Path("x.alpha"), ""))

    assert [
        item.plugin
        for item in registry.ordered_diagnostics()
        if item.code == "runtime-failed"
    ] == ["alpha", "zeta"]


def test_plugin_analyzer_failures_return_fail_closed_results(monkeypatch):
    from contextforge.plugins import AnalyzerCapability, plugin_registry

    class BrokenAnalyzer(DemoAnalyzer):
        name = "broken"

        def detect(self, repository):
            raise RuntimeError("do not disclose")

        def extract_symbols(self, source_file):
            raise RuntimeError("do not disclose")

    plugin = make_plugin(
        analyzers=(AnalyzerCapability(BrokenAnalyzer(), (".broken",)),),
        framework_analyzers=(),
    )
    install_plugins(monkeypatch, FakeEntryPoint("broken", "broken:plugin", plugin))

    registry = plugin_registry()
    analyzer = registry.analyzer_for_path(Path("module.broken"))

    assert analyzer.extract_symbols(SourceFile("module.broken", "secret")) == []
    assert analyzer.detect(Repository(Path("."), {})) == DetectionResult(
        "broken", False, "low", ()
    )
    assert any(item.code == "runtime-failed" for item in registry.diagnostics)
    assert all("do not disclose" not in item.message for item in registry.diagnostics)


def test_plugins_can_be_disabled_without_loading_entry_points(monkeypatch):
    import contextforge.plugins as plugins

    loaded = False

    def entry_points():
        nonlocal loaded
        loaded = True
        return FakeEntryPoints()

    monkeypatch.setenv("CONTEXTFORGE_DISABLE_PLUGINS", "1")
    monkeypatch.setattr(plugins.importlib.metadata, "entry_points", entry_points)
    plugins.reset_plugin_cache()

    registry = plugins.plugin_registry()

    assert loaded is False
    assert registry.plugins == ()
    assert registry.diagnostics == ()


def test_plugin_entry_point_may_be_a_zero_argument_factory(monkeypatch):
    from contextforge.plugins import plugin_registry

    install_plugins(
        monkeypatch,
        FakeEntryPoint("factory", "factory:create_plugin", lambda: make_plugin("factory")),
    )

    assert [item.name for item in plugin_registry().plugins] == ["factory"]


def test_plugin_extensions_are_included_in_repository_scans(monkeypatch, tmp_path):
    from contextforge.scanner import iter_code_files

    install_plugins(
        monkeypatch,
        FakeEntryPoint("demo-dist", "demo:plugin", make_plugin()),
    )
    (tmp_path / "module.demo").write_text("demo", encoding="utf-8")
    (tmp_path / "ignored.unknown").write_text("ignored", encoding="utf-8")

    assert [path.name for path in iter_code_files(tmp_path)] == ["module.demo"]
