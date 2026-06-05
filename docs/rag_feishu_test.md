# MiniClaw RAG 飞书测试

## 审批规则
RAG 索引操作必须走 PermissionGate，不能因为 read_file 成功就自动允许索引。

## Reindex 规则
文档小幅变更时应该优先做 incremental reindex，只更新 changed chunks；大改动、chunker 版本变化或 anchor schema 变化时再 fallback full reindex。

## Memory 规则
自动记忆只能写 memory_candidates，不能直接写入长期 memory。
