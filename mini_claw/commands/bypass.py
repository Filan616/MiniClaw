"""Slash command handlers for /bypass.

Implements four subcommands governing the per-session bypass sandbox mode:

    /bypass next        Single-use bypass; auto-reverts after the next tool call.
                        Stored as sandbox_mode_expires_at = 0 (sentinel).
    /bypass <duration>  TTL bypass (e.g. 10m, 1h). Stored as
                        sandbox_mode_expires_at = now + duration.
    /bypass persistent  Requests long-running bypass; requires confirmation.
                        A pending_confirmations row is created with type
                        'bypass_persistent' and a 60s window.
    /bypass confirm     Finalises persistent bypass if a non-expired pending
                        confirmation exists. Stored as
                        sandbox_mode_expires_at = NULL.

Storage layout (sessions table):
    sandbox_mode_override   'bypass' when active, else NULL/'safe'.
    sandbox_mode_expires_at 0 = single-use sentinel
                            >0 = epoch deadline
                            NULL = persistent (only meaningful when persistent=1)
    sandbox_mode_single_use 1 for single-use, else 0.
    sandbox_mode_persistent 1 for persistent, else 0.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_claw.storage.db import Database


SINGLE_USE_SENTINEL = 0
PERSISTENT_CONFIRM_TTL_SECONDS = 60
MAX_TTL_SECONDS = 24 * 60 * 60  # cap /bypass <duration> at 24 hours

_DURATION_RE = re.compile(r"^(\d+)\s*([smh])$", re.I)


@dataclass
class BypassResult:
    """User-facing result for a /bypass command."""

    message: str
    handled: bool = True


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def handle_bypass_command(
    storage: "Database",
    chat_id: str,
    agent_id: str,
    text: str,
    channel_name: str = "feishu",
) -> BypassResult | None:
    """Dispatch a /bypass ... message.

    Returns ``None`` when ``text`` is not a /bypass command, otherwise a
    :class:`BypassResult` whose ``message`` should be sent back to the user.
    """
    stripped = text.strip()
    if not stripped.startswith("/bypass"):
        return None

    remainder = stripped[len("/bypass"):].strip()
    arg = remainder.lower()

    # /bypass or /bypass next -> single-use bypass
    if arg in ("", "next"):
        _set_bypass_single_use(storage, chat_id, agent_id, channel_name)
        return BypassResult("Bypass enabled for next tool only")

    # /bypass persistent -> stage a confirmation request
    if arg == "persistent":
        _create_pending_confirmation(
            storage,
            chat_id,
            agent_id,
            channel_name,
            "bypass_persistent",
            ttl_seconds=PERSISTENT_CONFIRM_TTL_SECONDS,
        )
        return BypassResult(
            "Bypass requires confirmation. Reply /bypass confirm to proceed."
        )

    # /bypass confirm -> commit persistent bypass if a fresh request exists
    if arg == "confirm":
        if not _consume_pending_confirmation(
            storage, chat_id, agent_id, channel_name, "bypass_persistent"
        ):
            return BypassResult(
                "No pending bypass confirmation. Send /bypass persistent first."
            )
        _set_bypass_persistent(storage, chat_id, agent_id, channel_name)
        return BypassResult("Persistent bypass enabled (no expiration)")

    # /bypass <duration>
    duration = _parse_duration(arg)
    if duration is not None:
        if duration <= 0:
            return BypassResult(
                "Invalid duration. Use /bypass 10m, /bypass 1h, etc."
            )
        if duration > MAX_TTL_SECONDS:
            return BypassResult(
                f"Duration too long. Maximum is {MAX_TTL_SECONDS // 3600} hours."
            )
        expires_at = int(time.time()) + duration
        _set_bypass_ttl(storage, chat_id, agent_id, channel_name, expires_at)
        return BypassResult(
            f"Bypass enabled for {_format_duration_human(duration)} "
            f"(until {_format_until(expires_at)})"
        )

    return BypassResult(
        "Unknown bypass command. "
        "Try /bypass next, /bypass 10m, /bypass 1h, /bypass persistent."
    )


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------


def _parse_duration(token: str) -> int | None:
    """Parse '10m', '1h', '30s' to seconds. Returns None if unparseable."""
    m = _DURATION_RE.match(token.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    return None


def _format_until(expires_at: int) -> str:
    """Format epoch timestamp as local HH:MM."""
    return datetime.fromtimestamp(expires_at).strftime("%H:%M")


def _format_duration_human(seconds: int) -> str:
    """Render a duration in seconds as e.g. '10 minutes' or '1 hour'."""
    if seconds % 3600 == 0:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''}"
    if seconds % 60 == 0:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


# ----------------------------------------------------------------------
# Storage mutations
# ----------------------------------------------------------------------


def _set_bypass_single_use(
    storage: "Database", chat_id: str, agent_id: str, channel_name: str
) -> None:
    """Activate single-use bypass via expires_at=0 sentinel + single_use flag."""
    storage.execute(
        "UPDATE sessions SET "
        "sandbox_mode_override='bypass', "
        "sandbox_mode_expires_at=?, "
        "sandbox_mode_single_use=1, "
        "sandbox_mode_persistent=0, "
        "updated_at=? "
        "WHERE channel_name=? AND chat_id=? AND agent_id=?",
        (SINGLE_USE_SENTINEL, int(time.time()), channel_name, chat_id, agent_id),
    )


def _set_bypass_ttl(
    storage: "Database",
    chat_id: str,
    agent_id: str,
    channel_name: str,
    expires_at: int,
) -> None:
    """Activate TTL-bound bypass that auto-expires at the given epoch."""
    storage.execute(
        "UPDATE sessions SET "
        "sandbox_mode_override='bypass', "
        "sandbox_mode_expires_at=?, "
        "sandbox_mode_single_use=0, "
        "sandbox_mode_persistent=0, "
        "updated_at=? "
        "WHERE channel_name=? AND chat_id=? AND agent_id=?",
        (expires_at, int(time.time()), channel_name, chat_id, agent_id),
    )


def _set_bypass_persistent(
    storage: "Database", chat_id: str, agent_id: str, channel_name: str
) -> None:
    """Activate persistent bypass: NULL expires_at + persistent flag."""
    storage.execute(
        "UPDATE sessions SET "
        "sandbox_mode_override='bypass', "
        "sandbox_mode_expires_at=NULL, "
        "sandbox_mode_single_use=0, "
        "sandbox_mode_persistent=1, "
        "updated_at=? "
        "WHERE channel_name=? AND chat_id=? AND agent_id=?",
        (int(time.time()), channel_name, chat_id, agent_id),
    )


def _create_pending_confirmation(
    storage: "Database",
    chat_id: str,
    agent_id: str,
    channel_name: str,
    type_: str,
    ttl_seconds: int,
) -> None:
    """Upsert a pending confirmation row with TTL."""
    now = int(time.time())
    expires_at = now + ttl_seconds
    storage.execute(
        "INSERT OR REPLACE INTO pending_confirmations "
        "(channel_name, chat_id, agent_id, type, expires_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (channel_name, chat_id, agent_id, type_, expires_at, now),
    )


def _consume_pending_confirmation(
    storage: "Database",
    chat_id: str,
    agent_id: str,
    channel_name: str,
    type_: str,
) -> bool:
    """Atomically check + delete a pending confirmation.

    Returns True only if a non-expired record existed. Expired rows are
    cleaned up as a side effect either way to keep the table tidy.
    """
    now = int(time.time())
    row = storage.fetchone(
        "SELECT expires_at FROM pending_confirmations "
        "WHERE channel_name=? AND chat_id=? AND agent_id=? AND type=?",
        (channel_name, chat_id, agent_id, type_),
    )
    if row is None:
        return False

    storage.execute(
        "DELETE FROM pending_confirmations "
        "WHERE channel_name=? AND chat_id=? AND agent_id=? AND type=?",
        (channel_name, chat_id, agent_id, type_),
    )

    return int(row["expires_at"]) >= now
