"""Memory candidate validator (Phase 8 M5).

Three rejection layers:

1. **Policy override** — bypass / "ignore previous instructions" / 绕过 /
   自动允许. Reuses :data:`POLICY_LIKE_PHRASES` from M2.5. Same defense
   surface as ChainDetector link D, applied at memory-write time as well.

2. **Sensitive content** — secret patterns reused from prompt_compiler.
   If candidate text contains apparent secrets, refuse storage entirely
   (even if redaction was attempted upstream — defense in depth).

3. **Prompt injection** — known injection openers ("ignore previous",
   "you are now", "system:"). These slip past sensitivity but would
   poison future agent runs once retrieved.

Reuses :func:`mini_claw.workflow.prompt_validator` patterns where
possible to avoid drift across the codebase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mini_claw.permissions.policy import (
    POLICY_LIKE_PHRASES,
    looks_like_policy_override,
)
from mini_claw.rag.memory.candidate import MemoryCandidate
from mini_claw.workflow.prompt_compiler import SECRET_PATTERNS

__all__ = ["MemoryValidator", "ValidationResult"]


# Prompt-injection openers that are rare in genuine user preferences
# but common in prompt-injection attacks.
_INJECTION_PHRASES: tuple[str, ...] = (
    "ignore previous",
    "ignore the above",
    "disregard previous",
    "you are now",
    "you must now",
    "new system prompt",
    "system: ",
    "system:\n",
    "[system]",
    "</system>",
    "<system>",
    "你现在是",
    "现在你是",
    "忽略之前",
    "忘记之前",
)

# Phase 9 M9.4: Low-quality patterns that indicate noise / non-actionable content
_LOW_QUALITY_PATTERNS: tuple[str, ...] = (
    "i don't know",
    "not sure",
    "maybe",
    "perhaps",
    "tbd",
    "todo",
    "fixme",
    "wip",
    "不确定",
    "可能",
    "也许",
    "待定",
    "待办",
)

# Phase 9 M9.4: Generic / vague phrases that don't constitute durable knowledge
_VAGUE_PATTERNS: tuple[str, ...] = (
    "ok",
    "okay",
    "sure",
    "got it",
    "thanks",
    "thank you",
    "好的",
    "明白",
    "知道了",
    "谢谢",
)


@dataclass(slots=True)
class ValidationResult:
    """Outcome of :meth:`MemoryValidator.validate`."""

    ok: bool
    reason: str = ""
    matched_phrases: list[str] = None  # type: ignore[assignment]
    category: str = ""  # policy_override | sensitive | injection | empty | ok

    def __post_init__(self) -> None:
        if self.matched_phrases is None:
            self.matched_phrases = []


class MemoryValidator:
    """Stateless validator. Cheap to construct; no I/O."""

    def validate(self, candidate: MemoryCandidate) -> ValidationResult:
        """Return ok=False with category set when *candidate* must be rejected."""
        text = (candidate.content or "").strip()
        if not text:
            return ValidationResult(ok=False, reason="empty content", category="empty")

        # 1. Policy override — same surface as ChainDetector link D
        if looks_like_policy_override(text):
            matched = [p for p in POLICY_LIKE_PHRASES if p.lower() in text.lower()]
            return ValidationResult(
                ok=False,
                reason="content looks like a policy override",
                matched_phrases=matched,
                category="policy_override",
            )

        # 2. Sensitive content (secrets) — reuse 5 secret patterns
        for pattern in SECRET_PATTERNS:
            m = pattern.search(text)
            if m:
                # Don't echo the matched secret back; report category only.
                return ValidationResult(
                    ok=False,
                    reason="content contains secret-like pattern",
                    matched_phrases=[pattern.pattern[:60]],
                    category="sensitive",
                )

        # 3. Prompt injection openers
        lowered = text.lower()
        injection_hits = [p for p in _INJECTION_PHRASES if p in lowered]
        if injection_hits:
            return ValidationResult(
                ok=False,
                reason="content contains prompt-injection phrasing",
                matched_phrases=injection_hits,
                category="injection",
            )

        # 4. Phase 9 M9.4: Quality filter — low-quality / vague / non-actionable
        # Reject extremely short candidates that cannot carry meaning
        # (kept loose: explicit /memory remember commands often use compact rules
        # like "rule x"; reject only single-token noise.)
        if len(text) < 4:
            return ValidationResult(
                ok=False,
                reason="content too short to be meaningful",
                category="low_quality",
            )

        # Reject candidates dominated by uncertainty markers (only when short)
        low_quality_hits = [p for p in _LOW_QUALITY_PATTERNS if p in lowered]
        if low_quality_hits and len(text) < 80:
            # Short text with uncertainty markers = low quality
            return ValidationResult(
                ok=False,
                reason="content is too uncertain / non-actionable",
                matched_phrases=low_quality_hits,
                category="low_quality",
            )

        # Reject candidates that are just acknowledgments
        stripped = text.strip().rstrip("。.!?！？")
        if stripped.lower() in _VAGUE_PATTERNS:
            return ValidationResult(
                ok=False,
                reason="content is a generic acknowledgment, not durable knowledge",
                category="low_quality",
            )

        return ValidationResult(ok=True, category="ok")
