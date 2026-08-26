# ContextForge Roadmap

ContextForge is evolving from repository search into evidence-backed repository intelligence for humans and coding assistants.

> ContextForge should not merely find files. It should reconstruct how a repository works, show the evidence behind that reconstruction, and state clearly where certainty ends.

This roadmap describes the intended direction of the project. Priorities may change as implementation findings, security reviews, and user feedback reveal a better sequence.

## Status

| Release | Focus | Status |
|---|---|---|
| **0.2.1** | Index hardening, schema migration, and structured errors | Complete |
| **0.3.0** | Analyzer architecture and JavaScript/TypeScript Tree-sitter support | Complete |
| **0.4.0** | Framework-aware entities and improved tracing | Complete |
| **0.5.0** | Task-oriented investigation reports | Complete |
| **0.6.0** | Git history, coupling, hotspots, and ownership | Complete |
| **0.7.0** | Markdown, Mermaid, and Graphviz exports | Complete |
| **0.8.0** | Evidence and confidence standards | Complete |
| **0.9.0** | Public plugin API and compatibility testing | Complete |
| **1.0.0** | Stable schemas, plugin protocol, and compatibility guarantees | Complete |

## Completed foundations

### 0.2.1 — Harden the indexing foundation

ContextForge established a secure and coherent indexing base:

- Read source files through bounded, race-resistant snapshots.
- Derive indexed content and its digest from the same bytes.
- Reject source and cache redirection through unsafe filesystem paths.
- Store caches outside untrusted repositories by default.
- Key caches by canonical repository identity.
- Validate SQLite schema versions and definitions.
- Rebuild incompatible indexes through atomic replacement.
- Return structured failures for JSON output.
- Enforce configurable per-file and per-repository resource limits.
- Exercise source and cache boundaries with adversarial tests.

### 0.3.0 — Introduce the analyzer architecture

ContextForge moved language-specific analysis behind a stable analyzer protocol:

- Extract Python entities with the standard-library AST.
- Parse JavaScript, JSX, TypeScript, TSX, MTS, and CTS with Tree-sitter.
- Fall back conservatively when native syntax analysis is unavailable or malformed.
- Index symbols, references, imports, exports, calls, and inheritance.
- Invalidate incremental indexes when analyzer semantics change.
- Discover Python and Node entry points without executing repository code.
- Trace statically resolvable call relationships.
- Produce evidence-backed repository briefings.
- Preserve the public JSON `1.0` contract.
- Benchmark mixed-language indexing.

## Current release

### 0.4.0 — Framework-aware analysis and stronger tracing

ContextForge 0.4.0 moves from language awareness to conservative framework awareness. Framework analyzers identify architectural entities from syntax and proven registrations rather than relying primarily on filenames and directories.

#### Initial framework targets

**Python**

- FastAPI
- Flask
- Django
- SQLAlchemy
- Celery

**JavaScript and TypeScript**

- Express
- NestJS
- React
- Next.js
- Angular
- Prisma
- TypeORM

#### Initial entity coverage

- Routes and request handlers
- Controllers
- Middleware and authorization guards
- Services
- Components and hooks
- Models and schemas
- Repositories
- Background jobs

#### Tracing improvements delivered

- Follow route-to-handler relationships.
- Detect middleware and authorization boundaries.

Commands include:

```bash
cf routes
cf services
cf models
cf jobs
cf trace "POST /users"
```

Dynamic cross-file service/repository resolution, event-consumer tracing, migration intelligence, and generalized framework plugins remain later work. The 0.4.0 analyzers deliberately omit ambiguous or runtime-generated relationships.

### 0.5.0 — Task-oriented investigation

Delivered task-oriented reports for common development work:

```bash
cf investigate "How does password reset work?"
cf impact "Rename User.email to User.primaryEmail"
cf change "Add rate limiting to password reset"
cf debug "Orders remain pending after payment succeeds"
```

Reports combine:

- Relevant execution paths
- Definitions and references
- Data models and configuration
- Existing implementation patterns
- Tests to add or update
- Risks and invariants
- Ranked hypotheses where appropriate
- Observed facts, inferences, confidence, and unresolved questions

The reports remain bounded by indexed static evidence. Dynamic runtime wiring and behavior outside the repository are retained as explicit uncertainty rather than inferred as fact.

### 0.6.0 — Git intelligence

Use repository history to reveal relationships that are not explicit in the current source tree:

```bash
cf hotspots
cf coupling
cf owners src/payments
cf history authentication
```

Completed capabilities:

- Identify frequently changed files.
- Find files commonly changed together.
- Estimate module ownership.
- Detect historical renames.
- Find concentrations of bug-fix activity.
- Distinguish stable and volatile modules.

The release deliberately reports historical path and contributor evidence rather than asserting current maintainership or runtime ownership. Broader symbol-to-commit and architectural-change synthesis remains future work.

### 0.7.0 — Architecture exports

Produce artifacts suitable for documentation, CI, pull requests, and other coding assistants:

- Human-readable terminal output
- JSON
- Markdown
- Mermaid
- Graphviz DOT

Implemented commands include:

```bash
cf trace register --format mermaid
cf trace register --format graphviz
cf brief --format markdown
```

Markdown exports are available for repository briefs, task-oriented reports, and call traces. Mermaid and Graphviz DOT are intentionally scoped to `trace`, whose indexed call relationships have explicit graph semantics. Architecture-map graph inference remains deferred until its relationships can be represented without inventing unsupported edges.

## Current release

### 0.8.0 — Evidence and confidence standards

Standardize every generated claim around:

- Claim
- Status: observed or inferred
- Confidence
- Supporting evidence
- Conflicting evidence
- Unresolved uncertainty

Confidence should remain explainable:

- **High** — directly observed through definitions or registrations.
- **Medium** — strongly inferred from connected static evidence.
- **Low** — plausible but dependent on dynamic behavior.
- **Unknown** — insufficient evidence.

The claim contract is now emitted by repository briefs and task-oriented reports in JSON and Markdown. Existing facts and inferences remain available for compatibility, while every standardized claim carries explicit support, conflicts, and unresolved uncertainty. Claims without supporting evidence cannot assert confidence above `unknown`.

## Completed release: 0.9.0

### Public plugin system

Allow contributors to add language, framework, and infrastructure support without modifying the core.

Design requirements:

- Stable, versioned plugin protocol
- Capability discovery
- Deterministic analyzer ordering
- Conflict resolution
- Failure isolation
- Plugin-specific diagnostics
- Compatibility testing
- Explicit security boundaries for third-party plugins

ContextForge now discovers versioned `1.0` plugins through Python entry points,
selects analyzers and framework extractors with deterministic priority and
conflict rules, includes accepted language extensions in repository scans, and
reports discovery or runtime failures without allowing unsupported findings to
enter the evidence model. Third-party plugins remain explicitly trusted,
in-process code rather than a sandboxed extension mechanism.

See [the plugin guide](docs/plugins.md) for authoring, diagnostics,
compatibility testing, disablement, and security guidance.

## Completed release: 1.0.0

### Stable compatibility contract

The first stable release provides:

- Stable machine-readable schemas
- Stable plugin API and protocol
- Documented compatibility guarantees
- Documented upgrade and migration policy
- Security and resource-boundary documentation
- Release artifact and installation verification

ContextForge 1.0 publishes a stable `1.0` JSON envelope and plugin protocol,
an explicit additive-change and migration policy, machine-readable compatibility
metadata, documented security and resource boundaries, and release gates across
all supported Python versions and built artifacts. See
[the compatibility contract](docs/compatibility.md).

## Definition of done

A roadmap item is not complete until the relevant release satisfies its applicable quality gates:

- New behavior is covered by tests, preferably developed failing-first.
- The complete test suite passes.
- Machine-readable output validates against its documented schema.
- Existing CLI behavior remains compatible unless a breaking change is intentional and documented.
- Index upgrades are safe and deterministic.
- Security boundaries have adversarial coverage.
- Performance is measured when the change can affect indexing or query cost.
- Documentation includes examples and limitations.
- Independent review finds no unresolved blocking security or correctness defect.

## Contributing

Roadmap discussion and implementation proposals are welcome through GitHub issues. Substantial features should define their evidence model, compatibility impact, resource limits, and verification strategy before implementation begins.
