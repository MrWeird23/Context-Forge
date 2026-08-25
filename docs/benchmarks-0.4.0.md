# ContextForge 0.4.0 mixed-language indexing benchmark

Measured on 2026-07-29 using an Apple M3 running macOS 26.5.2 and Python 3.11.15.

## Method

Each synthetic repository contained an even rotation of independently parseable Python, JavaScript, and TypeScript files. Python files exercised AST import, function, call, and relationship extraction. JavaScript and TypeScript files exercised Tree-sitter imports, exports, functions or classes, calls, and relationships.

For each repository size, three isolated repositories and external cache roots were created. The table reports median wall-clock time. A cold run built a schema-generation 3 SQLite index; the immediately following warm run securely re-read and rehashed each unchanged file while reusing the completed index. Repository generation was excluded from timing.

Reproduce the default run from the repository root with:

```bash
python benchmarks/index_mixed_languages.py
```

Pass explicit file counts and `--repetitions N` to adjust the workload.

| Repository | Files | Cold | Cold throughput | Warm | Warm throughput | Symbols | References | Relationships |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Small | 100 | 0.0397 s | 2,520 files/s | 0.0076 s | 13,102 files/s | 133 | 134 | 266 |
| Medium | 1,000 | 0.3889 s | 2,571 files/s | 0.0615 s | 16,271 files/s | 1,333 | 1,334 | 2,666 |
| Substantial | 5,000 | 3.8361 s | 1,303 files/s | 0.3448 s | 14,500 files/s | 6,666 | 6,667 | 13,333 |

All cold runs indexed every file. All warm runs reported every file unchanged.

## Interpretation

Warm throughput remains high because unchanged files are securely snapshotted and hashed but not reparsed. Cold runs include native Python, JavaScript, and TypeScript syntax analysis and schema-generation 3 persistence. The synthetic fixtures do not contain framework registrations, so this benchmark measures the release indexing baseline rather than per-framework extraction cost.

## Limitations

These fixtures use small uniform source files on a local temporary filesystem. They do not reproduce production file-size distributions, malformed syntax frequency, monorepo directory topology, storage latency, generated code, or framework-specific syntax. Tree-sitter parsing and framework extraction remain bounded by ContextForge's file and repository byte limits, but those limits are not CPU deadlines. Results are release reference points, not universal performance guarantees.
