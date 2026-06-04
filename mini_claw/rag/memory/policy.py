"""Memory storage policy (Phase 8 M5).

Thin wrapper that combines :func:`should_store_memory` (scoring) with
:class:`MemoryValidator` (content rejection) into a single decision.

Used by :class:`MemoryStore` as the **last gate before candidate -> item**.
Even explicit user requests go through this; only the scoring relaxes.
"""

from __future__ import annotations

from dataclasses import dataclass

from mini_claw.rag.memory.candidate import MemoryCandidate, should_store_memory
from mini_claw.rag.memory.validator import MemoryValidator, ValidationResult

__all__ = ["MemoryDecision", "evaluate_candidate"]


@dataclass(slots=True)
class MemoryDecision:
    """Combined decision returned by :func:`evaluate_candidate`."""

    should_store: bool
    reason: str = ""
    category: str = ""  # ok | sensitivity | confidence | injection | sensitive | policy_override | empty
    matched_phrases: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.matched_phrases is None:
            self.matched_phrases = []


def evaluate_candidate(
    candidate: MemoryCandidate,
    *,
    validator: MemoryValidator | None = None,
    explicit: bool = False,
) -> MemoryDecision:
    """Apply scoring + validator. Validator wins on rejection.

    ``explicit`` corresponds to a user-typed ``/memory remember``: scoring
    relaxes (stability/reuse_value floors drop) but validator stays strict.
    """
    validator = validator or MemoryValidator()

    # Validator always runs first — security gate has priority.
    vr: ValidationResult = validator.validate(candidate)
    if not vr.ok:
        return MemoryDecision(
            should_store=False,
            reason=vr.reason,
            category=vr.category,
            matched_phrases=list(vr.matched_phrases),
        )

    # Scoring (relaxed for explicit user requests)
    ok, reason = should_store_memory(candidate, explicit=explicit)
    if not ok:
        # Map reason text to a stable category for audit
        cat = "scoring"
        if "sensitivity" in reason:
            cat = "sensitivity"
        elif "confidence" in reason:
            cat = "confidence"
        elif "stability" in reason:
            cat = "stability"
        elif "reuse_value" in reason:
            cat = "reuse_value"
        return MemoryDecision(should_store=False, reason=reason, category=cat)

    return MemoryDecision(should_store=True, category="ok")
