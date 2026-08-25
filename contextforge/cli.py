import argparse
import importlib.metadata
import shutil
import subprocess
import sys
from pathlib import Path

from rich.markup import escape

from contextforge import __version__
from .architecture import architecture_buckets
from .detector import detect_project
from .output import console, title, success, warning, error, line
from .scanner import IGNORE_DIRS, repo_root
from .search import search_repo
from .search import structured_search
from .git_intelligence import coupling, history, hotspots, owners
from .intelligence import (
    build_index,
    ContextForgeError,
    dumps,
    envelope,
    find_framework_entities,
    find_references,
    find_symbols,
    repository_brief,
    task_report,
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
  cf investigate "How does password reset work?"
  cf impact "Rename User.email to User.primaryEmail"
  cf change "Add rate limiting to password reset"
  cf debug "Orders remain pending after payment succeeds"
  cf hotspots
  cf coupling
  cf owners contextforge/cli.py
  cf history "fix parser"
  cf boundaries
  cf brief --format json
  cf doctor"""


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str):
        arguments = sys.argv[1:]
        json_requested = "--format=json" in arguments or any(
            argument == "--format" and index + 1 < len(arguments) and arguments[index + 1] == "json"
            for index, argument in enumerate(arguments)
        )
        if json_requested:
            command = arguments[0] if arguments and not arguments[0].startswith("-") else "unknown"
            failure = {
                "code": "invalid_arguments",
                "message": message,
                "details": {},
            }
            print(dumps(envelope(command, repo_root(), error=failure)))
            raise SystemExit(2)
        super().error(message)


def print_header(text: str):
    console.print(f"\n[bold cyan]{escape(text)}[/bold cyan]")


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


def _print_git_result(command: str, repo: Path, args, **payload):
    result = envelope(command, repo, **payload)
    if args.format == "json":
        print(dumps(result))
        return
    print_header(f"ContextForge — {command.title()}")
    if command == "hotspots":
        for item in payload["hotspots"]:
            console.print(f"{item['commits']:>5} commits  {item['authors']:>3} authors  {item['path']}")
    elif command == "coupling":
        for item in payload["couplings"]:
            console.print(f"{item['strength']:.0%}  {item['commits']:>4} commits  {' ↔ '.join(item['paths'])}")
    elif command == "owners":
        console.print(f"Path: {payload['ownership']['path']}")
        for item in payload["ownership"]["contributors"]:
            console.print(f"{item['share']:.0%}  {item['commits']:>4} commits  {item['name']}")
    else:
        for item in payload["history"]["commits"]:
            marker = "fix" if item["is_bug_fix"] else "   "
            console.print(f"{marker}  {item['hash'][:8]}  {item['authored_at']}  {item['subject']}")


def cmd_hotspots(args):
    repo = repo_root()
    _print_git_result("hotspots", repo, args, hotspots=hotspots(repo, args.limit))


def cmd_coupling(args):
    repo = repo_root()
    _print_git_result(
        "coupling",
        repo,
        args,
        couplings=coupling(repo, args.minimum_commits, args.limit),
    )


def cmd_owners(args):
    repo = repo_root()
    _print_git_result("owners", repo, args, ownership=owners(repo, args.path))


def cmd_history(args):
    repo = repo_root()
    _print_git_result("history", repo, args, history=history(repo, args.query, args.limit))


def cmd_index(args):
    repo = repo_root()
    result = envelope(
        "index",
        repo,
        **build_index(
            repo,
            max_file_size=args.max_file_size,
            max_repository_size=args.max_repository_size,
        ),
    )
    if args.format == "json":
        print(dumps(result))
        return
    print_header("ContextForge — Index")
    for key in (
        "indexed",
        "unchanged",
        "removed",
        "total_files",
        "symbols",
        "references",
        "relationships",
        "framework_entities",
    ):
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
        unresolved = (
            f"  [yellow]unresolved: {edge['reason']}[/yellow]"
            if edge.get("resolved") is False
            else ""
        )
        console.print(
            f"{edge['from']} → {edge['to']}  [dim]{edge['path']}[/dim]{unresolved}"
        )


def cmd_routes(args):
    return cmd_entities(args)


def cmd_entities(args):
    repo = repo_root()
    entities = find_framework_entities(repo, kinds=args.entity_kinds)
    result = envelope(args.command, repo, **{args.command: entities})
    if args.format == "json":
        print(dumps(result))
        return
    print_header(f"ContextForge — {args.command.title()}")
    for entity in entities:
        if args.entity_kinds == ("route",):
            attributes = entity["attributes"]
            label = f"{attributes.get('method', 'ANY'):>7}  {attributes.get('route', entity['name'])}"
        else:
            label = entity["name"]
        target = f" → {entity['target']}" if entity["target"] else ""
        console.print(
            f"{label}{target}  [dim]{entity['path']}:{entity['line']}[/dim]"
        )


def cmd_task_report(args):
    repo = repo_root()
    report = task_report(repo, args.query, args.command)
    result = envelope(args.command, repo, report=report)
    if args.format == "json":
        print(dumps(result))
        return
    print_header(f"ContextForge — {args.command.title()}: {args.query}")
    console.print(escape(report["summary"]))
    console.print(f"Confidence: {escape(report['confidence'])}")
    if report["facts"]:
        console.print("\n[bold]Observed facts[/bold]")
        for fact in report["facts"]:
            evidence = fact["evidence"]
            console.print(
                f"- {escape(fact['statement'])}  "
                f"{escape(evidence['path'])}:{evidence['line']}"
            )
    if report["inferences"]:
        console.print("\n[bold]Inferences[/bold]")
        for inference in report["inferences"]:
            locations = ", ".join(
                f"{escape(item['path'])}:{item['line']}" for item in inference["evidence"]
            )
            console.print(
                f"- {escape(inference['statement'])}  "
                f"confidence={escape(inference['confidence'])}  "
                f"evidence={locations}"
            )
    if report["execution_paths"]:
        console.print("\n[bold]Execution paths[/bold]")
        for path in report["execution_paths"]:
            names = " → ".join(
                f"{escape(node['name'])} ({escape(node['path'])}:{node['line']})"
                for node in path["nodes"]
            )
            edges = ", ".join(
                f"{escape(edge['kind'])}@{escape(edge['path'])}:{edge['from_line']}"
                f" resolved={edge['resolved']}"
                for edge in path["edges"]
            )
            console.print(f"- {escape(path['entry'])}: {names}  edges={edges}")
    mode_sections = {
        "impact": ("Affected areas", "affected_areas"),
        "change": ("Implementation patterns", "implementation_patterns"),
        "debug": ("Ranked hypotheses", "hypotheses"),
    }
    if args.command in mode_sections:
        heading, key = mode_sections[args.command]
        if report[key]:
            console.print(f"\n[bold]{heading}[/bold]")
            for item in report[key]:
                if args.command == "impact":
                    console.print(
                        f"- {escape(item['path'])}: "
                        f"{', '.join(escape(reason) for reason in item['reasons'])}"
                    )
                elif args.command == "change":
                    evidence = item["evidence"]
                    console.print(
                        f"- {escape(item['pattern'])}  "
                        f"{escape(evidence['path'])}:{evidence['line']}"
                    )
                else:
                    evidence = item["evidence"]
                    console.print(
                        f"- {item['rank']}. {escape(item['hypothesis'])}  "
                        f"confidence={escape(item['confidence'])}  "
                        f"{escape(evidence['path'])}:{evidence['line']}"
                    )
    if report["unresolved_questions"]:
        console.print("\n[bold]Unresolved questions[/bold]")
        for question in report["unresolved_questions"]:
            console.print(f"- {escape(question)}")
    if report["risks"]:
        console.print("\n[bold]Risks[/bold]")
        for item in report["risks"]:
            paths = (
                f"  paths={', '.join(escape(path) for path in item['paths'])}"
                if item["paths"]
                else ""
            )
            console.print(
                f"- {escape(item['risk'])}  basis={escape(item['basis'])}{paths}"
            )


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
            console.print(f"- {escape(item['path'])}: {escape(item['reason'])}")



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
    parser = JSONArgumentParser(
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

    hotspots_parser = add_format(subcommands.add_parser("hotspots", help="Rank frequently changed files"))
    hotspots_parser.add_argument("--limit", type=int, default=20)
    hotspots_parser.set_defaults(func=cmd_hotspots)

    coupling_parser = add_format(subcommands.add_parser("coupling", help="Find files that change together"))
    coupling_parser.add_argument("--minimum-commits", type=int, default=2)
    coupling_parser.add_argument("--limit", type=int, default=20)
    coupling_parser.set_defaults(func=cmd_coupling)

    owners_parser = add_format(subcommands.add_parser("owners", help="Show contributors for a path"))
    owners_parser.add_argument("path", help="Repository-relative file path")
    owners_parser.set_defaults(func=cmd_owners)

    history_parser = add_format(subcommands.add_parser("history", help="Search Git history and renames"))
    history_parser.add_argument("query", help="Commit message or path query")
    history_parser.add_argument("--limit", type=int, default=50)
    history_parser.set_defaults(func=cmd_history)

    index = add_format(subcommands.add_parser("index", help="Build or refresh the incremental repository index"))
    index.add_argument("--max-file-size", type=int, default=None, metavar="BYTES")
    index.add_argument("--max-repository-size", type=int, default=None, metavar="BYTES")
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

    task_commands = {
        "investigate": "Explain how a repository behavior is implemented",
        "impact": "Assess the evidence-backed impact of a proposed change",
        "change": "Find implementation patterns, tests, and risks for a change",
        "debug": "Rank evidence-backed debugging hypotheses",
    }
    for command, command_help in task_commands.items():
        task = add_format(subcommands.add_parser(command, help=command_help))
        task.add_argument("query", help="Question, proposed change, or observed symptom")
        task.set_defaults(func=cmd_task_report)

    routes = add_format(
        subcommands.add_parser("routes", help="List framework-aware route registrations")
    )
    routes.set_defaults(func=cmd_routes, entity_kinds=("route",))

    models = add_format(
        subcommands.add_parser("models", help="List framework-aware models")
    )
    models.set_defaults(func=cmd_entities, entity_kinds=("model",))

    jobs = add_format(
        subcommands.add_parser("jobs", help="List framework-aware jobs")
    )
    jobs.set_defaults(func=cmd_entities, entity_kinds=("job",))

    services = add_format(
        subcommands.add_parser("services", help="List framework-aware services")
    )
    services.set_defaults(
        func=cmd_entities, entity_kinds=("service", "repository")
    )

    boundaries = add_format(
        subcommands.add_parser(
            "boundaries", help="List framework-aware middleware and authorization"
        )
    )
    boundaries.set_defaults(
        func=cmd_entities, entity_kinds=("middleware", "authorization")
    )

    brief = add_format(subcommands.add_parser("brief", help="Generate an evidence-backed repository briefing"))
    brief.set_defaults(func=cmd_brief)

    return parser



def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args) or 0
    except ContextForgeError as exc:
        if getattr(args, "format", "text") == "json":
            print(dumps(envelope(args.command, repo_root(), error=exc.as_dict())))
        else:
            print(f"ContextForge error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if getattr(args, "format", "text") == "json":
            failure = {
                "code": "internal_error",
                "message": str(exc) or exc.__class__.__name__,
                "details": {"type": exc.__class__.__name__},
            }
            print(dumps(envelope(args.command, repo_root(), error=failure)))
            return 1
        raise


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
