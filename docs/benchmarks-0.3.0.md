# ContextForge 0.3.0 mixed-language indexing benchmark

Measured on 2026-07-24 using an Apple M3 running macOS 26.5.2 and Python 3.11.15.

## Method

Each synthetic repository contained an even rotation of independently parseable Python, JavaScript, and TypeScript files. Python files exercised AST import, function, call, and relationship extraction. JavaScript and TypeScript files exercised Tree-sitter imports, exports, functions or classes, calls, and relationships.

For each repository size, three isolated repositories and external cache roots were created. The table reports median wall-clock time. A cold run built a schema-v2 SQLite index; the immediately following warm run securely re-read and rehashed each unchanged file while reusing the completed index. Repository generation was excluded from timing.

Reproduce the default run from the repository root with:

```bash
python benchmarks/index_mixed_languages.py
```

Pass explicit file counts and `--repetitions N` to adjust the workload.

| Repository | Files | Cold | Cold throughput | Warm | Warm throughput | Symbols | References | Relationships |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Small | 100 | 0.0362 s | 2,764 files/s | 0.0080 s | 12,504 files/s | 133 | 134 | 266 |
| Medium | 1,000 | 0.3023 s | 3,308 files/s | 0.0659 s | 15,165 files/s | 1,333 | 1,334 | 2,666 |
| Substantial | 5,000 | 3.2049 s | 1,560 files/s | 0.3200 s | 15,627 files/s | 6,666 | 6,667 | 13,333 |

All cold runs indexed every file. All warm runs reported every file unchanged.

## Interpretation

Warm throughput remains close to the 0.2.1 baseline because unchanged files are securely snapshotted and hashed but not reparsed. Cold throughput is lower because 0.3.0 performs native JavaScript and TypeScript syntax analysis and persists architectural relationships in addition to symbols and references. The 5,000-file result also shows that parser and SQLite insertion costs become increasingly material at substantial scale.

## Limitations

These fixtures use small uniform source files on a local temporary filesystem. They do not reproduce production file-size distributions, malformed syntax frequency, monorepo directory topology, storage latency, generated code, or framework-specific syntax. Tree-sitter parsing remains bounded by ContextForge's file and repository byte limits, but those limits are not CPU deadlines. Results are release reference points, not universal performance guarantees.
