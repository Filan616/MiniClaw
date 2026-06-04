# Phase 8 补充设计：RAG 安全链路、降级策略、并发一致性与健康检查

## 1. ChainDetector 必须覆盖 RAG 操作

### 1.1 问题背景

MiniClaw 现有 ChainDetector 主要用于检测跨工具、跨消息的危险行为链，例如：

```text
write_script → chmod_exec → exec_script
```

引入 RAG 后，会出现新的攻击链：

```text
index_context(敏感文件或高价值文件)
↓
search_context(query 带有 exfiltration 意图)
↓
write_file / run_shell / send_message 将检索内容写到公开位置或外部位置
```

这类行为不是单个工具调用能看出来的，必须进入 session 级 ChainDetector。

### 1.2 新增 RAG 危险动作类型

在 `session_chain_state` 中新增 RAG 相关动作：

```text
rag_index_file
rag_index_sensitive_attempt
rag_index_high_value_file
rag_search_context
rag_search_sensitive_query
rag_search_exfil_query
rag_context_export_attempt
rag_write_retrieved_content
rag_public_write_after_search
rag_external_send_after_search
memory_candidate_created
memory_write_attempt
memory_write_policy_like_content
```

每条危险动作记录：

```text
agent_id
session_id
chat_id
workspace_dir
tool_name
action_type
item_id
source_path
query
sensitivity_level
timestamp
metadata_json
```

### 1.3 RAG 攻击链规则

#### 规则 A：敏感索引尝试链

```text
index_context(path 命中 sensitive pattern)
→ 后续 search_context
→ write_file / run_shell / send_message
```

处理：

```text
默认 deny 或升级 L3/L4 审批
写 security_audit: rag_sensitive_chain_detected
```

#### 规则 B：检索外泄链

```text
search_context(query 包含 exfil / dump / token / secret / key / password 等意图)
→ write_file 到 public/export/tmp/share 目录
```

处理：

```text
need_approval 或 deny
```

#### 规则 C：Memory 污染链

```text
用户输入/文档中包含“以后记住/以后绕过/以后允许”
→ MemoryConsolidator 生成 policy-like memory
→ memory_remember
```

处理：

```text
强制 L3 审批
PromptValidator 检查越权语句
写 security_audit: memory_poisoning_risk
```

#### 规则 D：RAG 结果外发链

```text
search_context 返回 chunk
→ run_shell curl/post/scp 等外发命令
```

处理：

```text
deny 或 L4 强审批
```

### 1.4 ChainDetector 接入点

RAG 工具执行前：

```text
ChainDetector.evaluate_before_tool(run, tool_call)
```

RAG 工具执行后：

```text
ChainDetector.observe_after_tool(run, tool_call, result, success)
```

新增文件：

```text
mini_claw/permissions/chain_detector.py
mini_claw/rag/chain_hooks.py
```

### 1.5 测试

新增测试：

```text
test_rag_chain_detector.py
- index sensitive attempt 被记录
- search exfil query 被记录
- search_context 后写公开文件触发 approval
- search_context 后 run_shell 外发触发 deny
- MemoryConsolidator 写越权规则触发 memory poisoning risk
```

---

## 2. memory_compact_to_rag 触发机制

### 2.1 问题背景

`memory_compact_to_rag` 不能只是一个工具名。必须明确：

```text
谁触发？
什么时候触发？
和 SessionManager.compact_history 的顺序是什么？
和 TaskState / session_chain_state 如何对接？
是否允许 LLM 主动触发？
```

### 2.2 触发来源

#### 触发源 A：用户显式命令

```text
/memory compact_to_rag
/memory remember <content>
```

特点：

```text
用户主动触发
仍然需要敏感检查
memory_write 仍然是 L3
```

#### 触发源 B：SessionManager 压缩后自动生成候选

调用顺序：

```text
SessionManager.compact_history()
↓
extract_facts_from_messages()
↓
update_task_state()
↓
MemoryExtractor.extract_candidates()
↓
MemoryPolicy.score()
↓
MemoryConsolidator.rewrite()
↓
MemoryValidator.validate()
↓
生成 pending memory_candidates
↓
如需审批则进入 ApprovalStore
```

注意：

```text
SessionManager.compact_history 不直接写 Memory RAG。
它只生成 MemoryCandidate。
真正写入必须经过 MemoryPolicy + MemoryValidator + ApprovalStore。
```

#### 触发源 C：TaskState 超阈值

触发条件：

```text
task_state.facts > 50
或 task_state prompt chars > 8000
或 compact summaries > 3
```

处理：

```text
pinned facts 保留在 TaskState
recent facts 保留在 TaskState
old non-pinned stable facts → MemoryCandidate
```

#### 触发源 D：Workflow 完成

触发点：

```text
WorkflowMerger 完成后
```

候选来源：

```text
key_findings
remaining_risks
architecture_decisions
recommended_next_steps
```

不直接入库，而是进入：

```text
MemoryCandidate → MemoryPolicy → MemoryConsolidator → MemoryValidator → ApprovalStore
```

### 2.3 LLM 是否能主动调用 memory_compact_to_rag

默认策略：

```text
LLM 可以建议 memory_compact_to_rag
但不能无审批直接写 Memory RAG
```

权限：

```text
memory_compact_to_rag = L3
```

如果 LLM 主动调用：

```text
生成 candidates
进入 awaiting_approval
用户 approve 后写入
```

### 2.4 与 session_chain_state 的关系

MemoryExtractor 需要读取：

```text
session_chain_state
task_state
compacted summaries
workflow findings
recent errors
```

但只抽取稳定事实，不抽取攻击链中未确认的内容。

如果某条事实来自可疑链条：

```text
memory_candidate.sensitivity += 1
memory_candidate.confidence -= 0.2
memory_candidate.metadata.chain_risk = true
```

### 2.5 测试

新增测试：

```text
test_memory_compact_trigger.py
- SessionManager.compact_history 后生成 candidates，不直接写 memory
- TaskState 超阈值后 old facts 进入 candidates
- pinned facts 不进入 Memory RAG
- workflow key_findings 生成 candidates
- LLM 调 memory_compact_to_rag 进入 L3 approval
```

---

## 3. RAG 降级策略

### 3.1 问题背景

成熟版 RAG 支持：

```text
SQLite FTS5
Chroma
sqlite-vec
Milvus
local embedding
```

但本地优先系统必须考虑失败情况：

```text
Chroma 没启动
embedding 模型加载失败
sentence-transformers 首次下载失败
Milvus 连接失败
sqlite-vec 不可用
```

不能因为向量后端挂了，就让 Agent 整体不可用。

### 3.2 后端优先级

推荐检索后端优先级：

```text
Hybrid retrieval
  ↓
Vector retrieval
  ↓
FTS5 retrieval
  ↓
metadata-only fallback
```

成熟版默认：

```yaml
rag:
  retrieval:
    fallback_to_fts: true
    notify_on_vector_fallback: true
    fail_closed_for_memory_write: true
```

### 3.3 自动 fallback 策略

#### Chroma 不可用

```text
vector_backend.health_check failed
↓
RagRetriever 标记 vector_unavailable
↓
本次检索 fallback 到 FTS5
↓
写 security_audit: vector_backend_fallback
↓
用户可见提示一次，不重复刷屏
```

用户提示：

```text
当前向量检索不可用，已自动降级为关键词检索。结果可能不如语义检索全面。
```

#### Embedding 模型加载失败

```text
embedding_provider 初始化失败
↓
禁用 vector retrieval
↓
index_context 仍写 FTS5
↓
rag_embeddings 不写入
↓
status = ready_fts_only
```

#### sqlite-vec 不可用

```text
fallback_to_fts = true → 使用 FTS5
fallback_to_fts = false → 报错并提示配置问题
```

#### Milvus 不可用

```text
Milvus backend health failed
↓
fallback to Chroma if configured
↓
否则 fallback to FTS5
```

### 3.4 已有向量索引如何处理

如果 vector backend 临时不可用：

```text
不删除已有向量元数据
rag_embeddings.status = unavailable
检索时跳过 vector backend
后台 health 恢复后自动重新启用
```

如果 embedding model 变化：

```text
rag_embeddings.embedding_model != current_model
↓
标记 embedding_stale
↓
后台 reembed
```

### 3.5 测试

新增测试：

```text
test_rag_degradation.py
- Chroma 挂掉 fallback 到 FTS5
- embedding 加载失败仍可 index FTS
- vector unavailable 不删除 embedding metadata
- fallback 提示只出现一次
- memory_write 在 validator 不可用时 fail closed
```

---

## 4. reindex_context 并发安全

### 4.1 问题背景

重新索引时，如果直接删除旧 chunks 再写新 chunks，会出现查询不一致：

```text
旧 chunks 已删除
新 chunks 还没写完
查询为空

或：

旧 chunks 还在
新 chunks 写了一半
查询返回新旧混合结果
```

成熟实现必须保证 reindex 原子切换。

### 4.2 推荐策略：版本化索引 + 原子切换

给 `rag_items` 增加：

```sql
active_version INTEGER DEFAULT 1;
reindexing_version INTEGER;
```

给 `rag_chunks` 增加：

```sql
version INTEGER NOT NULL DEFAULT 1;
```

reindex 流程：

```text
1. 获取 context-level reindex lock
2. 创建 new_version = active_version + 1
3. 新 chunks 写入 rag_chunks(version=new_version)
4. 新 FTS entries 写入临时标记 version=new_version
5. 新 vector entries 写入 vector backend，metadata 带 version
6. 全部成功后：
   rag_items.active_version = new_version
   rag_items.status = active/warm
7. 删除旧 version chunks / FTS / vector
8. 释放 lock
```

检索时只查：

```text
rag_chunks.version == rag_items.active_version
```

### 4.3 锁策略

新增锁：

```text
RagIndexLock(item_id)
```

锁粒度：

```text
同一个 context_id reindex 串行
同 workspace 的写索引操作受 workspace write lock 约束
search_context 不阻塞，但只读取 active_version
```

也就是说：

```text
reindex 写新版本时，search_context 继续读旧 active_version
切换完成后，search_context 读新 active_version
```

### 4.4 和 workspace lock 的关系

```text
index_context / reindex_context:
- 需要 workspace read lock 读取源文件
- 写 rag store 需要 RagIndexLock
- 不需要长期持有全局 workspace write lock
```

如果索引的是代码库目录：

```text
index_directory 可获取 workspace read snapshot lock
避免边读边改
```

### 4.5 失败回滚

如果 reindex 中途失败：

```text
active_version 不变
新 version chunks 标记 abandoned
后台 cleanup 删除 abandoned chunks
写 security_audit: rag_reindex_failed
```

### 4.6 测试

新增测试：

```text
test_rag_reindex_atomicity.py
- reindex 期间 search_context 仍返回旧版本
- reindex 成功后 search_context 返回新版本
- reindex 失败后 active_version 不变
- 不会返回新旧混合 chunks
- abandoned version 被 cleanup 清理
```

---

## 5. 自动索引与“不信任 LLM”原则的一致性说明

### 5.1 表面矛盾

计划中有两句话看起来冲突：

```text
LLM 不能自己决定把什么东西永久索引。
```

以及：

```text
如果 allow_auto_index=true，ResultProcessor 可以自动调用 index_context。
```

### 5.2 解释

这两者不矛盾，因为：

```text
LLM 决策和系统策略决策不同。
```

#### 禁止的是：

```text
LLM 看完内容后自行决定：
“我要把这个文件永久索引。”
```

#### 允许的是：

```text
系统根据显式配置和确定性规则：
- allow_auto_index=true
- 文件在 workspace 内
- 非敏感文件
- 文件类型允许
- 文件大小允许
- index_context 权限允许
- 写 audit
然后由系统触发 index_context。
```

也就是说，自动索引不是 LLM 主观决定，而是 **用户/管理员配置 + 系统确定性策略** 触发。

### 5.3 默认配置

默认必须保守：

```yaml
rag:
  auto_index:
    enabled: false
```

开启后也必须满足：

```text
non_sensitive
inside_workspace
allowed_file_type
permission_granted
audit_logged
```

### 5.4 文档补充原则

加入计划书核心原则：

```text
自动索引只能由系统确定性策略触发，不能由 LLM 自行决定。
ResultProcessor 只能提出 index suggestion；只有在 allow_auto_index=true 且所有安全条件通过时，才可自动调用 index_context。
```

---

## 6. RAG 健康检查命令

### 6.1 目标

RAG 系统一旦成熟，会涉及多个组件：

```text
SQLite FTS5
Chroma
Milvus
Embedding model
rag_items
rag_chunks
rag_embeddings
lifecycle
memory_candidates
stale/orphan 状态
```

如果出问题，用户需要一个统一健康检查入口。

新增命令：

```bash
mini-claw rag status
```

以及聊天命令：

```text
/rag status
```

### 6.2 输出示例

```text
RAG Status

FTS:
  status: ok
  chunks: 1243
  items: 37

Vector:
  backend: chroma
  status: ok
  collections:
    - miniclaw_context_code: 892 vectors
    - miniclaw_context_document: 241 vectors
    - miniclaw_memory_workspace: 110 vectors

Embedding:
  provider: local
  model: sentence-transformers/all-MiniLM-L6-v2
  status: ok
  dim: 384

Lifecycle:
  active: 4
  warm: 12
  archived: 18
  cold: 3
  stale: 3
  orphan: 1
  deleted: 9

Memory:
  candidates_pending: 2
  active_memory_items: 31
  pinned_memory_items: 6

Degradation:
  vector_fallback_active: false
  last_vector_error: none
```

### 6.3 JSON 输出

```bash
mini-claw rag status --json
```

用于测试和自动化。

### 6.4 健康检查项

```text
FTS table exists
FTS chunk count matches rag_chunks count
Chroma/Milvus health check
Embedding model load check
embedding dimension match
rag_items stale/orphan count
abandoned reindex versions count
pending memory candidates count
vector fallback state
```

### 6.5 测试

新增测试：

```text
test_rag_status.py
- FTS ok
- Chroma unavailable shows degraded
- embedding failure shows degraded
- stale/orphan counts correct
- JSON output valid
```

---

## 7. 对原 Phase 8 计划的补丁清单

需要把以下内容补入计划书：

```text
1. ChainDetector 增加 RAG dangerous actions。
2. RAG 攻击链：index → search → export/write/send。
3. memory_compact_to_rag 明确触发源：用户显式、Session compact、TaskState 阈值、Workflow 完成。
4. Memory 写入顺序：candidate → policy → consolidator → validator → approval → store。
5. Vector backend 降级策略：Chroma/Milvus/embedding 失败 fallback 到 FTS。
6. reindex_context 使用 versioned chunks + atomic switch。
7. 自动索引必须说明是系统确定性策略，不是 LLM 自主决定。
8. 增加 mini-claw rag status / /rag status。
9. 增加相关测试文件。
```

---

## 8. 新增测试总表

```text
test_rag_chain_detector.py
test_memory_compact_trigger.py
test_rag_degradation.py
test_rag_reindex_atomicity.py
test_rag_status.py
```

这些测试必须加入 Phase 8 验收。

---

## 9. 更新后的成熟验收标准

Phase 8 成熟版完成后，除了原有验收，还必须满足：

```text
1. ChainDetector 能识别 RAG 相关危险链。
2. index_context → search_context → write/export 的可疑链条会触发审批或拒绝。
3. memory_compact_to_rag 能从 Session compact、TaskState、Workflow 结果中生成候选记忆。
4. Memory 写入不会直接发生，必须经过候选、策略、校验和审批。
5. Chroma / embedding / Milvus 不可用时，系统自动 fallback 到 FTS。
6. fallback 状态可被用户通过 rag status 看到。
7. reindex_context 是原子切换，不会出现新旧 chunks 混合检索。
8. 自动索引只由系统确定性策略触发，默认关闭。
9. 所有 RAG 操作都写 security_audit。
10. 健康检查命令能显示 FTS、向量后端、embedding、生命周期、memory candidate 状态。
```

---

## 10. 最终结论

这几个补丁必须加入 Phase 8，否则 RAG 会形成新的安全和一致性盲区。

最关键的三句话是：

```text
RAG 操作也会形成攻击链，必须接入 ChainDetector。
RAG 后端可能不可用，必须有降级策略和健康检查。
RAG 重新索引必须版本化，不能让查询读到新旧混合结果。
```

加入这些后，Phase 8 才能从“功能完整”升级为“工程成熟”。
