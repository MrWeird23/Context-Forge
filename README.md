# ContextForge

ContextForge is an AI-first repository exploration CLI. It combines fast lexical search with an incremental symbol/reference index so humans and coding assistants can establish architectural context before editing code.

## Install

```bash
pipx install .
# Development installation
python -m pip install -e '.[dev]'
```

Python 3.10 or newer is required.

## Quick start

Run ContextForge from anywhere inside a repository:

```bash
cf doctor
cf map
cf architecture
cf index
cf brief
cf search authentication
cf symbol UserService
cf refs create_user
cf trace register
```

ContextForge resolves the Git root automatically. Outside Git, it treats the current directory as the repository root.

## Repository intelligence

### Incremental index

```bash
cf index
cf index --format json
```

The index is stored at `.contextforge/index.sqlite`. Files are keyed by SHA-256, so unchanged source files are not reparsed. Deleted files are removed from the index. Generated, dependency, cache, and virtual-environment directories are excluded. Source symlinks are never followed, and symlinked cache/database paths are rejected to keep reads and writes confined to the repository.

Python symbols and call references are extracted using the standard-library AST. Other supported source extensions receive conservative class/function/call extraction. This provides useful cross-language navigation without requiring a parser toolchain; framework-aware and Tree-sitter analyzers remain suitable future extensions.

### Symbols and references

```bash
cf symbol create_user
cf refs create_user
cf symbol create_user --format json
```

`symbol` returns exact definitions with kind, file, source range, parent scope, and signature. `refs` returns import and call sites with line evidence and caller scope where available.

### Execution tracing

```bash
cf trace register
cf trace register --max-depth 10 --format json
```

`trace` follows statically resolvable calls between indexed symbols. The result is evidence, not a claim that every dynamic runtime path has been discovered. Reflection, decorators, dependency-injection containers, callbacks, and dynamic dispatch may require framework-specific analyzers.

### Evidence-backed briefing

```bash
cf brief
cf brief --format json
```

The briefing includes project detection, package entry points, architecture buckets, symbol/reference totals, evidence, and a confidence label.

## Explainable search

```bash
cf search "session expiry" --format json
```

JSON search results include a `score_breakdown` showing filename matches, path matches, content occurrences, and architectural folder bonuses, along with matching source lines.

## Machine-readable output

Intelligence commands support `--format json` and emit schema version `1.0`:

- `cf search <query> --format json`
- `cf index --format json`
- `cf symbol <name> --format json`
- `cf refs <name> --format json`
- `cf trace <name> --format json`
- `cf brief --format json`

Each payload contains `schema_version`, `command`, and `repository`, followed by command-specific data.

## Existing exploration commands

- `cf map` — detect the project and list important folders.
- `cf architecture` — group supported files by architectural role.
- `cf search <query>` — rank files and show source evidence.
- `cf feature <topic>` — generate a focused reading order.
- `cf doctor` — validate Git context, Python, ripgrep, completion, and installation.
- `cf version` — display the installed version.

## Development

```bash
python -m pytest -q
```

The acceptance tests exercise JSON search, incremental indexing, symbol/reference navigation, static tracing, and repository briefing against temporary repositories.
