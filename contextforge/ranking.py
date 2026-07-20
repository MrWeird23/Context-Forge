import re
from pathlib import Path


def query_words(query: str) -> list[str]:
    return [word.lower() for word in re.findall(r"[a-zA-Z0-9_]+", query)]


def classify_file(path: Path) -> str:
    name = path.name.lower()
    parts = [part.lower() for part in path.parts]

    if ".component." in name:
        return "Components"
    if ".service." in name or "services" in parts:
        return "Services"
    if "route" in name or "routing" in name or "routes" in parts:
        return "Routes"
    if "model" in name or "schema" in name or "interface" in name or "models" in parts or "schemas" in parts:
        return "Models / Schemas"
    if ".spec." in name or ".test." in name or "tests" in parts or "__tests__" in parts:
        return "Tests"
    if "pb_migrations" in parts:
        return "PocketBase"
    if "api" in parts:
        return "API"
    if "utils" in parts or "lib" in parts:
        return "Utils / Lib"

    return "Other"


def score_file(path: Path, words: list[str], content: str) -> int:
    score, _ = score_file_explained(path, words, content)
    return score


def score_file_explained(path: Path, words: list[str], content: str) -> tuple[int, list[dict]]:
    score = 0
    breakdown = []
    path_text = str(path).lower()
    content_text = content.lower()
    filename = path.name.lower()
    parts = [part.lower() for part in path.parts]

    for word in words:
        if word in filename:
            score += 40
            breakdown.append({"reason": "filename_match", "word": word, "points": 40})
        if word in path_text:
            score += 25
            breakdown.append({"reason": "path_match", "word": word, "points": 25})

        occurrences = content_text.count(word)
        if occurrences:
            points = occurrences * 3
            score += points
            breakdown.append({"reason": "content_matches", "word": word, "occurrences": occurrences, "points": points})

    folder_bonus = {
        "services": 14,
        "api": 14,
        "routes": 14,
        "components": 10,
        "models": 8,
        "schemas": 8,
        "pb_migrations": 10,
        "tests": 5,
        "__tests__": 5,
    }

    for folder, bonus in folder_bonus.items():
        if folder in parts:
            score += bonus
            breakdown.append({"reason": "folder_bonus", "folder": folder, "points": bonus})

    return score, breakdown