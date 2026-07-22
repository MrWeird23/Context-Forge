# ContextForge

ContextForge is an AI-first repository exploration CLI. It combines fast lexical search with an incremental symbol/reference index so humans and coding assistants can establish architectural context before editing code.

## Install

```bash
pipx install .
# Development installation
python -m pip install -e '.[dev]'
```

Python 3.10 or newer is required. Secure source analysis in 0.2.1 requires POSIX descriptor-relative file access with `O_DIRECTORY` and `O_NOFOLLOW`. ContextForge fails closed with a structured `unsafe_source_access` error when those primitives are unavailable rather than silently following untrusted paths.

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

Indexes are stored outside the repository and keyed by a SHA-256 identity derived from the canonical repository path. The default cache roots are `~/Library/Caches/contextforge` on macOS and `$XDG_CACHE_HOME/contextforge` (or `~/.cache/contextforge`) on other supported POSIX systems. Set `CONTEXTFORGE_CACHE_DIR` to choose another root.

Each source file is opened without following symlinks and read into one bounded byte snapshot. ContextForge derives both the SHA-256 digest and parsed content from that snapshot, then verifies file identity, size, modification time, and change time around the read. Unchanged files are not reparsed and deleted files are removed. SQLite compatibility checks compare the complete stored schema definitions with a trusted schema generated from the current `INDEX_SCHEMA`, then verify `user_version`, integrity, columns, hidden-column state, and index semantics. This rejects unexpected objects, constraints, defaults, collations, and storage options. Incompatible indexes are rebuilt through atomic replacement after active WAL or rollback-journal writers are excluded and stale sidecars are cleared. Database device and inode identity are verified across open and commit boundaries. Generated, dependency, cache, and virtual-environment directories remain excluded.

The indexer defaults to 10 MiB per file and 1 GiB of supported source content per repository. Limits can be adjusted for a single explicit index operation:

```bash
cf index --max-file-size 20971520 --max-repository-size 2147483648
```

They can also be configured for explicit and implicit indexing with `CONTEXTFORGE_MAX_FILE_SIZE` and `CONTEXTFORGE_MAX_REPOSITORY_SIZE`. Values are bytes and must be positive integers.

The filesystem protections defend against repository-controlled paths, symlinked cache ancestors, cache redirection, and replacement during source reads. They do not attempt to isolate ContextForge from another process already executing as the same operating-system user; such a process has equivalent access to the user's cache directory.

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

Each payload contains `schema_version`, `command`, and `repository`, followed by command-specific data. The versioned contract is documented at [`docs/json-schema-1.0.json`](docs/json-schema-1.0.json).

When JSON output is requested, command, configuration, resource-limit, cache-safety, and argument failures use the same envelope and return a structured `error` object:

```json
{
  "schema_version": "1.0",
  "command": "index",
  "repository": "/path/to/repository",
  "error": {
    "code": "file_size_limit_exceeded",
    "message": "Source file exceeds the configured size limit: generated.py",
    "details": {
      "path": "generated.py",
      "limit": 10485760,
      "observed": 12000000
    }
  }
}
```

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

The acceptance tests exercise JSON schema validation, incremental indexing, bounded single-snapshot reads, source and cache replacement attacks, schema rebuilding, structured failures, symbol/reference navigation, static tracing, and repository briefing against temporary repositories.
