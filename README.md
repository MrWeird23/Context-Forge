# ContextForge

ContextForge is an AI-first repository exploration CLI. It combines fast lexical search with an incremental symbol, reference, and relationship index so humans and coding assistants can establish architectural context before editing code.

See the [project roadmap](ROADMAP.md) for completed foundations, the next release, and the path toward ContextForge 1.0.

## Install

```bash
pipx install .
# Development installation
python -m pip install -e '.[dev]'
```

Python 3.10 or newer is required. Secure source analysis requires POSIX descriptor-relative file access with `O_DIRECTORY` and `O_NOFOLLOW`. ContextForge fails closed with a structured `unsafe_source_access` error when those primitives are unavailable rather than silently following untrusted paths.

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
cf investigate "How does password reset work?"
cf impact "Rename User.email to User.primaryEmail"
cf change "Add rate limiting to password reset"
cf debug "Orders remain pending after payment succeeds"
cf routes
cf services
cf models
cf jobs
cf boundaries
```

ContextForge resolves the Git root automatically. Outside Git, it treats the current directory as the repository root.

## Task-oriented investigation

ContextForge can assemble bounded, evidence-backed reports around common development tasks:

```bash
cf investigate "How does password reset work?"
cf impact "Rename User.email to User.primaryEmail"
cf change "Add rate limiting to password reset"
cf debug "Orders remain pending after payment succeeds"
```

Each command accepts `--format json`. Reports separate observed facts from inferences, attach repository-relative path and line evidence, state overall and per-item confidence, retain unresolved questions, and identify relevant definitions, references, static execution paths, models, configuration, tests, and risks. `impact` adds affected areas, `change` adds evidence-backed implementation patterns, and `debug` adds ranked hypotheses.

The reports are deliberately conservative. They describe only relationships present in the static index, make uncertainty explicit, and do not claim to observe runtime wiring, reflection, generated code, or behavior outside the indexed repository.

## Repository intelligence

### Incremental index

```bash
cf index
cf index --format json
```

Indexes are stored outside the repository and keyed by a SHA-256 identity derived from the canonical repository path. The default cache roots are `~/Library/Caches/contextforge` on macOS and `$XDG_CACHE_HOME/contextforge` (or `~/.cache/contextforge`) on other supported POSIX systems. Set `CONTEXTFORGE_CACHE_DIR` to choose another root.

Each source file is opened without following symlinks and read into one bounded byte snapshot. ContextForge derives the SHA-256 digest and every analyzer result from those same bytes, then verifies file identity, size, modification time, and change time around the read. Unchanged files are not reparsed and deleted files are removed. SQLite compatibility checks compare the complete stored schema definitions with a trusted schema generated from the current `INDEX_SCHEMA`, then verify `user_version`, analyzer fingerprint, integrity, columns, hidden-column state, and index semantics. This rejects stale analyzer results, unexpected objects, constraints, defaults, collations, and storage options. Incompatible indexes are rebuilt through atomic replacement after active WAL or rollback-journal writers are excluded and stale sidecars are cleared. Database device and inode identity are verified across open and commit boundaries. Generated, dependency, cache, and virtual-environment directories remain excluded.

The indexer defaults to 10 MiB per file and 1 GiB of supported source content per repository. Limits can be adjusted for a single explicit index operation:

```bash
cf index --max-file-size 20971520 --max-repository-size 2147483648
```

They can also be configured for explicit and implicit indexing with `CONTEXTFORGE_MAX_FILE_SIZE` and `CONTEXTFORGE_MAX_REPOSITORY_SIZE`. Values are bytes and must be positive integers.

The filesystem protections defend against repository-controlled paths, symlinked cache ancestors, cache redirection, and replacement during source reads. They do not attempt to isolate ContextForge from another process already executing as the same operating-system user; such a process has equivalent access to the user's cache directory.

Python analysis uses the standard-library AST. JavaScript, JSX, ESM/CJS variants, TypeScript, TSX, MTS, and CTS use bundled Tree-sitter grammars. These analyzers extract symbols, references, imports, exports, calls, and inheritance from native syntax trees. Files in other supported languages receive conservative lexical class/function/call extraction. A malformed Tree-sitter syntax tree falls back to lexical symbols and references and does not emit structural relationships.

Analyzer project detection and entry-point discovery operate on bounded manifest snapshots supplied by the core. ContextForge does not import `setup.py`, execute package scripts, or load repository code during discovery.

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

`trace` follows statically resolvable indexed call relationships between symbols. The result is evidence, not a claim that every dynamic runtime path has been discovered. Reflection, decorators, dependency-injection containers, callbacks, and dynamic dispatch may require framework-specific analyzers.

### Framework-aware entities

```bash
cf routes
cf services
cf models
cf jobs
cf boundaries
cf routes --format json
cf trace "POST /users"
```

ContextForge derives framework entities from source syntax and registration evidence without importing or executing repository code. The initial analyzers cover:

- Routes: FastAPI, Flask, Django, Express, NestJS, Angular, and Next.js App Router handlers.
- Models and repositories: SQLAlchemy, Prisma, and TypeORM.
- Services and frontend entities: SQLAlchemy repositories, React components and hooks, Next.js server actions, and Angular components and services.
- Jobs: Celery application tasks and `shared_task` decorators.
- Boundaries: FastAPI and Flask middleware, FastAPI route dependencies, Express middleware, and NestJS guards and interceptors.

Evidence must be static and unambiguous. Supported patterns include exact or explicitly aliased framework imports, literal route and table metadata, proven application or client construction, and direct decorators or registrations. Dynamic imports, computed metadata, unsupported control flow, ambiguous ownership, rebinding, mutation before registration, and runtime-generated framework objects are deliberately omitted. These omissions are conservative false negatives rather than speculative high-confidence results.

### Evidence-backed briefing

```bash
cf brief
cf brief --format json
```

The briefing includes project detection, analyzer evidence, safely discovered package entry points, architecture buckets, symbol/reference/relationship totals, evidence, and a confidence label. Existing JSON 1.0 detection and entry-point fields remain compatible.

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
- `cf routes --format json`
- `cf services --format json`
- `cf models --format json`
- `cf jobs --format json`
- `cf boundaries --format json`

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

The acceptance tests exercise JSON schema validation, incremental indexing, bounded single-snapshot reads, source and cache replacement attacks, schema rebuilding, structured failures, Python and Tree-sitter analyzers, relationship persistence, analyzer fingerprint invalidation, symbol/reference navigation, static tracing, and repository briefing against temporary repositories.

Release benchmark methodology and mixed-language results are documented in [`docs/benchmarks-0.4.0.md`](docs/benchmarks-0.4.0.md).

## License

ContextForge is available under the [MIT License](LICENSE).
