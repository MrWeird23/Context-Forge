from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


REQUIRED_SOURCE_FILES = {
    "contextforge/compatibility.py",
    "docs/compatibility-1.0.json",
    "docs/compatibility.md",
    "docs/json-schema-1.0.json",
    "docs/plugins.md",
}
REQUIRED_WHEEL_FILES = {
    "contextforge/compatibility.py",
    "share/contextforge/compatibility-1.0.json",
    "share/contextforge/compatibility.md",
    "share/contextforge/json-schema-1.0.json",
    "share/contextforge/plugins.md",
}


def run(*arguments: str, cwd: Path | None = None) -> None:
    subprocess.run(arguments, cwd=cwd, check=True)


def wheel_entries(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def sdist_entries(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        root = archive.getmembers()[0].name.split("/", 1)[0]
        prefix = f"{root}/"
        return {
            member.name.removeprefix(prefix)
            for member in archive.getmembers()
            if member.isfile()
        }


def assert_required(
    entries: set[str], artifact: Path, required_files: set[str]
) -> None:
    missing = sorted(
        required
        for required in required_files
        if not any(entry.endswith(required) for entry in entries)
    )
    if missing:
        raise SystemExit(f"{artifact.name} is missing: {', '.join(missing)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify ContextForge release artifacts")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()

    wheels = sorted(args.dist.glob("contextforge_cli-*.whl"))
    sdists = sorted(args.dist.glob("contextforge_cli-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit("Expected exactly one wheel and one source distribution")

    wheel = wheels[0]
    sdist = sdists[0]
    assert_required(wheel_entries(wheel), wheel, REQUIRED_WHEEL_FILES)
    assert_required(sdist_entries(sdist), sdist, REQUIRED_SOURCE_FILES)

    with tempfile.TemporaryDirectory(prefix="contextforge-release-") as directory:
        environment = Path(directory)
        run(sys.executable, "-m", "venv", str(environment))
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        cf = environment / ("Scripts/cf.exe" if sys.platform == "win32" else "bin/cf")
        run(str(python), "-m", "pip", "install", "--quiet", str(wheel.resolve()))
        version = subprocess.check_output([str(cf), "--version"], text=True).strip()
        if version != "ContextForge 1.0.0":
            raise SystemExit(f"Unexpected installed version: {version}")
        repository = environment / "repository"
        repository.mkdir()
        (repository / "example.py").write_text("def example():\n    return 1\n")
        run("git", "init", "--quiet", str(repository))
        doctor = subprocess.run(
            [str(cf), "doctor"],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if doctor.returncode not in {0, 1} or "Doctor check complete" not in doctor.stdout:
            raise SystemExit("Installed CLI doctor command did not execute correctly")
        index_payload = json.loads(
            subprocess.check_output(
                [str(cf), "index", "--format", "json"],
                cwd=repository,
                text=True,
            )
        )
        if not {
            "schema_version",
            "command",
            "repository",
        }.issubset(index_payload):
            raise SystemExit("Installed CLI emitted an invalid JSON envelope")
        manifest = json.loads(
            subprocess.check_output(
                [
                    str(python),
                    "-c",
                    "import json, pathlib, sys; "
                    "p=pathlib.Path(sys.prefix)/'share/contextforge/compatibility-1.0.json'; "
                    "print(p.read_text())",
                ],
                text=True,
            )
        )
        if manifest["compatibility_version"] != "1.0":
            raise SystemExit("Installed compatibility manifest is invalid")

    print(f"Verified {wheel.name} and {sdist.name}")


if __name__ == "__main__":
    main()
