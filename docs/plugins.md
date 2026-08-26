# ContextForge plugins

ContextForge 1.x provides an intentionally small, stable public plugin protocol for adding source-language and project or infrastructure analyzers, plus framework entity extractors, without modifying core. An analyzer with no declared extensions can contribute project or infrastructure detection without claiming source files. The complete 1.x compatibility and migration policy is documented in [the compatibility contract](compatibility.md).

## Compatibility

Plugins register one entry point in the `contextforge.plugins` group. The entry point must return a `contextforge.plugins.Plugin` instance, either directly or from a zero-argument factory.

```toml
[project.entry-points."contextforge.plugins"]
my-plugin = "my_contextforge_plugin:plugin"
```

```python
from contextforge.plugins import (
    AnalyzerCapability,
    FrameworkCapability,
    Plugin,
)

plugin = Plugin(
    name="my-plugin",
    version="1.0.0",
    protocol_version="1.0",
    priority=100,
    analyzers=(AnalyzerCapability(MyAnalyzer(), (".example",)),),
    framework_analyzers=(FrameworkCapability(MyFrameworkAnalyzer()),),
)
```

Protocol `1.0` is the stable protocol accepted by ContextForge 1.x. ContextForge rejects incompatible or malformed plugins and reports them through `cf doctor`. Protocol compatibility, rather than the plugin package version, governs whether a plugin can load.

Language analyzers implement the public `Analyzer` protocol from `contextforge.analyzers`; framework analyzers implement `FrameworkAnalyzer` from `contextforge.frameworks`. Their return values use the public dataclasses in those modules.

## Selection and conflicts

Discovery and selection are deterministic:

1. Entry points are discovered from installed distributions.
2. Candidates are ordered by ascending `priority`, plugin name, and entry-point name.
3. Lower numeric priority wins.
4. Core analyzers always retain their built-in extensions.
5. The first accepted plugin wins duplicate plugin names, analyzer names, framework analyzer names, and extension claims.
6. Rejected capabilities produce plugin diagnostics rather than replacing established behavior.

Extension declarations must begin with `.` and cannot contain path separators or NUL bytes. They are normalized to lowercase. Accepted extensions are included automatically in repository scans.

## Failure isolation

ContextForge isolates plugin discovery and analyzer calls. An import, factory, detection, extraction, or entry-point failure omits that plugin's result and records a diagnostic; it does not abort core analysis. Runtime failures are fail-closed and never become evidence-backed findings.

Diagnostics intentionally exclude exception text because third-party exceptions may contain repository content, credentials, or other sensitive values.

Run `cf doctor` to inspect loaded plugins and diagnostics. Set `CONTEXTFORGE_DISABLE_PLUGINS=1` to disable discovery entirely for troubleshooting or hardened environments.

## Security boundary

> [!WARNING]
> ContextForge plugins are trusted in-process Python code, not a sandbox.

Installing or running a plugin grants it the same operating-system permissions as ContextForge. A plugin can read files, access the network, start processes, mutate state, or terminate the process. Failure isolation protects ContextForge's result integrity from ordinary plugin exceptions; it does not contain malicious code, native crashes, excessive resource use, or deliberate process termination.

Review plugin source and publisher provenance before installation. Use a virtual environment or container when evaluating unfamiliar plugins, pin plugin versions, and disable plugins when processing sensitive repositories unless each installed plugin is trusted.

## Compatibility testing

Plugin authors should test at minimum:

- their entry point returns a valid `Plugin` object;
- the declared `protocol_version` is `1.0`;
- extension selection is deterministic and does not conflict with core;
- all analyzer methods return the documented core dataclasses;
- malformed and unsupported source fails closed with empty results;
- the plugin remains importable on every supported Python version.

ContextForge's own compatibility suite covers direct and factory entry points, ordering, duplicate names, extension conflicts, malformed and incompatible plugins, runtime isolation, scanner integration, and complete discovery disablement.
