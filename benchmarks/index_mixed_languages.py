import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

from contextforge.intelligence import build_index


def source_for(index: int) -> tuple[str, str]:
    language = index % 3
    if language == 0:
        return ".py", (
            f"from storage_{index} import persist_{index}\n"
            f"def handler_{index}(value):\n"
            f"    return persist_{index}(value)\n"
        )
    if language == 1:
        return ".js", (
            f"import {{ persist_{index} }} from './storage_{index}.js';\n"
            f"export function handler_{index}(value) {{ return persist_{index}(value); }}\n"
        )
    return ".ts", (
        f"import {{ persist_{index} }} from './storage_{index}';\n"
        f"export class Service_{index} {{\n"
        f"  handler(value: string): void {{ persist_{index}(value); }}\n"
        f"}}\n"
    )


def measure(count: int, repetitions: int) -> dict:
    cold_times: list[float] = []
    warm_times: list[float] = []
    final_stats: dict | None = None
    for _ in range(repetitions):
        with tempfile.TemporaryDirectory(prefix="contextforge-benchmark-") as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            for index in range(count):
                extension, source = source_for(index)
                (repository / f"module_{index}{extension}").write_text(
                    source,
                    encoding="utf-8",
                )
            os.environ["CONTEXTFORGE_CACHE_DIR"] = str(root / "cache")
            started = time.perf_counter()
            final_stats = build_index(repository)
            cold_times.append(time.perf_counter() - started)
            started = time.perf_counter()
            warm_stats = build_index(repository)
            warm_times.append(time.perf_counter() - started)
            assert warm_stats["unchanged"] == count
    assert final_stats is not None
    cold = statistics.median(cold_times)
    warm = statistics.median(warm_times)
    return {
        "files": count,
        "cold_seconds": cold,
        "cold_files_per_second": count / cold,
        "warm_seconds": warm,
        "warm_files_per_second": count / warm,
        "symbols": final_stats["symbols"],
        "references": final_stats["references"],
        "relationships": final_stats["relationships"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("counts", nargs="*", type=int, default=[100, 1000, 5000])
    parser.add_argument("--repetitions", type=int, default=3)
    arguments = parser.parse_args()
    if arguments.repetitions < 1 or any(count < 1 for count in arguments.counts):
        parser.error("counts and repetitions must be positive integers")
    print(
        json.dumps(
            [measure(count, arguments.repetitions) for count in arguments.counts],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
