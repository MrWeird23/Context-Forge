from collections import defaultdict
from pathlib import Path

from .ranking import classify_file
from .scanner import iter_code_files


def architecture_buckets(repo: Path):
    buckets = defaultdict(list)

    for path in iter_code_files(repo):
        relative_path = path.relative_to(repo)
        category = classify_file(relative_path)
        buckets[category].append(relative_path)

    return buckets