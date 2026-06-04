# MiniClaw Phase 8.3.5：Incremental Reindex / Delta Update 成熟执行计划书

## 0. 背景

MiniClaw Phase 8 已规划 Context RAG、Code RAG、Log RAG、Memory RAG、RAG ChainDetector、RAG Lifecycle、Chroma/FTS Hybrid Retrieval、`/rag status` 等能力。

当前已经有这些基础设计：

```text
rag_items.content_hash
rag_items.status = active / warm / stale / orphan / deleted
rag_items.active_version
rag_chunks.version
/context reindex
RagLifecycleManager
RagHealthManager
RAG ChainDetector
FTS5 + Chroma / vector backend
```

这些机制能够解决：

```text
文档改了以后，不继续使用旧索引
reindex 时不出现新旧 chunks 混合
向量库不可用时可以 fallback
RAG 操作会被审计
```

但还缺少一个成熟能力：

> 当用户只改了文档、代码或 log 的一小部分时，不应该全量重建整个 RAG，而应该只更新变化的部分。

因此新增：

```text
Phase 8.3.5：Incremental Reindex / Delta Update
```

---

## 1. 总目标

实现一套成熟的增量索引系统，使 MiniClaw 能够在文档、代码、日志发生小幅变化时：

```text
1. 检测源文件变更；
2. 重新 chunk 当前文件；
3. 用稳定 anchor 匹配旧 chunks；
4. 计算 added / updated / deleted / reused diff；
5. 只更新变化 chunks；
6. 只重算变化 chunks 的 embedding；
7. 只更新变化 chunks 的 FTS / vector backend；
8. 使用 active_version 原子切换；
9. 记录 reindex diff 和 audit；
10. 在变化过大、chunker 变化、embedding model 变化时自动退回 full reindex。
```

核心目标：

```text
小改动 → 增量 reindex
大改动 → 全量 reindex
chunker / embedding 版本变化 → 全量 reindex
```

---

## 2. 核心原则

### 2.1 不按 chunk_index 判断变化

禁止只用 `chunk_index` 匹配新旧 chunks。

原因：

```text
用户在文档前面插入一段内容后，后面所有 chunk_index 都会变化。
如果只按 index 比较，会误判大量 chunks 都变了。
```

正确方式：

```text
文档：用 section_title + first_sentence_hash 生成 anchor_id
代码：用 file_path + symbol_name 生成 anchor_id
日志：用 offset / line range / timestamp range 生成 anchor_id
```

### 2.2 增量更新不能破坏 active_version 原子性

即使是增量更新，也不能直接覆盖旧 chunks。

必须保持：

```text
search_context 永远只读取 active_version
reindex 写 new_version
new_version 全部写完后才切换 active_version
失败则 active_version 不变
```

### 2.3 unchanged chunks 不重算 embedding

对于内容 hash 没变的 chunks：

```text
不重新写 FTS
不重新算 embedding
不重新写 Chroma / Milvus / sqlite-vec
只在新版本映射中复用
```

### 2.4 增量更新也必须走权限和审计

`incremental_reindex` 本质仍然是写 RAG 库。

必须经过：

```text
PermissionGate
Workspace scope check
Sensitive path check
RagIndexLock
SecurityAuditLogger
ChainDetector observe
```

### 2.5 增量失败不能影响旧索引

如果增量更新中途失败：

```text
active_version 不变
旧 chunks 继续可检索
新写入的临时 chunks 标记 abandoned
/rag status 显示 abandoned reindex versions
后台 cleanup 可清理
```

---

## 3. 功能范围

### 3.1 支持对象

成熟版支持三类 source：

```text
document：Markdown / txt / rst / html / json / yaml
code：py / js / ts / java / go / cpp / c / rs / sh
log：log / traceback / pytest output / shell output
```

### 3.2 支持命令

新增或增强：

```text
/context reindex <context_id>
/context reindex <context_id> --incremental
/context reindex <context_id> --full
/context reindex <context_id> --dry-run
/context reindex <context_id> --force
/context diff <context_id>
/context diff <context_id> --last
/rag status
```

### 3.3 支持工具

新增或增强工具：

```text
reindex_context
diff_context
inspect_reindex_diff
cleanup_abandoned_reindex
```

权限等级：

| 工具                          | 权限等级 | 说明               |
| --------------------------- | ---- | ---------------- |
| `diff_context`              | L1   | 只计算 diff，不写库     |
| `inspect_reindex_diff`      | L1   | 查看上次 reindex 差异  |
| `reindex_context`           | L2   | 写 RAG 索引         |
| `cleanup_abandoned_reindex` | L2   | 清理失败版本           |
| `reindex_sensitive_context` | L4   | 敏感材料重索引，默认拒绝或强审批 |

---

## 4. 数据库设计

### 4.1 修改 `rag_items`

新增字段：

```sql
ALTER TABLE rag_items ADD COLUMN chunker_version TEXT DEFAULT 'v1';
ALTER TABLE rag_items ADD COLUMN embedding_model TEXT;
ALTER TABLE rag_items ADD COLUMN incremental_reindex_enabled INTEGER DEFAULT 1;
ALTER TABLE rag_items ADD COLUMN last_reindex_mode TEXT; -- full | incremental
ALTER TABLE rag_items ADD COLUMN last_reindex_at INTEGER;
ALTER TABLE rag_items ADD COLUMN last_reindex_diff_id TEXT;
```

说明：

```text
chunker_version：
  记录索引时使用的 chunker 版本。
  如果当前 chunker_version 与旧版本不同，必须 full reindex。

embedding_model：
  记录索引时使用的 embedding model。
  如果 embedding model 改变，必须 full reindex 或 full reembed。

last_reindex_mode：
  记录上次是 full 还是 incremental。

last_reindex_diff_id：
  指向 rag_reindex_diffs。
```

---

### 4.2 修改 `rag_chunks`

新增字段：

```sql
ALTER TABLE rag_chunks ADD COLUMN anchor_id TEXT;
ALTER TABLE rag_chunks ADD COLUMN chunk_hash TEXT;
ALTER TABLE rag_chunks ADD COLUMN source_hash TEXT;
ALTER TABLE rag_chunks ADD COLUMN is_active INTEGER DEFAULT 1;
ALTER TABLE rag_chunks ADD COLUMN previous_chunk_id TEXT;
ALTER TABLE rag_chunks ADD COLUMN updated_at INTEGER;
ALTER TABLE rag_chunks ADD COLUMN chunker_version TEXT DEFAULT 'v1';
```

说明：

```text
anchor_id：
  稳定匹配 ID。
  文档按标题/段落生成。
  代码按 symbol 生成。
  log 按 offset / timestamp 生成。

chunk_hash：
  当前 chunk 内容 hash。

source_hash：
  当前源文件 hash。

previous_chunk_id：
  如果该 chunk 是某个旧 chunk 的更新版本，记录旧 chunk id。

is_active：
  当前 chunk 是否有效。
```

---

### 4.3 新增 `rag_item_chunk_versions`

用于成熟版真正增量复用 chunk，而不是复制所有 chunks。

```sql
CREATE TABLE IF NOT EXISTS rag_item_chunk_versions (
    item_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    chunk_id TEXT NOT NULL,
    chunk_order INTEGER NOT NULL,
    anchor_id TEXT,
    is_reused INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(item_id, version, chunk_id)
);
```

作用：

```text
一个 chunk 内容只存一份。
不同版本通过 mapping 表引用 chunks。
```

示例：

```text
version 1: chunk_1, chunk_2, chunk_3
version 2: chunk_1, chunk_2_new, chunk_3
```

其中：

```text
chunk_1 / chunk_3 复用旧内容
chunk_2_new 是变化后的新 chunk
```

---

### 4.4 新增 `rag_reindex_diffs`

记录每次 reindex 的差异。

```sql
CREATE TABLE IF NOT EXISTS rag_reindex_diffs (
    diff_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    old_version INTEGER NOT NULL,
    new_version INTEGER NOT NULL,

    mode TEXT NOT NULL,             -- full | incremental | dry_run
    source_type TEXT NOT NULL,      -- document | code | log

    added_chunks INTEGER DEFAULT 0,
    updated_chunks INTEGER DEFAULT 0,
    deleted_chunks INTEGER DEFAULT 0,
    reused_chunks INTEGER DEFAULT 0,
    total_old_chunks INTEGER DEFAULT 0,
    total_new_chunks INTEGER DEFAULT 0,

    change_ratio REAL DEFAULT 0.0,
    fallback_reason TEXT,

    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    status TEXT NOT NULL,           -- pending | completed | failed | abandoned
    error TEXT,

    metadata_json TEXT
);
```

---

### 4.5 新增 `rag_reindex_diff_chunks`

记录每个 chunk 的具体变化。

```sql
CREATE TABLE IF NOT EXISTS rag_reindex_diff_chunks (
    diff_id TEXT NOT NULL,
    anchor_id TEXT,
    old_chunk_id TEXT,
    new_chunk_id TEXT,
    change_type TEXT NOT NULL,      -- added | updated | deleted | reused
    old_chunk_hash TEXT,
    new_chunk_hash TEXT,
    old_start_line INTEGER,
    old_end_line INTEGER,
    new_start_line INTEGER,
    new_end_line INTEGER,
    metadata_json TEXT,
    PRIMARY KEY(diff_id, anchor_id, change_type)
);
```

---

## 5. Anchor 生成策略

### 5.1 Document Anchor

适用于：

```text
Markdown
txt
rst
html
json
yaml
```

生成方式：

```text
anchor_id = hash(
    normalized_section_path
    + normalized_heading
    + normalized_first_sentence_or_paragraph
)
```

示例：

```text
section_path: "Phase 8 / M3 / Reindex"
heading: "M3 — Active Context + Lifecycle + Reindex"
first_sentence: "目标：active_context 概念落地..."
```

生成：

```text
doc:phase-8/m3/reindex:7f3a...
```

如果没有标题：

```text
anchor_id = hash(normalized_first_120_chars)
```

### 5.2 Code Anchor

适用于：

```text
.py / .js / .ts / .java / .go / .cpp / .rs / .sh
```

生成方式：

```text
anchor_id = hash(file_path + symbol_type + symbol_name + parent_symbol)
```

示例：

```text
mini_claw/gateway/router.py::class:Gateway
mini_claw/gateway/router.py::method:Gateway.handle_message
mini_claw/permissions/gate.py::function:evaluate
```

第一版：

```text
正则识别 class / def / function / method
```

成熟增强：

```text
Python AST
Tree-sitter
imports / call graph metadata
```

### 5.3 Log Anchor

日志通常 append-only。

生成方式：

```text
anchor_id = hash(file_path + offset_range + timestamp_range + first_error_line)
```

如果日志有时间戳：

```text
anchor_id = hash(timestamp + log_level + normalized_message_prefix)
```

如果没有时间戳：

```text
anchor_id = hash(byte_offset + first_line_hash)
```

---

## 6. 增量 Diff 算法

### 6.1 输入

```text
old_chunks:
  来自 rag_chunks + rag_item_chunk_versions
  version = rag_items.active_version

new_chunks:
  重新 chunk 当前文件得到
  尚未正式写入 active version
```

每个 chunk 必须包含：

```text
anchor_id
chunk_hash
start_line
end_line
section_title / symbol_name
content
metadata
```

### 6.2 匹配流程

```text
1. old_by_anchor = {old.anchor_id: old_chunk}
2. new_by_anchor = {new.anchor_id: new_chunk}

3. 对每个 new_chunk：
   如果 anchor_id 在 old_by_anchor：
      如果 chunk_hash 相同 → reused
      如果 chunk_hash 不同 → updated
   如果 anchor_id 不在 old_by_anchor：
      added

4. 对每个 old_chunk：
   如果 anchor_id 不在 new_by_anchor：
      deleted
```

### 6.3 fuzzy anchor fallback

有些情况下标题没变，但首句变了，anchor_id 会变。

为避免误判，可以加二级匹配：

```text
如果 anchor_id 不匹配：
  尝试用 section_title / symbol_name / line proximity 做 fuzzy match
```

匹配策略：

```text
document:
  section_title 相同
  chunk 内容相似度 > 0.75

code:
  symbol_name 相同
  file_path 相同

log:
  timestamp range 接近
  error signature 相同
```

匹配成功后：

```text
视为 updated，而不是 deleted + added
```

---

## 7. 增量 Reindex 流程

### 7.1 dry-run 流程

命令：

```text
/context reindex ctx_xxx --incremental --dry-run
```

流程：

```text
1. 检查 context_id 可见性
2. 检查源文件是否存在
3. 读取当前文件
4. 重新 chunk
5. 计算 diff
6. 不写 rag_chunks
7. 写或返回 dry_run diff
8. 返回用户：
   - added
   - updated
   - deleted
   - reused
   - change_ratio
   - 推荐 incremental 或 full
```

用户看到：

```text
检测到文档变化：
- 新增 chunks: 2
- 更新 chunks: 1
- 删除 chunks: 0
- 复用 chunks: 84
- 变化比例: 3.4%

建议执行增量重索引：
/context reindex ctx_xxx --incremental
```

---

### 7.2 incremental reindex 正式流程

```text
1. PermissionGate.evaluate(tool='reindex_context')
2. 检查 context ownership / workspace / session scope
3. 检查 source_path 是否存在
4. 检查敏感路径
5. 获取 RagIndexLock(item_id)
6. 读取 active_version = V
7. 读取 old active chunks
8. 重新 chunk 当前文件
9. 计算 diff
10. 判断是否超过 full_reindex_change_ratio
11. 如果没超过，进入 incremental
12. new_version = V + 1
13. 对 added / updated chunks：
    - 写 rag_chunks
    - 写 FTS5
    - 写 vector backend
14. 对 reused chunks：
    - 不写内容
    - 只写 rag_item_chunk_versions mapping
15. 对 deleted chunks：
    - 不加入 new_version mapping
    - 可保留旧 chunk 供旧版本 / audit 查看
16. 写 rag_reindex_diffs
17. 原子更新 rag_items.active_version = new_version
18. 更新 rag_items.content_hash
19. 更新 rag_items.last_reindex_diff_id
20. 写 security_audit
21. 释放 RagIndexLock
```

---

### 7.3 full reindex fallback

如果出现以下情况，自动退回 full reindex：

```text
change_ratio > full_reindex_change_ratio
chunker_version 改变
embedding_model 改变
source_type 改变
anchor 匹配失败率过高
旧 chunks 缺失 anchor_id
旧索引版本过旧
vector backend metadata 不一致
```

fallback 记录：

```text
rag_reindex_diffs.fallback_reason = "change_ratio_exceeded"
```

并提示用户：

```text
文档变化较大，增量更新不安全，已切换为全量重索引。
```

---

## 8. Vector Backend 增量更新

### 8.1 Chroma / Milvus / sqlite-vec 更新规则

对 changed chunks：

```text
added：
  embed new content
  upsert vector

updated：
  delete old vector
  embed new content
  upsert new vector

deleted：
  delete old vector 或标记 inactive

reused：
  不动 vector
```

### 8.2 vector_id 设计

禁止用：

```text
chunk_index
```

必须用稳定：

```text
chunk_id
```

因为 chunk_index 会随文档插入变化。

### 8.3 向量失败处理

如果 vector backend 更新失败：

```text
1. 不切换 active_version
2. 新写的 chunks 标记 abandoned
3. rag_reindex_diffs.status = failed
4. 写 audit vector_update_failed
5. search_context 继续使用旧 active_version
```

如果配置允许 fallback：

```text
rag.reindex.allow_fts_only_on_vector_failure = true
```

则：

```text
1. active_version 可切换
2. rag_embeddings 标记 embedding_pending
3. /rag status 显示 vector incomplete
4. 后台 reembed
```

默认建议：

```text
allow_fts_only_on_vector_failure = false
```

因为 mature 实现更强调一致性。

---

## 9. FTS5 增量更新

### 9.1 added / updated chunks

写入：

```text
rag_chunks
rag_chunks_fts
rag_item_chunk_versions
```

### 9.2 deleted chunks

从 new_version mapping 中排除。

旧 FTS row 可以选择：

```text
方案 A：保留旧 FTS row，但 search_context join active_version mapping 过滤；
方案 B：删除旧 FTS row。
```

成熟版建议方案 A：

```text
保留旧 FTS row，靠 active_version mapping 过滤。
```

好处：

```text
旧版本可追溯
删除风险低
原子切换更简单
```

后续 cleanup 再删除旧版本无用 chunks。

---

## 10. 并发与锁

### 10.1 锁粒度

新增：

```text
RagIndexLock(item_id)
```

规则：

```text
同一个 item_id 的 reindex 串行
不同 item_id 可并行
search_context 不阻塞 reindex
search_context 永远读取 active_version
```

### 10.2 和 workspace lock 的关系

`reindex_context` 需要：

```text
workspace read lock：
  读取源文件，避免边读边写

RagIndexLock：
  写 RAG store 和 vector backend
```

不需要长时间持有：

```text
workspace write lock
```

除非 reindex 同时修改源文件；正常 reindex 不修改源文件。

### 10.3 并发查询一致性

保证：

```text
reindex 期间，search_context 继续读旧 active_version。
reindex 成功后，search_context 读新 active_version。
reindex 失败后，search_context 仍读旧 active_version。
```

禁止：

```text
search_context 返回新旧混合 chunks
```

---

## 11. Lifecycle 接入

### 11.1 stale 检测

每次使用 active_context 前：

```text
1. 检查 source_path 是否存在
2. 计算当前 source_hash
3. 比较 rag_items.content_hash
4. 如果不同，status = stale
```

### 11.2 stale 后行为

默认：

```text
不使用旧 chunks 回答
提示用户 reindex
```

如果开启：

```yaml
rag:
  reindex:
    auto_incremental_on_stale: true
```

则：

```text
自动执行 dry-run
如果变化比例低于阈值，则自动增量 reindex
否则提示用户 full reindex
```

默认配置：

```text
auto_incremental_on_stale = false
```

### 11.3 orphan / moved

```text
source_path 不存在 → orphan
/context rebind <id> <new_path> → 检查 hash
hash 相同 → 更新 source_path
hash 不同 → 提示 reindex
```

---

## 12. 权限与安全

### 12.1 权限等级

| 工具                              | 等级 |
| ------------------------------- | -- |
| `diff_context`                  | L1 |
| `reindex_context --dry-run`     | L1 |
| `reindex_context --incremental` | L2 |
| `reindex_context --full`        | L2 |
| `reindex_sensitive_context`     | L4 |
| `cleanup_abandoned_reindex`     | L2 |

### 12.2 ChainDetector 接入

新增危险动作：

```text
rag_reindex_sensitive_context
rag_reindex_after_sensitive_search
rag_reindex_then_export
```

规则：

```text
如果用户先 search_context 查敏感内容，
随后 reindex 敏感 context，
再 write_file / run_shell 外发，
触发 deny 或 L4 审批。
```

### 12.3 Audit 事件

新增：

```text
rag_reindex_attempt
rag_reindex_dry_run_completed
rag_reindex_incremental_completed
rag_reindex_full_completed
rag_reindex_failed
rag_reindex_fallback_to_full
rag_reindex_vector_failed
rag_reindex_abandoned_cleanup
```

每个 audit 记录：

```text
item_id
old_version
new_version
mode
added_chunks
updated_chunks
deleted_chunks
reused_chunks
change_ratio
fallback_reason
agent_id
session_id
workspace_dir
source_path
```

---

## 13. 配置设计

```yaml
rag:
  reindex:
    incremental_enabled: true
    default_mode: incremental       # incremental | full
    full_reindex_change_ratio: 0.3
    fuzzy_anchor_match: true
    fuzzy_similarity_threshold: 0.75

    auto_incremental_on_stale: false
    allow_fts_only_on_vector_failure: false

    preserve_old_versions: true
    cleanup_old_versions_after_days: 30
    max_preserved_versions: 5

    require_approval_for_sensitive_reindex: true
```

---

## 14. 用户体验设计

### 14.1 stale 文档提示

用户问 stale 文档时：

```text
这个文档在上次索引后已经被修改。
为了避免使用旧索引误答，当前不会直接使用旧 RAG chunks。

检测建议：
/context reindex ctx_xxx --incremental --dry-run
```

### 14.2 dry-run 返回

```text
增量重索引预检查完成：

context_id: ctx_xxx
source: docs/RAG.md
old_version: 4
recommended_mode: incremental

变化统计：
- 新增 chunks: 2
- 更新 chunks: 1
- 删除 chunks: 0
- 复用 chunks: 84
- 变化比例: 3.4%

建议执行：
/context reindex ctx_xxx --incremental
```

### 14.3 incremental 完成返回

```text
增量重索引完成：

context_id: ctx_xxx
new_version: 5

更新结果：
- 新增 chunks: 2
- 更新 chunks: 1
- 删除 chunks: 0
- 复用 chunks: 84
- FTS 更新: ok
- Vector 更新: ok
- active_version 已切换到 5
```

### 14.4 fallback full reindex 返回

```text
文档变化比例为 46.2%，超过阈值 30%。
增量更新可能不安全，已切换为全量重索引。
```

---

## 15. `/rag status` 增强

新增展示：

```text
Reindex:
  last diff:
    item_id: ctx_xxx
    mode: incremental
    added: 2
    updated: 1
    deleted: 0
    reused: 84
    change_ratio: 3.4%
  abandoned versions: 0
  stale items: 2
  orphan items: 1
  embedding pending: 0
```

新增 CLI：

```bash
mini-claw rag status --reindex
mini-claw rag diff <context_id>
mini-claw rag cleanup-abandoned
```

---

## 16. 实施 Milestone

### M3.5.1 — Schema + Models

新增字段和表：

```text
rag_chunks.anchor_id
rag_chunks.chunk_hash
rag_chunks.previous_chunk_id
rag_item_chunk_versions
rag_reindex_diffs
rag_reindex_diff_chunks
```

新增模型：

```text
RagChunkFingerprint
RagReindexDiff
RagReindexDiffChunk
RagIncrementalPlan
```

测试：

```text
test_rag_incremental_schema.py
```

---

### M3.5.2 — Anchor 生成器

新增：

```text
mini_claw/rag/anchors.py
```

实现：

```text
DocumentAnchorGenerator
CodeAnchorGenerator
LogAnchorGenerator
```

测试：

```text
test_rag_anchor_document.py
test_rag_anchor_code.py
test_rag_anchor_log.py
```

---

### M3.5.3 — Diff Engine

新增：

```text
mini_claw/rag/diff.py
```

实现：

```text
compute_chunk_diff(old_chunks, new_chunks)
fuzzy_match_anchor()
calculate_change_ratio()
recommend_reindex_mode()
```

测试：

```text
test_rag_diff_added_updated_deleted_reused.py
test_rag_diff_fuzzy_match.py
test_rag_diff_full_reindex_threshold.py
```

---

### M3.5.4 — Incremental Reindex Runner

新增：

```text
mini_claw/rag/incremental_reindex.py
```

实现：

```text
dry_run_incremental_reindex()
run_incremental_reindex()
fallback_to_full_reindex()
rollback_failed_reindex()
```

测试：

```text
test_rag_incremental_reindex_success.py
test_rag_incremental_reindex_failure_rollback.py
test_rag_incremental_reindex_active_version.py
```

---

### M3.5.5 — FTS / Vector Delta Update

修改：

```text
mini_claw/rag/store.py
mini_claw/rag/vector_backend.py
mini_claw/rag/hybrid_retriever.py
```

实现：

```text
upsert_changed_chunks()
delete_removed_vectors()
reuse_unchanged_vectors()
mark_embedding_pending()
```

测试：

```text
test_rag_delta_fts.py
test_rag_delta_vector_chroma.py
test_rag_delta_vector_failure.py
```

---

### M3.5.6 — Commands + Health + Audit

新增命令：

```text
/context reindex --incremental
/context reindex --full
/context reindex --dry-run
/context diff
```

新增 audit：

```text
rag_reindex_incremental_completed
rag_reindex_fallback_to_full
rag_reindex_failed
```

测试：

```text
test_rag_reindex_commands.py
test_rag_reindex_audit.py
test_rag_status_reindex.py
```

---

## 17. 测试总表

必须新增：

```text
test_rag_incremental_schema.py
test_rag_anchor_document.py
test_rag_anchor_code.py
test_rag_anchor_log.py
test_rag_diff_added_updated_deleted_reused.py
test_rag_diff_fuzzy_match.py
test_rag_diff_full_reindex_threshold.py
test_rag_incremental_reindex_success.py
test_rag_incremental_reindex_failure_rollback.py
test_rag_incremental_reindex_active_version.py
test_rag_delta_fts.py
test_rag_delta_vector_chroma.py
test_rag_delta_vector_failure.py
test_rag_reindex_commands.py
test_rag_reindex_audit.py
test_rag_status_reindex.py
```

验收命令：

```bash
pytest tests/test_rag_incremental_*.py -v
pytest tests/test_rag_diff_*.py -v
pytest tests/test_rag_delta_*.py -v
pytest tests/ -q
mini-claw rag status --reindex
```

---

## 18. 验收标准

完成后必须满足：

```text
1. 文档小改动时，只更新 changed chunks。
2. 代码函数小改动时，只更新对应 symbol chunk。
3. log append 时，只索引新增部分。
4. chunk_index 变化不会导致整篇误判为 changed。
5. reindex 期间 search_context 只返回旧 active_version。
6. reindex 成功后 search_context 返回新 active_version。
7. reindex 失败后 active_version 不变。
8. FTS 只更新 added / updated chunks。
9. Vector backend 只更新 added / updated / deleted chunks。
10. unchanged chunks 不重新 embedding。
11. change_ratio 超过阈值自动 full reindex。
12. chunker_version 改变自动 full reindex。
13. embedding_model 改变自动 full reindex 或 full reembed。
14. rag_reindex_diffs 能记录本次变化统计。
15. /rag status 能展示 reindex 健康状态。
16. 所有 reindex 操作写 security_audit。
```

---


## 20. 最终结论

成熟 RAG 不能只有：

```text
文件变了 → stale → 全量 reindex
```

而应该支持：

```text
文件变了
↓
重新 chunk
↓
anchor 匹配
↓
chunk_hash 对比
↓
added / updated / deleted / reused diff
↓
只更新变化 chunks
↓
FTS / vector delta update
↓
active_version 原子切换
```

一句话总结：

> Incremental Reindex 的核心是：用稳定 anchor 找到“同一个语义块”，用 chunk_hash 判断它有没有变，用 version mapping 保证查询一致性，用 delta update 降低 FTS 和向量库更新成本。
