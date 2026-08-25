import json
from pathlib import Path

from .intelligence import (
    ContextForgeError,
    DEFAULT_MAX_FILE_SIZE,
    SourceAccessError,
    read_source_snapshot,
)


def detect_project(repo: Path) -> list[str]:
    found: list[str] = []

    package_json = repo / "package.json"

    if package_json.exists():
        try:
            snapshot = read_source_snapshot(repo, Path("package.json"), DEFAULT_MAX_FILE_SIZE)
            data = json.loads(snapshot.decode(errors="ignore"))
            if not isinstance(data, dict):
                raise SourceAccessError("package.json must contain a JSON object")

            runtime_dependencies = data.get("dependencies", {})
            development_dependencies = data.get("devDependencies", {})
            if not isinstance(runtime_dependencies, dict) or not isinstance(
                development_dependencies, dict
            ):
                raise SourceAccessError(
                    "package.json dependencies must be JSON objects"
                )
            deps = {**runtime_dependencies, **development_dependencies}

            scripts = data.get("scripts", {})

            if "@angular/core" in deps:
                found.append("Framework: Angular")
            elif "next" in deps:
                found.append("Framework: Next.js")
            elif "react" in deps:
                found.append("Framework: React")
            elif "vue" in deps:
                found.append("Framework: Vue")

            if "typescript" in deps:
                found.append("Language: TypeScript")
            if "vite" in deps:
                found.append("Build: Vite")
            if "vitest" in deps:
                found.append("Tests: Vitest")
            if "jest" in deps:
                found.append("Tests: Jest")

            if (repo / "pnpm-lock.yaml").exists():
                found.append("Package manager: pnpm")
            elif (repo / "yarn.lock").exists():
                found.append("Package manager: yarn")
            elif (repo / "package-lock.json").exists():
                found.append("Package manager: npm")

            if scripts:
                found.append("Scripts: " + ", ".join(scripts.keys()))

        except ContextForgeError:
            raise
        except Exception:
            pass

    if (repo / "angular.json").exists():
        found.append("Angular config: Yes")

    if (repo / "docker-compose.yml").exists() or (repo / "Dockerfile").exists():
        found.append("Docker: Yes")

    if (repo / "pb_migrations").exists():
        found.append("Backend: PocketBase")

    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists():
        found.append("Python: Yes")

    return found or ["Framework: Unknown"]