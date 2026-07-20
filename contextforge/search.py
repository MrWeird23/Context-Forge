from collections import defaultdict
from pathlib import Path

from .ranking import classify_file, query_words, score_file, score_file_explained
from .scanner import iter_code_files, read_file


def search_repo(repo: Path, query: str):
    words = query_words(query)

    ranked = []
    evidence = defaultdict(list)

    for path in iter_code_files(repo):
        content = read_file(path)
        score = score_file(path, words, content)

        if score <= 0:
            continue

        relative_path = path.relative_to(repo)
        category = classify_file(relative_path)

        ranked.append((score, relative_path, category))

        for line_number, line in enumerate(content.splitlines(), start=1):
            lowered = line.lower()

            if any(word in lowered for word in words):
                evidence[str(relative_path)].append((line_number, line.strip()))

                if len(evidence[str(relative_path)]) >= 4:
                    break

    ranked.sort(reverse=True, key=lambda item: item[0])

    return ranked, evidence


def structured_search(repo: Path, query: str) -> list[dict]:
    words = query_words(query)
    results = []
    for path in iter_code_files(repo):
        content = read_file(path)
        score, breakdown = score_file_explained(path, words, content)
        if score <= 0:
            continue
        relative = path.relative_to(repo)
        matches = []
        for line_number, line in enumerate(content.splitlines(), start=1):
            if any(word in line.lower() for word in words):
                matches.append({"line": line_number, "text": line.strip()[:180]})
            if len(matches) >= 4:
                break
        results.append({
            "path": str(relative),
            "category": classify_file(relative),
            "score": score,
            "score_breakdown": breakdown,
            "evidence": matches,
        })
    return sorted(results, key=lambda item: (-item["score"], item["path"]))