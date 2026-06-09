"""Phase 10 M10.3: ReActPolicyResolver.

Builds the per-iteration :class:`ReActPolicy` by layering, in order:

1. agent / system defaults (controlled mode);
2. workflow node override;
3. high-risk task defaults;
4. ad-hoc user/task override.
"""

from __future__ import annotations

from typing import Any

from mini_claw.agent.reflection_trigger import ReActPolicy


def _read_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def policy_from_config(config: Any) -> ReActPolicy:
    """Resolve a :class:`ReActPolicy` from an ``agent.react`` config blob.

    ``config`` may be a Pydantic model, a dataclass, or a plain dict.
    Falls back to safe defaults when the config is missing keys.
    """
    if config is None:
        return ReActPolicy()

    default_mode = _read_attr(config, "default_mode", "controlled") or "controlled"
    controlled = _read_attr(config, "controlled")

    # Read controlled-mode knobs
    re_every = bool(_read_attr(controlled, "reflect_every_iteration", False))
    re_finalize = bool(_read_attr(controlled, "reflect_before_finalize", True))
    re_finalize_mode = (
        _read_attr(controlled, "reflect_before_finalize_mode", "deterministic_first")
        or "deterministic_first"
    )
    re_tool_error = bool(_read_attr(controlled, "reflect_on_tool_error", True))
    re_perm_denied = bool(_read_attr(controlled, "reflect_on_permission_denied", True))
    re_appr_rejected = bool(_read_attr(controlled, "reflect_on_approval_rejected", True))
    re_chain = bool(_read_attr(controlled, "reflect_on_chain_blocked", True))
    re_repeat = bool(_read_attr(controlled, "reflect_on_repeated_tool_call", True))
    re_hallu = bool(_read_attr(controlled, "reflect_on_hallucination_guard", True))
    re_empty = bool(_read_attr(controlled, "reflect_on_empty_rag_result", True))
    threshold = _read_attr(controlled, "reflect_on_iteration_threshold", 7)
    threshold_ratio = _read_attr(controlled, "reflect_on_iteration_threshold_ratio", 0.7)

    timeout = int(_read_attr(config, "reflection_timeout_sec", 15) or 15)
    max_refl = int(_read_attr(config, "max_reflection_chars", 4000) or 4000)
    max_obs = int(_read_attr(config, "max_observation_chars", 2500) or 2500)
    store_refl = bool(_read_attr(config, "store_reflection", True))
    fin_enabled = bool(_read_attr(config, "finalizer_enabled", True))
    fin_timeout = int(_read_attr(config, "finalizer_timeout_sec", 20) or 20)

    policy = ReActPolicy(
        mode=default_mode,
        reflect_every_iteration=re_every,
        reflect_before_finalize=re_finalize,
        reflect_before_finalize_mode=re_finalize_mode,
        reflect_on_tool_error=re_tool_error,
        reflect_on_permission_denied=re_perm_denied,
        reflect_on_approval_rejected=re_appr_rejected,
        reflect_on_chain_blocked=re_chain,
        reflect_on_repeated_tool_call=re_repeat,
        reflect_on_hallucination_guard=re_hallu,
        reflect_on_empty_rag_result=re_empty,
        reflect_on_iteration_threshold=int(threshold) if threshold is not None else None,
        reflect_on_iteration_threshold_ratio=(
            float(threshold_ratio) if threshold_ratio is not None else None
        ),
        reflection_timeout_sec=timeout,
        max_reflection_chars=max_refl,
        max_observation_chars=max_obs,
        store_reflection=store_refl,
        finalizer_enabled=fin_enabled,
        finalizer_timeout_sec=fin_timeout,
    )

    if default_mode == "strict":
        policy.apply_high_risk_defaults()
    return policy


def resolve_react_policy(
    *,
    config: Any = None,
    workflow_node: Any = None,
    task_risk: str | None = None,
    user_override: dict | None = None,
) -> ReActPolicy:
    """Compose a final policy from layered overrides."""
    policy = policy_from_config(config)

    node_policy = _read_attr(workflow_node, "react_policy")
    if node_policy is not None:
        node_mode = _read_attr(node_policy, "mode", policy.mode)
        if node_mode == "strict":
            policy.apply_high_risk_defaults()
        else:
            policy.mode = node_mode or policy.mode
        # Selective overrides
        node_every = _read_attr(node_policy, "reflect_every_iteration", None)
        if node_every is not None:
            policy.reflect_every_iteration = bool(node_every)
        node_finalize = _read_attr(node_policy, "reflect_before_finalize", None)
        if node_finalize is not None:
            policy.reflect_before_finalize = bool(node_finalize)

    if task_risk == "high":
        policy.apply_high_risk_defaults()

    if user_override:
        for k, v in user_override.items():
            if hasattr(policy, k):
                setattr(policy, k, v)

    return policy
