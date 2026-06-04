"""Phase 8: Context RAG + Memory RAG + Vector Retrieval.

M1: Schema + RagStore + 配置骨架（无外部行为变化）

本模块提供：
- 长文档/代码/日志的索引与检索（Context RAG）
- 跨会话的长期记忆存储与检索（Memory RAG）
- 多种向量后端支持（FTS5 / Chroma / Milvus / sqlite-vec）
- 完整的权限控制、审计、生命周期管理

关键安全原则：
- read_file 成功 ≠ index_context 允许
- bypass read 不允许 index
- 索引/检索/记忆写入全部经过 PermissionGate + SecurityAuditLogger
- RAG 操作纳入 ChainDetector 跟踪
- 所有 namespace（context / memory）强制隔离
"""

from __future__ import annotations

__all__ = ["models", "store"]