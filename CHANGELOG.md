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
