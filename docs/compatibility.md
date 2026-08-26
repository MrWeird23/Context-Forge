# ContextForge 1.x compatibility contract

ContextForge 1.0 establishes the compatibility boundary for machine-readable output, plugins, supported Python runtimes, index upgrades, and installed release artifacts. The machine-readable summary is [`compatibility-1.0.json`](compatibility-1.0.json).

## Versioning policy

ContextForge follows semantic versioning for its documented public contracts.

- **Patch releases** may fix defects and add diagnostics without removing or changing documented fields or behavior.
- **Minor releases** may add commands, fields, enum values, plugin capabilities, and optional dataclass fields with defaults. Consumers must ignore unknown object fields and tolerate new diagnostic codes.
- **Major releases** may remove or reinterpret documented fields, change required plugin methods or dataclass fields, or adopt an incompatible schema or plugin protocol version. Such changes receive a migration guide.

Private names beginning with `_`, terminal presentation, undocumented Python objects, diagnostic wording, analyzer fingerprint values, cache locations, and SQLite implementation details are not stable APIs.

## Machine-readable JSON

Every command supporting `--format json` emits the `1.0` envelope documented by [`json-schema-1.0.json`](json-schema-1.0.json). The following top-level fields remain required throughout ContextForge 1.x:

- `schema_version`
- `command`
- `repository`

The `error` object retains `code`, `message`, and `details`. Standardized claims retain `claim`, `status`, `confidence`, `supporting_evidence`, `conflicting_evidence`, and `unresolved_uncertainty`.

The schema deliberately permits additive fields. Consumers should select fields they understand rather than reject unknown fields. ContextForge will not remove fields, change their documented types, or change their meaning within the `1.x` line. New optional fields and enum values may be added in minor releases. A breaking JSON change requires a new schema version and a major ContextForge release.

## Plugin protocol

The public `1.0` plugin surface comprises:

- `contextforge.plugins.Plugin`
- `contextforge.plugins.AnalyzerCapability`
- `contextforge.plugins.FrameworkCapability`
- `contextforge.analyzers.Analyzer` and its public input and result dataclasses
- `contextforge.frameworks.FrameworkAnalyzer` and `FrameworkEntity`
- the `contextforge.plugins` entry-point group

ContextForge 1.x accepts protocol `1.0`. Existing required fields, method signatures, default values, and semantics will not change incompatibly during the 1.x line. Minor releases may add optional dataclass fields with defaults or new optional capabilities. Plugins should use keyword arguments when constructing public dataclasses and must ignore diagnostic codes they do not recognize.

The plugin package version is independent of the protocol version. An incompatible future protocol will use a different protocol version and will be rejected cleanly by a host that does not support it.

## Upgrade and migration policy

Upgrading within 1.x requires no manual migration for documented JSON consumers or protocol-1.0 plugins.

ContextForge indexes are disposable derived data, not a public storage API. On an incompatible index schema or analyzer fingerprint, ContextForge validates the existing cache and atomically rebuilds it from repository snapshots. Downgrades may likewise rebuild the index. Consumers must not read or modify the SQLite cache directly.

Before a future major upgrade:

1. Review the changelog and migration guide.
2. Validate representative JSON output against the new published schema.
3. Run plugin compatibility tests against the new host.
4. Allow ContextForge to rebuild derived indexes; do not copy internal SQLite state between incompatible versions.

## MCP tool protocol

ContextForge 1.1 adds a read-only stdio MCP tool protocol at version `1.0`. Tool results use JSON schema version `1.0`. Compatible releases may add optional tools, parameters, or response fields but will not remove or reinterpret the seven documented 1.0 tools. The transport and protocol version are recorded in `compatibility-1.0.json`.

## Security and resource boundaries

ContextForge treats repository contents, paths, Git metadata, and analyzer input as untrusted data. Source snapshots use bounded, descriptor-relative access on supported POSIX systems; unsafe path traversal or symlink conditions fail closed. Index caches are stored outside repositories, keyed by canonical repository identity, validated before use, and replaced atomically when incompatible.

Indexing enforces configurable per-file and repository byte limits. Static analysis remains conservative: unsupported or failed analysis produces omissions and diagnostics, not fabricated high-confidence evidence.

Plugins are the deliberate exception to the data boundary. Installed plugins execute as trusted in-process Python code with the user's permissions. They are not sandboxed. Review plugin source, publisher, dependencies, and provenance before installation. Set `CONTEXTFORGE_DISABLE_PLUGINS=1` when processing sensitive repositories with no third-party extensions required.

## Supported runtimes and release verification

ContextForge 1.0 supports CPython 3.10, 3.11, 3.12, and 3.13. CI runs the complete warning-strict test suite on every supported version.

A release is ready only when:

- all JSON command fixtures validate against the published schema;
- plugin protocol and compatibility contract tests pass;
- the complete test matrix passes with warnings treated as errors;
- wheel and source distributions build successfully and pass metadata checks;
- published compatibility and schema files are present in both artifacts;
- an isolated installation from the wheel can run `cf --version`, `cf doctor`, and a representative JSON command;
- the release diff receives an independent review, or the release notes explicitly record why independent review could not be obtained.

Maintainers can reproduce the artifact gate with:

```bash
rm -rf dist build
python -m build
python -m twine check dist/*
python scripts/verify-release.py
```
