import argparse
import importlib.metadata
import shutil
import subprocess
import sys
from pathlib import Path

from contextforge import __version__
from .architecture import architecture_buckets
from .detector import detect_project
from .output import console, title, success, warning, error, line
from .scanner import IGNORE_DIRS, repo_root
from .search import search_repo
from .search import structured_search
from .intelligence import (
    build_index,
    dumps,
    envelope,
    find_references,
    find_symbols,
    repository_brief,
    trace_symbol,
)


IMPORTANT_FOLDERS = {
    "src",
    "app",
    "pages",
    "routes",
    "api",
    "services",
    "components",
    "models",
    "schemas",
    "tests",
    "__tests__",
    "pb_migrations",
    "workflows",
    "lib",
    "utils",
    "hooks",
}


HELP_TEXT = """Examples:
  cf map
  cf architecture
  cf search auth
  cf feature auth
  cf index
  cf symbol UserService
  cf refs create_user
  cf trace register
  cf brief --format json
  cf doctor"""


def print_header(text: str):
    console.print(f"\n[bold cyan]{text}[/bold cyan]")


def cmd_map(_args):
    repo = repo_root()

    print_header("ContextForge — Map")
    console.print(f"Repo: {repo}\n")

    console.print("[bold]Detection[/bold]")
    for item in detect_project(repo):
        success(item)

    console.print("\n[bold]Important folders[/bold]")

    folders = []
    for path in repo.rglob("*"):
        relative_parts = path.relative_to(repo).parts

        if any(part in IGNORE_DIRS for part in relative_parts):
            continue

        if path.is_dir() and path.name in IMPORTANT_FOLDERS:
            folders.append(path.relative_to(repo))

    for folder in sorted(folders)[:100]:
        console.print(f"- {folder}")



def cmd_architecture(_args):
    repo = repo_root()
    buckets = architecture_buckets(repo)

    print_header("ContextForge — Architecture")

    console.print("[bold]Detection[/bold]")
    for item in detect_project(repo):
        success(item)

    preferred = [
        "Components",
        "Services",
        "Routes",
        "API",
        "Models / Schemas",
        "PocketBase",
        "Tests",
        "Utils / Lib",
        "Other",
    ]

    for category in preferred:
        items = buckets.get(category, [])

        if not items:
            continue

        console.print(f"\n[bold]{category}[/bold]")

        for item in items[:40]:
            console.print(f"- {item}")



def cmd_search(args):
    repo = repo_root()
    if args.format == "json":
        print(dumps(envelope("search", repo, query=args.query, results=structured_search(repo, args.query))))
        return
    ranked, evidence = search_repo(repo, args.query)

    print_header("ContextForge — Search")
    console.print(f"Query: {args.query}\n")

    console.print("[bold]Top ranked files[/bold]")
    for score, path, category in ranked[:25]:
        console.print(f"{score:>4}  [dim][{category}][/dim] {path}")

    console.print("\n[bold]Evidence[/bold]")

    for _, path, _ in ranked[:10]:
        key = str(path)

        if key not in evidence:
            continue

        console.print(f"\n[cyan]{key}[/cyan]")

        for line_number, code_line in evidence[key]:
            console.print(f"  [dim]L{line_number}:[/dim] {code_line[:180]}")



def cmd_feature(args):
    repo = repo_root()
    ranked, evidence = search_repo(repo, args.query)

    print_header("ContextForge — Feature Report")
    console.print(f"Topic: {args.query}\n")

    console.print("[bold]Project context[/bold]")
    for item in detect_project(repo):
        success(item)

    console.print("\n[bold]Most relevant files[/bold]")
    for score, path, category in ranked[:12]:
        console.print(f"- {path} [dim][{category}] score={score}[/dim]")

    console.print("\n[bold]Suggested reading order[/bold]")

    priority = {
        "Services": 1,
        "Routes": 2,
        "API": 3,
        "Components": 4,
        "Models / Schemas": 5,
        "PocketBase": 6,
        "Tests": 7,
        "Utils / Lib": 8,
        "Other": 9,
    }

    ordered = sorted(ranked[:20], key=lambda item: priority.get(item[2], 99))

    for index, (_, path, category) in enumerate(ordered[:10], start=1):
        console.print(f"{index}. {path} — [dim]{category}[/dim]")

    console.print("\n[bold]Evidence[/bold]")

    for _, path, _ in ranked[:8]:
        key = str(path)

        if key not in evidence:
            continue

        console.print(f"\n[cyan]{key}[/cyan]")

        for line_number, code_line in evidence[key]:
            console.print(f"  [dim]L{line_number}:[/dim] {code_line[:170]}")

    console.print("\n[bold]Claude instruction[/bold]")
    console.print("Read the files above in the suggested order. Do not edit until the flow is clear.")



def cmd_version(_args):
    console.print(f"ContextForge {__version__}")


def cmd_index(args):
    repo = repo_root()
    result = envelope("index", repo, **build_index(repo))
    if args.format == "json":
        print(dumps(result))
        return
    print_header("ContextForge — Index")
    for key in ("indexed", "unchanged", "removed", "total_files", "symbols", "references"):
        console.print(f"{key.replace('_', ' ').title()}: {result[key]}")


def cmd_symbol(args):
    repo = repo_root()
    definitions = find_symbols(repo, args.name)
    result = envelope("symbol", repo, query=args.name, definitions=definitions)
    if args.format == "json":
        print(dumps(result))
        return
    print_header(f"ContextForge — Symbol: {args.name}")
    for item in definitions:
        console.print(f"{item['kind']:>8}  {item['path']}:{item['line']}  {item['name']}")


def cmd_refs(args):
    repo = repo_root()
    references = find_references(repo, args.name)
    result = envelope("refs", repo, query=args.name, references=references)
    if args.format == "json":
        print(dumps(result))
        return
    print_header(f"ContextForge — References: {args.name}")
    for item in references:
        console.print(f"{item['path']}:{item['line']}  {item['context']}")


def cmd_trace(args):
    repo = repo_root()
    nodes, edges = trace_symbol(repo, args.name, args.max_depth)
    result = envelope("trace", repo, query=args.name, nodes=nodes, edges=edges)
    if args.format == "json":
        print(dumps(result))
        return
    print_header(f"ContextForge — Trace: {args.name}")
    for edge in edges:
        console.print(f"{edge['from']} → {edge['to']}  [dim]{edge['path']}[/dim]")


def cmd_brief(args):
    repo = repo_root()
    architecture = {
        category: [str(item) for item in items]
        for category, items in architecture_buckets(repo).items()
    }
    result = envelope("brief", repo, **repository_brief(repo, detect_project(repo), architecture))
    if args.format == "json":
        print(dumps(result))
        return
    print_header("ContextForge — Repository Brief")
    console.print(f"Name: {result.get('name') or repo.name}")
    console.print("Detection: " + ", ".join(result["detections"]))
    console.print(f"Symbols: {result['symbols']['total']}  References: {result['symbols']['references']}")
    if result["entry_points"]:
        console.print("\n[bold]Entry points[/bold]")
        for item in result["entry_points"]:
            console.print(f"- {item['command']}: {item['target']}")



def cmd_doctor(_args):
    repo = repo_root()
    issues = 0

    title("ContextForge Doctor")

    if sys.version_info >= (3, 10):
        success(f"Python {sys.version.split()[0]}")
    else:
        issues += 1
        error(f"Python {sys.version.split()[0]} found; Python 3.10+ is required")

    git_root = _git_root()
    if git_root is not None:
        success(f"Git repository detected: {git_root}")
    else:
        issues += 1
        warning("Not inside a Git repository")

    detections = detect_project(repo)
    if detections == ["Framework: Unknown"]:
        warning("Project detection returned unknown")
    else:
        success("Project detection available")

    for item in detections:
        success(item)

    if shutil.which("rg"):
        success("ripgrep installed")
    else:
        issues += 1
        warning("ripgrep not found")

    completion = _completion_status()
    if completion:
        success(f"Shell completion detected: {completion}")
    else:
        warning("Shell completion not found")

    if _package_installed():
        success("Package installation detected")
    else:
        issues += 1
        warning("Package installation not detected; run: pip install -e .")

    if _entry_point_installed():
        success("cf console script installed")
    else:
        issues += 1
        warning("cf console script not detected")

    line()
    if issues:
        warning(f"Doctor check complete with {issues} issue(s)")
        return 1

    success("Doctor check complete")
    return 0



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cf",
        description="ContextForge - AI-first repository exploration for coding assistants.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_TEXT,
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"ContextForge {__version__}",
    )

    subcommands = parser.add_subparsers(
        title="commands",
        dest="command",
        metavar="<command>",
        required=True,
    )

    subcommands.add_parser(
        "map",
        help="Detect framework and repository layout",
    ).set_defaults(func=cmd_map)
    subcommands.add_parser(
        "architecture",
        help="Show categorized project architecture",
    ).set_defaults(func=cmd_architecture)
    subcommands.add_parser(
        "doctor",
        help="Check local setup",
    ).set_defaults(func=cmd_doctor)
    subcommands.add_parser(
        "version",
        help="Show ContextForge version",
    ).set_defaults(func=cmd_version)

    def add_format(parser):
        parser.add_argument("--format", choices=("text", "json"), default="text")
        return parser

    search = subcommands.add_parser("search", help="Search and rank repository files")
    search.add_argument("query", help="Search query")
    add_format(search)
    search.set_defaults(func=cmd_search)

    feature = subcommands.add_parser("feature", help="Generate an AI-friendly feature report")
    feature.add_argument("query", help="Feature or topic to investigate")
    feature.set_defaults(func=cmd_feature)

    index = add_format(subcommands.add_parser("index", help="Build or refresh the incremental repository index"))
    index.set_defaults(func=cmd_index)

    symbol = add_format(subcommands.add_parser("symbol", help="Find symbol definitions"))
    symbol.add_argument("name", help="Exact symbol name")
    symbol.set_defaults(func=cmd_symbol)

    refs = add_format(subcommands.add_parser("refs", help="Find references to a symbol"))
    refs.add_argument("name", help="Exact symbol name")
    refs.set_defaults(func=cmd_refs)

    trace = add_format(subcommands.add_parser("trace", help="Trace calls from a symbol"))
    trace.add_argument("name", help="Entry symbol name")
    trace.add_argument("--max-depth", type=int, default=6)
    trace.set_defaults(func=cmd_trace)

    brief = add_format(subcommands.add_parser("brief", help="Generate an evidence-backed repository briefing"))
    brief.set_defaults(func=cmd_brief)

    return parser



def main():
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args) or 0


def _git_root() -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    return Path(result.stdout.strip()).resolve()


def _completion_status() -> str | None:
    candidates = [
        Path.home() / ".zsh" / "completions" / "_cf",
        Path.home() / ".local" / "share" / "bash-completion" / "completions" / "cf",
        Path.home() / ".config" / "fish" / "completions" / "cf.fish",
    ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return None


def _package_installed() -> bool:
    try:
        installed_version = importlib.metadata.version("contextforge")
    except importlib.metadata.PackageNotFoundError:
        return False

    return installed_version == __version__


def _entry_point_installed() -> bool:
    entry_points = importlib.metadata.entry_points()

    for entry_point in entry_points.select(group="console_scripts", name="cf"):
        if entry_point.value == "contextforge.cli:main":
            return True

    return False
