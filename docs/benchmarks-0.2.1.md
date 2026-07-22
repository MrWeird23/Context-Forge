# ContextForge 0.2.1 indexing benchmark

Measured on 2026-07-20 using an Apple M3 running macOS 26.5.2 and Python 3.11.15.

## Method

Each synthetic repository contained independently parseable Python files with one function per file. A cold run built a new external SQLite index; a warm run re-read and rehashed the same files while reusing the completed index. Cache and repositories were created on the local temporary filesystem. Times are single-run wall-clock measurements and should be treated as release reference points, not universal performance guarantees.

| Repository | Files | Cold | Cold throughput | Warm | Warm throughput |
|---|---:|---:|---:|---:|---:|
| Small | 100 | 0.0122 s | 8,212 files/s | 0.0055 s | 18,044 files/s |
| Medium | 1,000 | 0.1064 s | 9,395 files/s | 0.0548 s | 18,247 files/s |
| Substantial | 5,000 | 0.7961 s | 6,281 files/s | 0.3359 s | 14,887 files/s |

All cold runs indexed every file. All warm runs reported every file as unchanged.

## Limitations

These fixtures emphasize filesystem access, hashing, AST parsing, and SQLite writes. They do not reproduce the file-size distribution, parser complexity, storage latency, or directory topology of a large production monorepo. Future releases should retain this baseline and add corpus-based benchmarks.
