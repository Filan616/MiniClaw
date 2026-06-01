"""Result processor: truncates large tool outputs for context efficiency."""

from __future__ import annotations

import traceback
import uuid
from dataclasses import dataclass, field


@dataclass(slots=True)
class ToolResultProcessor:
    """Processes tool results to fit within context budgets.

    Large outputs are truncated with a head+tail strategy, preserving
    the beginning and end while noting the omitted middle section.
    """

    max_chars: int = 8000
    head: int = 3000
    tail: int = 3000

    def process(self, result: str, tool_name: str) -> str:
        """Process a tool result string.

        If the result fits within max_chars, return as-is.
        Otherwise, keep head and tail portions with an artifact reference.
        """
        if len(result) <= self.max_chars:
            return result

        artifact_id = uuid.uuid4().hex[:12]
        omitted = len(result) - self.head - self.tail

        head_part = result[: self.head]
        tail_part = result[-self.tail :]

        return (
            f"{head_part}\n"
            f"\n... [{omitted} chars omitted, artifact_id={artifact_id}] ...\n\n"
            f"{tail_part}"
        )

    def process_error(self, err: BaseException) -> str:
        """Extract key error information from an exception.

        Returns a concise representation with the exception type,
        message, and the most relevant traceback frames.
        """
        tb_lines = traceback.format_exception(type(err), err, err.__traceback__)
        full_tb = "".join(tb_lines)

        # Keep it concise: type + message + last few frames
        frames = traceback.extract_tb(err.__traceback__)
        key_frames = frames[-3:] if len(frames) > 3 else frames

        parts: list[str] = [
            f"[{type(err).__name__}] {err}",
            "",
            "Traceback (most recent calls):",
        ]
        for frame in key_frames:
            parts.append(
                f"  File \"{frame.filename}\", line {frame.lineno}, in {frame.name}"
            )
            if frame.line:
                parts.append(f"    {frame.line}")

        result = "\n".join(parts)
        # Apply same max_chars limit
        if len(result) > self.max_chars:
            result = result[: self.max_chars] + "\n... (truncated)"
        return result
