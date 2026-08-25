from __future__ import annotations

import json
from collections.abc import Iterable


def _text(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def _code(value: object) -> str:
    text = _text(value)
    fence = "`" if "`" not in text else "``"
    return f"{fence}{text}{fence}"


def _heading(value: object) -> str:
    return _text(value).replace("#", "\\#")


def _bullet(value: object) -> str:
    return _text(value).replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")


def _location(item: dict) -> str:
    path = _code(item.get("path", "unknown"))
    line = item.get("line")
    return f"{path}:{line}" if line is not None else path


def _section(lines: list[str], title: str, entries: Iterable[str]) -> None:
    values = list(entries)
    if not values:
        return
    lines.extend(("", f"## {title}", ""))
    lines.extend(f"- {value}" for value in values)


def markdown_export(payload: dict) -> str:
    command = str(payload.get("command", "report"))
    title = f"ContextForge {command.replace('_', ' ').title()}"
    lines = [
        f"# {_heading(title)}",
        "",
        "## Repository",
        "",
        _code(payload.get("repository", "")),
    ]

    if query := payload.get("query"):
        lines.extend(("", "## Query", "", _code(query)))

    if command == "brief":
        if description := payload.get("description"):
            lines.extend(("", _bullet(description)))
        detections = (_bullet(item) for item in payload.get("detections", []))
        _section(lines, "Detections", detections)
        architecture = payload.get("architecture", {})
        if architecture:
            lines.extend(("", "## Architecture"))
            for category in sorted(architecture):
                lines.extend(("", f"### {_heading(category)}", ""))
                lines.extend(f"- {_code(path)}" for path in architecture[category])
        entries = []
        for item in payload.get("entry_points", []):
            label = item.get("command") or item.get("name") or item.get("target")
            target = item.get("target") or item.get("module", "")
            entries.append(f"{_code(label)} → {_code(target)}")
        _section(lines, "Entry Points", entries)
        evidence = (
            f"{_bullet(item.get('claim', 'evidence'))}: {_location(item)}"
            for item in payload.get("evidence", [])
        )
        _section(lines, "Evidence", evidence)
        lines.extend(
            ("", "## Confidence", "", _bullet(payload.get("confidence", "unknown")))
        )
    elif "report" in payload:
        report = payload["report"]
        lines.extend(("", _bullet(report.get("summary", ""))))
        _section(
            lines,
            "Observed Facts",
            (
                f"{_bullet(item['statement'])} ({_location(item['evidence'])})"
                for item in report.get("facts", [])
            ),
        )
        _section(
            lines,
            "Inferences",
            (
                f"{_bullet(item['statement'])} (confidence: {_code(item['confidence'])})"
                for item in report.get("inferences", [])
            ),
        )
        _section(
            lines,
            "Unresolved Questions",
            (_bullet(item) for item in report.get("unresolved_questions", [])),
        )
        lines.extend(
            ("", "## Confidence", "", _bullet(report.get("confidence", "unknown")))
        )
    else:
        for key in sorted(payload):
            if key in {"schema_version", "command", "repository", "query"}:
                continue
            value = payload[key]
            lines.extend(("", f"## {_heading(key.replace('_', ' ').title())}", ""))
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        lines.append(
                            f"- {_code(json.dumps(item, sort_keys=True, ensure_ascii=False))}"
                        )
                    else:
                        lines.append(f"- {_bullet(item)}")
            elif isinstance(value, dict):
                lines.append(
                    f"```json\n{json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)}\n```"
                )
            else:
                lines.append(_bullet(value))

    return "\n".join(lines).rstrip() + "\n"


def _graph_nodes(payload: dict) -> list[dict]:
    return sorted(
        payload.get("nodes", []),
        key=lambda item: (
            str(item.get("name", "")),
            str(item.get("path", "")),
            int(item.get("line", 0)),
        ),
    )


def _graph_edges(payload: dict) -> list[dict]:
    return sorted(
        payload.get("edges", []),
        key=lambda item: (
            str(item.get("from", "")),
            str(item.get("to", "")),
            str(item.get("path", "")),
            int(item.get("from_line", 0)),
        ),
    )


def _node_ids(nodes: list[dict], edges: list[dict]) -> dict[str, str]:
    names = {str(node.get("name", "")) for node in nodes}
    names.update(str(edge.get(key, "")) for edge in edges for key in ("from", "to"))
    return {name: f"n{index}" for index, name in enumerate(sorted(names))}


def _mermaid_label(value: object) -> str:
    return _text(value).replace("\\", "\\\\").replace('"', "&quot;")


def mermaid_export(payload: dict) -> str:
    nodes = _graph_nodes(payload)
    edges = _graph_edges(payload)
    identifiers = _node_ids(nodes, edges)
    lines = ["flowchart TD"]
    for name, identifier in identifiers.items():
        lines.append(f'  {identifier}["{_mermaid_label(name)}"]')
    for edge in edges:
        source = identifiers[str(edge.get("from", ""))]
        target = identifiers[str(edge.get("to", ""))]
        label = _mermaid_label(edge.get("kind", "calls"))
        lines.append(f"  {source} -->|{label}| {target}")
    return "\n".join(lines) + "\n"


def _dot(value: object) -> str:
    return _text(value).replace("\\", "\\\\").replace('"', '\\"')


def graphviz_export(payload: dict) -> str:
    nodes = _graph_nodes(payload)
    edges = _graph_edges(payload)
    identifiers = _node_ids(nodes, edges)
    lines = ["digraph contextforge {", "  rankdir=LR;"]
    for name, identifier in identifiers.items():
        lines.append(f'  {identifier} [label="{_dot(name)}"];')
    for edge in edges:
        source = identifiers[str(edge.get("from", ""))]
        target = identifiers[str(edge.get("to", ""))]
        label = _dot(edge.get("kind", "calls"))
        lines.append(f'  {source} -> {target} [label="{label}"];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def export(payload: dict, format_name: str) -> str:
    exporters = {
        "markdown": markdown_export,
        "mermaid": mermaid_export,
        "graphviz": graphviz_export,
    }
    try:
        renderer = exporters[format_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported export format: {format_name}") from exc
    return renderer(payload)
