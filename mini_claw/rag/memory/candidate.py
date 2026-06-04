"""Memory candidate scoring (Phase 8 M5).

Re-exports :class:`MemoryCandidate` from rag.models so callers in this
package don't need to reach across packages, and adds the
``should_store_memory`` decision function.

Scoring (RAG.md §7.3):
    should_store = (
        stability >= 3
        and reuse_value >= 3
        and sensitivity <= 2
        and confidence >= 0.7
    )

User-explicit memories ("/memory remember <text>") may pass even with
lower stability — but sensitivity / policy override checks NEVER bypass.
"""

from __future__ import annotations

from typing import Any

from mini_claw.rag.models import MemoryCandidate

__all__ = ["MemoryCandidate", "should_store_memory"]


# Default thresholds (overridable via RagConfig in policy.py)
_STABILITY_THRESHOLD = 3
_REUSE_THRESHOLD = 3
_SENSITIVITY_MAX = 2
_CONFIDENCE_THRESHOLD = 0.7


def should_store_memory(
    candidate: MemoryCandidate,
    *,
    explicit: bool = False,
) -> tuple[bool, str]:
    """Decide whether *candidate* meets the bar for long-term storage.

    Returns ``(should_store, reason)``. ``reason`` describes the failing
    threshold so callers can surface it to the user / audit.

    When ``explicit=True`` (user typed ``/memory remember``), stability
    and reuse_value gates are relaxed, but sensitivity stays strict.
    """
    if candidate.sensitivity > _SENSITIVITY_MAX:
        return False, f"sensitivity {candidate.sensitivity} exceeds max {_SENSITIVITY_MAX}"

    if candidate.confidence < _CONFIDENCE_THRESHOLD:
        return (
            False,
            f"confidence {candidate.confidence:.2f} below {_CONFIDENCE_THRESHOLD}",
        )

    if not explicit:
        if candidate.stability < _STABILITY_THRESHOLD:
            return (
                False,
                f"stability {candidate.stability} below {_STABILITY_THRESHOLD}",
            )
        if candidate.reuse_value < _REUSE_THRESHOLD:
            return (
                False,
                f"reuse_value {candidate.reuse_value} below {_REUSE_THRESHOLD}",
            )

    return True, ""
