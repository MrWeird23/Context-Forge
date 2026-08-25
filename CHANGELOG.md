## 0.5.1

### Packaging

- Publish the CLI under the `contextforge-cli` distribution name to avoid the existing, unrelated `contextforge` project on PyPI.
- Keep the Python import package as `contextforge` and the command-line entry point as `cf`.

## 0.5.0

### Task-oriented investigation

- Add `cf investigate`, `cf impact`, `cf change`, and `cf debug` reports for common repository investigation workflows.
- Combine indexed definitions, references, static execution paths, framework entities, models, configuration, relevant tests, risks, and unresolved questions.
- Separate observed facts from evidence-backed inferences and preserve explicit confidence in both text and JSON output.
- Add mode-specific affected areas, implementation patterns, and ranked debugging hypotheses without claiming runtime certainty.

## 0.4.0

### Framework-aware intelligence

- Add conservative syntax-derived analyzers for FastAPI, Flask, Django, Express, NestJS, SQLAlchemy, Celery, React, Next.js, Angular, Prisma, and TypeORM.
- Index routes, controllers, components, hooks, models, repositories, services, server actions, jobs, middleware, guards, interceptors, and route dependencies when ownership and provenance are statically proven.
- Add `.prisma` schema scanning and literal Prisma model, table mapping, client, and delegate extraction without invoking Prisma tooling.
- Fail closed on ambiguous imports, aliases, rebinding, unsupported control flow, dynamic metadata, mutation timing, and unresolved ownership.

### Persistence, tracing, and CLI

- Upgrade the internal SQLite index to schema generation 3 for persistent framework entities and exact route-to-handler relationships.
- Add stable `cf routes`, `cf services`, `cf models`, `cf jobs`, and `cf boundaries` commands with text and JSON output under the existing public schema version `1.0`.
- Trace exact framework routes to uniquely resolved handlers while rejecting stale, duplicate, wrong-kind, or ambiguous relationship evidence.
- Invalidate digest-equal indexes when framework analyzer semantics change through versioned analyzer fingerprints.

### Testing and performance

- Add adversarial framework coverage for import provenance, aliases, rebinding, control flow, mutation timing, malformed syntax, duplicate ownership, and false-positive prevention.
- Record schema-generation 3 mixed-language indexing measurements in `docs/benchmarks-0.4.0.md`.

## 0.3.0

### Analyzer architecture

- Add a stable analyzer protocol with shared source, repository, symbol, reference, relationship, detection, and entry-point models.
- Move Python AST extraction out of the central indexer into a dedicated Python analyzer.
- Add a conservative lexical fallback for unsupported languages and malformed native syntax trees.

### JavaScript and TypeScript

- Add Tree-sitter analyzers for JavaScript, JSX, ESM/CJS variants, TypeScript, TSX, MTS, and CTS.
- Extract classes, interfaces, functions, arrow functions, methods, calls, and constructor calls from native syntax trees.
- Preserve exact bounded source bytes through digesting and Tree-sitter parsing.

### Relationships and detection

- Index imports, exports, calls, and inheritance with source-line evidence and confidence.
- Upgrade the canonical SQLite index to schema version 2 with relationship indexes and atomic rebuilding.
- Add an analyzer fingerprint so semantic changes invalidate digest-equal incremental indexes safely.
- Discover Python and Node entry points from bounded manifest snapshots without importing or executing repository code.
- Add analyzer evidence and relationship totals to repository briefings while preserving JSON schema 1.0 payload compatibility.
- Drive static tracing from indexed call relationships.

### Testing and performance

- Add unit and end-to-end coverage for analyzer selection, native extraction, malformed-tree fallback, exact-byte parsing, relationship persistence, fingerprint invalidation, safe manifest analysis, and JSON compatibility.
- Record mixed Python, JavaScript, and TypeScript indexing measurements in `docs/benchmarks-0.3.0.md`.

## 0.2.1

### Security

- Read indexed files through bounded, no-symlink byte snapshots and derive digests and parsed content from the same bytes.
- Detect source mutation through device, inode, size, modification-time, and change-time checks; reject source paths replaced by symlinks.
- Move indexes outside untrusted repositories and key them by canonical repository identity.
- Reject symlinked external cache ancestors, identities, sidecars, and database paths.
- Fail closed when descriptor-relative no-follow source access is unavailable.

### Index integrity

- Validate SQLite indexes by comparing complete stored schema definitions with a trusted generated schema, in addition to `PRAGMA user_version`, `quick_check`, hidden-column rejection, exact columns, and complete primary/secondary index semantics.
- Rebuild missing, corrupt, or incompatible schemas through atomic replacement after quiescing WAL and rollback-journal state; refuse replacement while another writer is active.
- Bind cache database opens and commits to a verified device and inode identity.
- Add configurable per-file and aggregate repository indexing limits.

### CLI

- Return versioned structured failures whenever `--format json` is requested, including malformed command arguments.
- Publish and validate the JSON output contract at `docs/json-schema-1.0.json`.

### Testing

- Add adversarial tests for source replacement, mutation during reads, cache redirection, interrupted schema rebuilding, and resource exhaustion boundaries.
- Record cold and warm indexing measurements for 100-, 1,000-, and 5,000-file repositories in `docs/benchmarks-0.2.1.md`.
