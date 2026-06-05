"""Pydantic-based configuration for Mini-Claw.

Configuration is stored as ``config.yaml`` in the current working directory.
Run ``mini-claw setup`` in your project directory to generate it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


CONFIG_FILENAME = "config.yaml"


def get_config_path(path: Path | None = None) -> Path:
    """Resolve the config path. Defaults to ``./config.yaml`` in the cwd."""
    if path is not None:
        return path
    return Path.cwd() / CONFIG_FILENAME


def get_data_dir(config_path: Path | None = None) -> Path:
    """Directory used for runtime data (sqlite db, etc).

    Co-located with the config file so the project directory stays
    self-contained.
    """
    return get_config_path(config_path).parent


class ConcurrencyConfig(BaseModel):
    """Concurrency and locking configuration.

    For single-process deployments, use default settings.
    For multi-process deployments, set lock_backend to 'file' and cache_mode to 'db'.
    """

    lock_backend: Literal["asyncio", "file", "sqlite"] = "asyncio"
    """Lock backend: 'asyncio' (single-process), 'file' (multi-process), 'sqlite' (cross-platform multi-process)."""

    cache_mode: Literal["memory", "db"] = "memory"
    """Cache mode: 'memory' (single-process), 'db' (multi-process, disables in-memory cache)."""

    file_lock_dir: str = "./data/locks"
    """Directory for file locks (only used when lock_backend='file')."""

    lock_timeout: float = 30.0
    """Lock acquisition timeout in seconds."""


class PluginsConfig(BaseModel):
    """Plugin system configuration."""

    integrity_mode: Literal["strict", "warn"] = "strict"
    """Integrity check mode: 'strict' (reject hash mismatch) or 'warn' (log only, allow load)."""


class ProviderConfig(BaseModel):
    provider: str = "deepseek"
    api_key: str = ""
    model: str = "deepseek-chat"
    base_url: Optional[str] = None


class FeishuChannelConfig(BaseModel):
    """Feishu (Lark) channel — long-connection mode.

    Long-connection (WebSocket) only requires ``app_id`` / ``app_secret``;
    the SDK opens an outbound connection to Feishu and events are pushed
    over it, so no Webhook URL, ``verification_token``, or ``encrypt_key``
    is needed.
    """

    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    health_check_interval_sec: int = 60
    restart_on_disconnect: bool = True
    idle_restart_seconds: int = 0


class ChannelConfig(BaseModel):
    name: str
    type: str
    enabled: bool = True
    options: dict = Field(default_factory=dict)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str = ""


class PermissionsHighRiskConfig(BaseModel):
    allow_explicit: bool = False
    allowed_command_templates: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Shell blacklist defaults
# ---------------------------------------------------------------------------
# Patterns matched via ``re.search`` (substring-anywhere) on the command
# string. Keep word-bounded (``\b``) and flag-anchored where possible to
# avoid firing on safe commands like ``echo 'hello'``, ``ls -la``, or
# ``pytest -k 'rm or curl'``. Mirror updates here in any project-level
# ``config.yaml`` overrides.

_DEFAULT_SHELL_BLACKLIST: tuple[str, ...] = (
    # --- shell-internal destruction ---
    r"\brm\s+(?:-[rRfv]+\s+|--recursive\s+|--force\s+)+/(?:\s|$)",
    r"\brm\s+(?:-[rRfv]+\s+|--recursive\s+|--force\s+)+~(?:/|\s|$)",
    r"\brm\s+(?:-[rRfv]+\s+|--recursive\s+|--force\s+)+\$HOME\b",
    r"\brm\s+(?:-[rRfv]+\s+|--recursive\s+|--force\s+)+/\*",
    r"\bmkfs(?:\.|\s)",
    r"\bdd\s+if=",
    r":\(\)\s*\{",
    r"\bshred\s+-",
    r"\bswapoff\s+-a\b",
    # --- destructive find ---
    r"\bfind\b[^|;&]*\s-delete\b",
    r"\bfind\b[^|;&]*-exec\s+rm\b",
    # --- network -> shell pipes ---
    r"\bcurl\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b",
    r"\bwget\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b",
    r"\bfetch\b[^|]*\|\s*(?:ba)?sh\b",
    # --- inline interpreters (-c / -e / -r) ---
    r"\bbash\s+-c\b",
    r"\bsh\s+-c\b",
    r"\bzsh\s+-c\b",
    r"\bpython3?\s+-c\b",
    r"\bnode\s+-e\b",
    r"\bperl\s+-e\b",
    r"\bruby\s+-e\b",
    r"\bphp\s+-r\b",
    # --- encoding / decoding bypass ---
    r"\bbase64\s+(?:-d|--decode)\b[^|]*\|\s*(?:ba)?sh\b",
    r"\bxxd\s+-r\b[^|]*\|\s*(?:ba)?sh\b",
    r"\bopenssl\s+enc\s+-d\b[^|]*\|\s*(?:ba)?sh\b",
    # --- eval / command substitution feeding shell ---
    r"\beval\s+[\"']?\$\(\s*curl\b",
    r"\beval\s+[\"']?\$\(\s*wget\b",
    r"`\s*curl\b",
    r"`\s*wget\b",
    # --- credential / system file overwrite (Linux) ---
    r">\s*~/\.ssh/",
    r">\s*\$HOME/\.ssh/",
    r">\s*/etc/passwd\b",
    r">\s*/etc/shadow\b",
    r">\s*/etc/sudoers\b",
    # --- Windows PowerShell vectors ---
    r"(?i)\bpowershell(?:\.exe)?\s+(?:-|/)e(?:nc(?:odedcommand)?)?\b",
    r"(?i)\bpowershell(?:\.exe)?\s+(?:-|/)c(?:ommand)?\b",
    r"(?i)\b(?:iex|Invoke-Expression)\b",
    r"(?i)\bDownloadString\s*\(",
    r"(?i)\biwr\b[^|]*\|\s*iex\b",
)


class ChainDetectorConfig(BaseModel):
    """Chain attack detector configuration (Phase A.3)."""

    enabled: bool = True
    """Enable chain detection. If False, no chain detection runs."""

    session_scope: bool = False
    """Default False: only run-level detection. True: also persists state for cross-message detection."""

    session_ttl: int = 604800
    """Session-level state TTL in seconds (default 7 days)."""


class PermissionsConfig(BaseModel):
    default_level: str = "L2"
    sandbox_mode: str = "safe"  # "safe" = path sandbox + sensitive check; "bypass" = no restrictions
    require_confirm: list[str] = Field(default_factory=lambda: ["L3"])
    deny_by_default: list[str] = Field(default_factory=lambda: ["L4"])
    shell_blacklist: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_SHELL_BLACKLIST)
    )
    high_risk: PermissionsHighRiskConfig = Field(
        default_factory=PermissionsHighRiskConfig
    )
    chain_detector: ChainDetectorConfig = Field(default_factory=ChainDetectorConfig)


class WorkflowTemplateConfig(BaseModel):
    enabled: bool = True


class WorkflowRiskPolicyConfig(BaseModel):
    write_file: str = "approval"
    run_shell: str = "approval"
    dynamic_workflow: str = "approval"


class WorkflowTemplatesConfig(BaseModel):
    debug_fix: WorkflowTemplateConfig = Field(default_factory=WorkflowTemplateConfig)
    code_review: WorkflowTemplateConfig = Field(default_factory=WorkflowTemplateConfig)
    migration: WorkflowTemplateConfig = Field(default_factory=WorkflowTemplateConfig)


class WorkflowAutoDetectConfig(BaseModel):
    """Phase 7: keyword pre-filter + LLM intent classification.

    Only consulted when ``WorkflowConfig.auto_detect`` is True. The pre-filter
    uses ``WorkflowPlanner.should_use_workflow`` keywords (zero LLM cost). The
    LLM fallback is invoked only for messages whose length falls in the
    ``[min_chars, max_chars]`` band — short messages skip workflow, long ones
    fall through to ``code_review`` via existing length heuristic.
    """

    min_chars: int = 80
    max_chars: int = 500
    llm_timeout_ms: int = 4000


class WorkflowPromptReviewConfig(BaseModel):
    """Phase 7: automatic prompt_reviewer node injection.

    When enabled, every workflow gets a ``prompt_reviewer`` subagent node
    inserted between the original subagents and the merge node. The reviewer
    inspects upstream redacted prompts and produces ``{approved, prompt_issues}``.
    Issues at or above ``severity_threshold`` cause the workflow to escalate to
    ``awaiting_approval`` for human review.
    """

    enabled: bool = True
    severity_threshold: Literal["low", "medium", "high"] = "medium"
    node_id: str = "prompt_review"
    timeout: int = 180


class WorkflowConfig(BaseModel):
    enabled: bool = False
    auto_detect: bool = False
    require_approval: bool = True
    max_nodes_per_workflow: int = 8
    max_parallel_nodes: int = 3
    max_total_agent_runs: int = 12
    allow_dynamic: bool = False
    allow_llm_generated_script: bool = False
    max_prompt_chars: int = 12000
    templates: WorkflowTemplatesConfig = Field(default_factory=WorkflowTemplatesConfig)
    risk_policy: WorkflowRiskPolicyConfig = Field(default_factory=WorkflowRiskPolicyConfig)
    auto_detect_options: WorkflowAutoDetectConfig = Field(
        default_factory=WorkflowAutoDetectConfig
    )
    prompt_review: WorkflowPromptReviewConfig = Field(
        default_factory=WorkflowPromptReviewConfig
    )


class AgentConfig(BaseModel):
    id: str = "default"
    name: str | None = None
    system_prompt: str = (
        "你是一个高效的个人助手，自称噜噜，能调用工具帮用户完成各种任务。\n\n"
        "## Tool Usage Policy\n"
        "- When the user requests an ACTION (create, write, delete, run, execute, index, remember), "
        "you MUST call the corresponding tool.\n"
        "- NEVER claim you have completed an action without actually calling the tool.\n"
        "- If you're unsure which tool to use, ask the user for clarification.\n\n"
        "当用户要求执行操作（创建、写入、删除、运行、索引、记住等）时，你必须调用相应的工具。"
        "绝不要在没有实际调用工具的情况下声称已完成操作。\n\n"
        "## 操作前回应（Prelude）\n"
        "如果用户请求需要调用工具或较长处理，在第一次工具调用的 assistant message 中，"
        "生成一句简短自然的操作前回应，告诉用户你**将要**做什么（不要说**已经**完成）。\n"
        "示例：\n"
        "- 用户\"帮我创建文件\" → 你\"好的，让我为你创建这个文件。\" + [调用 write_file]\n"
        "- 用户\"分析日志\" → 你\"收到，我先读取并整理日志主要错误。\" + [调用 read_file]\n"
        "如果是普通闲聊或无需工具的简单回答，不要生成额外的操作前回应。\n"
        "禁止说\"已完成\"、\"已创建\"、\"测试通过\"等完成声明，只说\"我将\"、\"我会\"、\"让我\"、\"我先\"等将来时态。"
    )
    workspace: str | None = None
    provider: ProviderConfig | None = None
    provider_fallback: list[ProviderConfig] = Field(default_factory=list)
    """Phase B.7: ordered fallback providers when primary fails."""
    model: str | None = None
    enabled: bool = True
    tools: list[str] = Field(
        default_factory=lambda: [
            "current_time",
            "open_app",
            "run_shell",
            "read_file",
            "write_file",
        ]
    )
    skills: list[str] = Field(default_factory=list)
    route_chat_ids: list[str] = Field(default_factory=list)


# ======================================================================
# Phase 8 RAG configuration (M1)
# ======================================================================
# All flags default to False so RAG is opt-in and never changes Phase 0-7
# behavior unless the user explicitly enables it in config.yaml.


class RagNamespacesConfig(BaseModel):
    context_enabled: bool = False
    memory_enabled: bool = False


class RagBackendConfig(BaseModel):
    text_search: str = "fts5"  # fts5 | like
    vector_backend: str = "none"  # none | chroma | milvus | sqlite_vec
    hybrid_enabled: bool = False


class RagFtsConfig(BaseModel):
    enabled: bool = True
    top_k: int = 8


class RagEmbeddingConfig(BaseModel):
    enabled: bool = False
    provider: str = "local"  # local | openai | custom
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    dim: int = 384
    batch_size: int = 32


class RagChromaConfig(BaseModel):
    persist_dir: str = "./data/chroma"
    collection_prefix: str = "miniclaw"


class RagMilvusConfig(BaseModel):
    enabled: bool = False
    uri: str = "http://127.0.0.1:19530"
    collection_prefix: str = "miniclaw"


class RagChunkConfig(BaseModel):
    max_tokens: int = 800
    overlap_tokens: int = 100
    max_file_size_mb: int = 20
    binary_file_policy: str = "deny"  # deny | allow


class RagSecurityConfig(BaseModel):
    allow_index_in_bypass: bool = False
    allow_sensitive_index: bool = False
    require_approval_for_index: bool = False
    require_approval_for_sensitive_index: bool = True
    require_approval_for_memory_write: bool = True


class RagSharingConfig(BaseModel):
    allow_workspace_context_sharing: bool = False
    allow_cross_agent_context: bool = False


class RagRetrievalConfig(BaseModel):
    auto_context_retrieval: bool = False
    auto_memory_retrieval: bool = False  # Legacy alias for auto_user_memory_retrieval
    # Phase 9 M9.5: four-channel auto retrieval (default OFF for backward compat)
    auto_chat_retrieval: bool = False
    auto_user_memory_retrieval: bool = False
    auto_workspace_memory_retrieval: bool = False
    context_top_k: int = 6
    memory_top_k: int = 3
    chat_top_k: int = 5  # Phase 9 M9.1
    min_memory_confidence: float = 0.75
    include_archived_by_default: bool = False


class RagLifecycleConfig(BaseModel):
    warm_after_days: int = 7
    archive_after_days: int = 30
    cold_after_days: int = 90
    delete_after_days: int = 180
    log_ttl_days: int = 7
    keep_tombstone: bool = True


class RagAutoIndexConfig(BaseModel):
    enabled: bool = False
    min_chars: int = 20000
    max_file_size_mb: int = 5
    require_non_sensitive: bool = True


class RagReindexConfig(BaseModel):
    chunker_version: str = "chunker.v1"
    anchor_schema_version: str = "anchor.v1"
    code_anchor_backend: str = "tree_sitter"
    rename_similarity_threshold: float = 0.88
    uncertain_similarity_threshold: float = 0.72
    parse_error_ratio_threshold: float = 0.20
    lock_ttl_seconds: int = 600


class MemoryControlConfig(BaseModel):
    """Phase 9 M9.2: Memory control operations configuration."""

    allow_hard_delete: bool = False
    max_batch_approve: int = 20
    batch_approve_max: int = 20  # alias for max_batch_approve (per plan spec)
    max_batch_reject: int = 20  # mc-11: batch safety limit for reject-all operations
    export_redact_by_default: bool = True
    # Phase 9 plan additions
    auto_candidate: bool = True   # plan: auto candidate intake (compaction/task_state/workflow)
    auto_write: bool = False      # plan: never auto-write memory bypassing approval
    require_approval: bool = True
    allow_export: bool = True
    allow_clear_scope: bool = True
    auto_candidate_from_agent: bool = False  # Whether to auto-generate candidates from agent summaries
    export_large_threshold: int = 50  # Plan spec: large export ≥ 50 rows triggers L3


class MemoryMaintenanceConfig(BaseModel):
    """Phase 9 M9.6: Memory maintenance thresholds."""

    enabled: bool = True
    dupe_threshold: float = 0.85  # Plan spec: 0.85 Jaccard
    conflict_threshold: float = 0.55
    stale_age_days: int = 90
    stale_max_access: int = 1
    auto_apply: bool = False
    auto_run_on_compaction: bool = False
    run_every_days: int = 7
    # Phase 9 plan additions
    suggest_only: bool = True  # Never auto-apply, only generate suggestions
    run_on_startup: bool = False  # Auto-run maintenance scan on startup
    dedupe_text_threshold: float = 0.85  # Plan spec value
    dedupe_embedding_threshold: float = 0.92  # For hybrid mode
    mode: str = "auto"  # auto | text_only | hybrid


class MemoryConfig(BaseModel):
    """Phase 9: Top-level memory configuration container."""

    control: MemoryControlConfig = Field(default_factory=MemoryControlConfig)
    maintenance: MemoryMaintenanceConfig = Field(default_factory=MemoryMaintenanceConfig)


class RagConfig(BaseModel):
    enabled: bool = False
    namespaces: RagNamespacesConfig = Field(default_factory=RagNamespacesConfig)
    backend: RagBackendConfig = Field(default_factory=RagBackendConfig)
    fts: RagFtsConfig = Field(default_factory=RagFtsConfig)
    embedding: RagEmbeddingConfig = Field(default_factory=RagEmbeddingConfig)
    chroma: RagChromaConfig = Field(default_factory=RagChromaConfig)
    milvus: RagMilvusConfig = Field(default_factory=RagMilvusConfig)
    chunk: RagChunkConfig = Field(default_factory=RagChunkConfig)
    security: RagSecurityConfig = Field(default_factory=RagSecurityConfig)
    sharing: RagSharingConfig = Field(default_factory=RagSharingConfig)
    retrieval: RagRetrievalConfig = Field(default_factory=RagRetrievalConfig)
    lifecycle: RagLifecycleConfig = Field(default_factory=RagLifecycleConfig)
    auto_index: RagAutoIndexConfig = Field(default_factory=RagAutoIndexConfig)
    reindex: RagReindexConfig = Field(default_factory=RagReindexConfig)
    memory_control: MemoryControlConfig = Field(default_factory=MemoryControlConfig)
    memory_maintenance: MemoryMaintenanceConfig = Field(default_factory=MemoryMaintenanceConfig)

    @property
    def memory(self) -> MemoryConfig:
        """Phase 9 ct-1: Runtime accessor for top-level memory config.

        Returns a synthetic MemoryConfig that reflects current memory_control
        and memory_maintenance state. This enables code to access config.rag.memory
        consistently, matching the top-level memory: YAML structure.
        """
        return MemoryConfig(
            control=self.memory_control,
            maintenance=self.memory_maintenance,
        )


class ChatSearchConfig(BaseModel):
    """Phase 9 M9.1: Chat search configuration."""

    enabled: bool = False
    allow_global: bool = False
    fts_max_results: int = 50
    # Phase 9 P0.1: workspace scope can optionally include rows whose
    # ``workspace_dir`` was best-effort-inferred during migration.
    include_inferred: bool = False


class AppConfig(BaseModel):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    channels_feishu: FeishuChannelConfig = Field(default_factory=FeishuChannelConfig)
    channels: list[ChannelConfig] = Field(default_factory=list)
    server: ServerConfig = Field(default_factory=ServerConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    chat_search: ChatSearchConfig = Field(default_factory=ChatSearchConfig)
    # Phase 9: top-level memory config (mirrors rag.memory_control / rag.memory_maintenance
    # for forward-compatibility with the planned YAML structure).
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    agents_defaults: AgentConfig | None = None
    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()])

    def model_post_init(self, __context) -> None:
        """Phase 9 ct-1: Ensure top-level memory config propagates to rag.

        When AppConfig is constructed programmatically (not via load_config YAML),
        the top-level memory: fields should still propagate to rag.memory_control
        and rag.memory_maintenance so all runtime paths see consistent config.
        """
        super().model_post_init(__context)
        # Only propagate if top-level memory has non-default values
        if self.memory.control != MemoryControlConfig() or self.memory.maintenance != MemoryMaintenanceConfig():
            # Merge top-level into rag (top-level wins)
            self.rag.memory_control = MemoryControlConfig(
                **{**self.rag.memory_control.model_dump(), **self.memory.control.model_dump(exclude_unset=True)}
            )
            self.rag.memory_maintenance = MemoryMaintenanceConfig(
                **{**self.rag.memory_maintenance.model_dump(), **self.memory.maintenance.model_dump(exclude_unset=True)}
            )


def _merge_agent_defaults(data: dict) -> dict:
    defaults = data.get("agents_defaults")
    agents = data.get("agents")
    if not defaults or not isinstance(defaults, dict) or not isinstance(agents, list):
        return data

    merged_agents = []
    for agent in agents:
        if isinstance(agent, dict):
            merged_agents.append({**defaults, **agent})
        else:
            merged_agents.append(agent)

    data = dict(data)
    data["agents"] = merged_agents
    return data


def _propagate_top_level_memory(data: dict) -> dict:
    """Phase 9: forward top-level ``memory:`` into rag.memory_control /
    rag.memory_maintenance so existing call sites (RagManager, router,
    chain_detector) keep working without ripple changes.

    Top-level wins; existing rag.memory_control/maintenance values are
    preserved for keys not present at the top level.
    """
    mem = data.get("memory")
    if not isinstance(mem, dict):
        return data

    rag = data.setdefault("rag", {}) if isinstance(data.get("rag"), dict) or "rag" not in data else data["rag"]
    if not isinstance(rag, dict):
        return data

    control_top = mem.get("control") if isinstance(mem.get("control"), dict) else None
    maint_top = mem.get("maintenance") if isinstance(mem.get("maintenance"), dict) else None

    if control_top:
        existing = rag.get("memory_control") if isinstance(rag.get("memory_control"), dict) else {}
        rag["memory_control"] = {**existing, **control_top}
    if maint_top:
        existing = rag.get("memory_maintenance") if isinstance(rag.get("memory_maintenance"), dict) else {}
        rag["memory_maintenance"] = {**existing, **maint_top}

    data = dict(data)
    data["rag"] = rag
    return data


def _normalize_channels(data: dict) -> dict:
    data = dict(data)
    if data.get("channels"):
        return data

    feishu = data.get("channels_feishu")
    if isinstance(feishu, dict):
        data["channels"] = [
            {
                "name": "feishu",
                "type": "feishu",
                "enabled": feishu.get("enabled", False),
                "options": {
                    "app_id": feishu.get("app_id", ""),
                    "app_secret": feishu.get("app_secret", ""),
                    "health_check_interval_sec": feishu.get("health_check_interval_sec", 60),
                    "restart_on_disconnect": feishu.get("restart_on_disconnect", True),
                    "idle_restart_seconds": feishu.get("idle_restart_seconds", 0),
                },
            }
        ]
    return data


def load_config(path: Path | None = None) -> AppConfig:
    config_path = get_config_path(path)
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if "channels" in data and isinstance(data["channels"], dict) and "feishu" in data["channels"]:
            data["channels_feishu"] = data["channels"]["feishu"]
            del data["channels"]
        data = _normalize_channels(data)
        data = _merge_agent_defaults(data)
        data = _propagate_top_level_memory(data)
        return AppConfig(**data)
    return AppConfig()
