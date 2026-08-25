from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

ClaimStatus = Literal["observed", "inferred"]
Confidence = Literal["high", "medium", "low", "unknown"]

_STATUSES = {"observed", "inferred"}
_CONFIDENCE_LEVELS = {"high", "medium", "low", "unknown"}


def _items(values: Iterable[Any] | None) -> list[Any]:
    return list(values or ())


def evidence(
    path: str,
    line: int | None = None,
    *,
    kind: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"path": path}
    if line is not None:
        item["line"] = line
    if kind is not None:
        item["kind"] = kind
    if detail is not None:
        item["detail"] = detail
    return item


def claim(
    statement: str,
    *,
    status: ClaimStatus,
    confidence: Confidence,
    supporting_evidence: Iterable[Mapping[str, Any] | str] | None = None,
    conflicting_evidence: Iterable[Mapping[str, Any] | str] | None = None,
    unresolved_uncertainty: Iterable[str] | None = None,
) -> dict[str, Any]:
    if status not in _STATUSES:
        raise ValueError(f"unsupported claim status: {status}")
    if confidence not in _CONFIDENCE_LEVELS:
        raise ValueError(f"unsupported confidence: {confidence}")

    support = _items(supporting_evidence)
    conflicts = _items(conflicting_evidence)
    uncertainty = _items(unresolved_uncertainty)
    effective_confidence: Confidence = confidence if support else "unknown"

    return {
        "claim": statement,
        "status": status,
        "confidence": effective_confidence,
        "supporting_evidence": support,
        "conflicting_evidence": conflicts,
        "unresolved_uncertainty": uncertainty,
    }
