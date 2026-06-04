# Phase 8：成熟版 Context RAG + Memory RAG + Vector Retrieval 执行计划书

## 0. Phase 8 总目标

Phase 8 的目标是为 MiniClaw 增加一套完整、成熟、可长期运行的 RAG 系统，使 MiniClaw 能够处理以下场景：

1. 用户读取长文档，后续问题持续围绕该文档。
2. 用户读取长代码文件或整个代码目录，后续围绕代码结构、函数、模块提问。
3. 用户读取长日志、测试输出、traceback，后续围绕错误原因、失败位置、修复建议提问。
4. Agent 长期运行后，Session 历史、TaskState、workflow findings、项目规则越来越多，需要转成可检索长期记忆。
5. 不同 agent、不同 workspace、不同 channel 的 RAG 内容不能混淆。
6. RAG 内容必须有生命周期管理，不能永久堆积。
7. 索引、检索、记忆写入都必须纳入 MiniClaw 原有安全链路：PermissionGate、ApprovalStore、SecurityAuditLogger、Workspace lock、AgentManager、SessionManager。

Phase 8 的最终目标不是“接一个向量库”，而是实现：

> 一个受权限控制、可审计、可隔离、可过期、可检索、可升级向量后端的长期上下文系统。

---

## 1. 核心原则

### 1.1 Context RAG 和 Memory RAG 必须分开

RAG 分成两个逻辑 namespace：

```text
context namespace:
- 用户读取的文档
- 用户读取的代码
- 用户读取的日志
- 当前项目文件
- 当前 session active context

memory namespace:
- 用户长期偏好
- 项目长期规则
- 架构决策
- workflow key findings
- 旧 TaskState facts
- 旧 session summaries
- 稳定 bug pattern
```

核心区别：

```text
Context RAG = 用户给我的材料
Memory RAG  = 系统长期记住的经验和决策
```

禁止把文档 chunk、代码 chunk、日志 chunk、用户偏好、项目规则混在一个无类型向量库里。

### 1.2 读取不等于索引

必须明确：

```text
read_file 成功 ≠ 允许 index_context
bypass 下 read_file 成功 ≠ bypass 下允许 index_context
LLM 不能自己决定把某个文件永久索引
```

`read_file` 是临时读取，`index_context` 是长期写入，两者风险等级不同。

### 1.3 索引是写操作

`index_context` 会写入：

```text
rag_items
rag_chunks
rag_chunks_fts
rag_embeddings
active_contexts
security_audit
```

因此索引操作必须被视为写操作。

要求：

```text
必须经过 PermissionGate
必须写 security_audit
必须记录 indexed_by_agent_id / indexed_by_chat_id / indexed_by_channel
必须记录 source_path / content_hash / workspace_dir
```

### 1.4 Memory 写入是高风险操作

Memory RAG 不是简单存聊天记录，而是让系统决定什么要长期记住。

它可能被 prompt injection 污染，因此必须更严格：

```text
memory_write = L3
MemoryConsolidator 输出必须过 PromptValidator
敏感信息必须过滤
越权规则必须拒绝
必须记录 source chain
必要时需要用户审批
```

### 1.5 RAG 必须有生命周期

所有 RAG item 都必须有状态：

```text
active
warm
archived
cold
stale
orphan
deleted
```

不能无限增长。

### 1.6 检索必须按 scope 过滤

任何检索都必须先过滤权限范围：

```text
agent_id
workspace_dir
session_id
channel_name
scope_type
scope_id
namespace
source_type
status
```

不能让 Agent A 搜到 Agent B 的私有 context。

### 1.7 Prompt 注入必须分区

检索结果注入 prompt 时必须分开：

```text
[Retrieved Context]
文档 / 代码 / 日志证据

[Retrieved User Memory]
长期偏好 / 项目规则 / 架构决策
```

禁止混成一个”相关上下文”大段。

---

## 2. 总体架构

### 2.1 新增模块结构

```text
mini_claw/
├── rag/
│   ├── __init__.py
│   ├── models.py              # RagItem / RagChunk / RagSearchResult / ActiveContext
│   ├── store.py               # SQLite CRUD + FTS5 + embedding metadata
│   ├── chunker.py             # document/code/log chunking
│   ├── indexer.py             # index_file / index_directory / index_text / reindex
│   ├── retriever.py           # FTS / vector / hybrid retrieval
│   ├── lifecycle.py           # active/warm/archived/cold/stale/orphan/deleted
│   ├── manager.py             # RagManager 统一门面
│   ├── permissions.py         # RAG 权限辅助
│   ├── redaction.py           # secret redaction / sensitive checks
│   ├── query_router.py        # 判断搜 context 还是 memory
│   ├── injector.py            # prompt 注入构造
│   ├── embeddings.py          # embedding provider 抽象
│   ├── vector_backend.py      # Chroma / Milvus / sqlite-vec 抽象
│   └── memory/
│       ├── candidate.py       # MemoryCandidate
│       ├── extractor.py       # 从 session/task/workflow 提取候选记忆
│       ├── consolidator.py    # 改写成独立事实
│       ├── policy.py          # should_store_memory
│       ├── validator.py       # 越权/敏感/注入检查
│       └── store.py           # memory item lifecycle
```

### 2.2 新增工具

```text
Context RAG tools:
- index_context
- search_context
- list_contexts
- inspect_context
- clear_context
- archive_context
- delete_context
- reindex_context
- rebind_context

Memory RAG tools:
- memory_remember
- memory_search
- memory_list
- memory_inspect
- memory_delete
- memory_pin
- memory_unpin
- memory_compact_to_rag
```

### 2.3 新增命令

```text
/context index <path>
/context search <query>
/context list
/context use <context_id>
/context clear
/context inspect <context_id>
/context archive <context_id>
/context delete <context_id>
/context reindex <context_id>
/context rebind <context_id> <new_path>
/context cleanup

/memory remember <content>
/memory search <query>
/memory list
/memory inspect <memory_id>
/memory delete <memory_id>
/memory pin <memory_id>
/memory unpin <memory_id>
/memory compact_to_rag
```

---

## 3. 数据库设计

### 3.1 `rag_items`

统一保存 context 和 memory 的元数据。

```sql
CREATE TABLE IF NOT EXISTS rag_items (
    item_id TEXT PRIMARY KEY,

    namespace TEXT NOT NULL,        -- context | memory
    source_type TEXT NOT NULL,      -- document | code | log | user_preference | project_rule | architecture_decision | workflow_finding | task_fact | session_summary
    scope_type TEXT NOT NULL,       -- user | agent | workspace | session | document | codebase
    scope_id TEXT NOT NULL,

    owner_agent_id TEXT NOT NULL,
    session_id TEXT,
    chat_id TEXT,
    channel_name TEXT,
    workspace_dir TEXT,

    source_path TEXT,
    title TEXT,
    content_hash TEXT,

    status TEXT NOT NULL,           -- active | warm | archived | cold | stale | orphan | deleted
    importance INTEGER DEFAULT 3,
    pinned INTEGER DEFAULT 0,
    confidence REAL DEFAULT 1.0,

    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_accessed_at INTEGER,
    access_count INTEGER DEFAULT 0,
    expires_at INTEGER,

    indexed_by_agent_id TEXT,
    indexed_by_chat_id TEXT,
    indexed_by_channel TEXT,

    source_chain_json TEXT,
    metadata_json TEXT
);
```

### 3.2 `rag_chunks`

保存原文切片。

```sql
CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,

    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    token_count INTEGER,

    start_line INTEGER,
    end_line INTEGER,
    section_title TEXT,
    symbol_name TEXT,
    language TEXT,

    content_hash TEXT,
    metadata_json TEXT,

    FOREIGN KEY(item_id) REFERENCES rag_items(item_id)
);
```

### 3.3 `rag_chunks_fts`

第一层检索使用 SQLite FTS5。

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts
USING fts5(
    chunk_id,
    item_id,
    content,
    section_title,
    symbol_name,
    tokenize = 'unicode61'
);
```

### 3.4 `rag_embeddings`

保存 embedding 元数据。向量本体根据后端不同存储：

* Chroma：存 Chroma collection。
* Milvus：存 Milvus collection。
* sqlite-vec：存 SQLite vector table。
* none：不存向量。

```sql
CREATE TABLE IF NOT EXISTS rag_embeddings (
    chunk_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    backend TEXT NOT NULL,          -- chroma | milvus | sqlite_vec
    collection_name TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    dim INTEGER,
    vector_id TEXT,
    created_at INTEGER NOT NULL,
    metadata_json TEXT
);
```

### 3.5 `active_contexts`

保存当前 session 正在围绕哪个材料提问。

```sql
CREATE TABLE IF NOT EXISTS active_contexts (
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    context_type TEXT NOT NULL,     -- document | code | log | codebase
    title TEXT,
    activated_at INTEGER NOT NULL,
    expires_at INTEGER,
    PRIMARY KEY(session_id, agent_id, context_id)
);
```

### 3.6 `memory_candidates`

保存待审批或待确认的长期记忆候选。

```sql
CREATE TABLE IF NOT EXISTS memory_candidates (
    candidate_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,

    source_type TEXT NOT NULL,      -- explicit | compaction | task_state | workflow
    source_chain_json TEXT,

    stability INTEGER,
    reuse_value INTEGER,
    sensitivity INTEGER,
    confidence REAL,

    status TEXT NOT NULL,           -- pending | approved | rejected | stored
    approval_id TEXT,

    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    metadata_json TEXT
);
```

### 3.7 索引

```sql
CREATE INDEX IF NOT EXISTS idx_rag_items_owner
ON rag_items(owner_agent_id, namespace, status);

CREATE INDEX IF NOT EXISTS idx_rag_items_scope
ON rag_items(scope_type, scope_id, namespace, status);

CREATE INDEX IF NOT EXISTS idx_rag_items_source
ON rag_items(source_path, content_hash);

CREATE INDEX IF NOT EXISTS idx_rag_items_workspace
ON rag_items(workspace_dir, namespace, status);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_item
ON rag_chunks(item_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_active_contexts_session
ON active_contexts(session_id, agent_id);
```

---

## 4. 配置设计

```yaml
rag:
  enabled: true

  namespaces:
    context_enabled: true
    memory_enabled: true

  backend:
    text_search: fts5
    vector_backend: chroma        # none | chroma | milvus | sqlite_vec
    hybrid_enabled: true

  fts:
    enabled: true
    top_k: 8

  embedding:
    enabled: true
    provider: local               # local | openai | custom
    model: sentence-transformers/all-MiniLM-L6-v2
    dim: 384
    batch_size: 32

  chroma:
    persist_dir: ./data/chroma
    collection_prefix: miniclaw

  milvus:
    enabled: false
    uri: http://127.0.0.1:19530
    collection_prefix: miniclaw

  chunk:
    max_tokens: 800
    overlap_tokens: 100
    max_file_size_mb: 20
    binary_file_policy: deny

  security:
    allow_index_in_bypass: false
    allow_sensitive_index: false
    require_approval_for_index: false
    require_approval_for_sensitive_index: true
    require_approval_for_memory_write: true

  sharing:
    allow_workspace_context_sharing: false
    allow_cross_agent_context: false

  retrieval:
    auto_context_retrieval: true
    auto_memory_retrieval: true
    context_top_k: 6
    memory_top_k: 3
    min_memory_confidence: 0.75
    include_archived_by_default: false

  lifecycle:
    warm_after_days: 7
    archive_after_days: 30
    cold_after_days: 90
    delete_after_days: 180
    log_ttl_days: 7
    keep_tombstone: true
```

---

## 5. 权限模型

### 5.1 工具权限等级

| 工具                        | 权限等级 | 说明                                |
| ------------------------- | ---- | --------------------------------- |
| `search_context`          | L1   | 检索当前 agent/session/workspace 可见材料 |
| `list_contexts`           | L1   | 查看 context 列表                     |
| `inspect_context`         | L1   | 查看 context 元数据和脱敏片段               |
| `index_context`           | L2   | 写入文档/代码/log 索引                    |
| `archive_context`         | L2   | 归档 context                        |
| `clear_context`           | L2   | 清除 active context                 |
| `reindex_context`         | L2   | 重新索引                              |
| `rebind_context`          | L2   | 重新绑定 path                         |
| `delete_context`          | L3   | 删除 chunks / embeddings            |
| `index_sensitive_context` | L4   | 默认拒绝                              |
| `memory_search`           | L1   | 搜长期记忆                             |
| `memory_list`             | L1   | 列出长期记忆                            |
| `memory_remember`         | L3   | 写长期记忆                             |
| `memory_delete`           | L3   | 删除长期记忆                            |
| `memory_compact_to_rag`   | L3   | 从 TaskState/session 批量生成长期记忆      |

### 5.2 `index_context` 权限管道

```text
1. Gateway/AgentLoop 收到 index_context tool call
2. PermissionGate.evaluate(tool="index_context", args)
3. 检查 path 是否在 workspace 内
4. 检查 sensitive path
5. 检查 bypass 配置
6. 检查文件大小、类型、是否 binary
7. 需要审批则写 ApprovalStore
8. 允许后写 security_audit: rag_index_attempt
9. RagIndexer 执行索引
10. 写 security_audit: rag_index_completed / rag_index_failed
```

### 5.3 `search_context` 权限管道

```text
1. PermissionGate.evaluate(tool="search_context", args)
2. RagRetriever 根据 agent_id/session_id/workspace_dir 过滤
3. 只检索 namespace=context
4. 默认只检索 active/warm
5. 不跨 agent
6. 不检索 stale/orphan/deleted
7. 返回带 source_path / line range / chunk_id 的结果
```

### 5.4 `memory_remember` 权限管道

```text
1. PermissionGate.evaluate(tool="memory_remember", args)
2. MemoryCandidate 生成
3. MemoryConsolidator 改写成独立事实
4. MemoryValidator 检查敏感、越权、prompt injection
5. 需要审批则写 ApprovalStore
6. 批准后写 rag_items(namespace=memory)
7. 写 security_audit: memory_write_completed
```

---

## 6. Context RAG 成熟实现

### 6.1 支持 source_type

```text
document
code
codebase
log
shell_output
test_output
workflow_artifact
```

### 6.2 Document Chunker

支持：

```text
.md
.txt
.rst
.html
.json
.yaml
.yml
```

规则：

```text
优先按标题切
其次按段落切
最后按 token 上限切
保留 start_line/end_line/section_title
```

### 6.3 Code Chunker

支持：

```text
.py
.js
.ts
.java
.go
.cpp
.c
.rs
.sh
```

第一版规则：

```text
按 def/class/function/method 边界切
无法识别时按固定行数 + token 上限切
保留 symbol_name/language/start_line/end_line
```

后续增强：

```text
Python AST
Tree-sitter
imports/calls metadata
symbol graph
```

### 6.4 Log Chunker

支持：

```text
.log
.txt
pytest output
traceback
shell output
```

规则：

```text
按 traceback 块切
按 ERROR/WARN 块切
按时间戳区间切
保留 log_level/timestamp_range
```

### 6.5 Indexer

`RagIndexer.index_path(path, context)` 流程：

```text
1. 判断文件/目录
2. 权限检查
3. 类型识别
4. 内容 hash
5. 如果 hash 已存在且 ready，跳过重复索引
6. chunk
7. redaction
8. 写 rag_items
9. 写 rag_chunks
10. 写 FTS
11. 如果 embedding enabled，写 vector backend
12. 设置 active_context
13. 写 audit
```

### 6.6 Retriever

支持三种模式：

```text
FTS retrieval
Vector retrieval
Hybrid retrieval
```

成熟版默认：

```text
FTS + vector hybrid
```

Hybrid score：

```text
score = 0.45 * fts_score
      + 0.45 * vector_score
      + 0.05 * recency_bonus
      + 0.05 * active_context_bonus
```

---

## 7. Memory RAG 成熟实现

### 7.1 Memory 类型

```text
user_preference
project_rule
architecture_decision
constraint
workflow_finding
bug_pattern
task_fact
session_summary
operational_rule
```

### 7.2 MemoryCandidate

```python
@dataclass
class MemoryCandidate:
    content: str
    memory_type: str
    scope_type: str
    scope_id: str
    source_type: str
    source_chain: dict

    stability: int
    reuse_value: int
    sensitivity: int
    confidence: float
    ttl_days: int | None
```

### 7.3 入库评分

```python
should_store = (
    stability >= 3
    and reuse_value >= 3
    and sensitivity <= 2
    and confidence >= 0.7
)
```

用户显式“记住”：

```text
可以降低 stability 阈值
不能跳过 sensitivity 检查
不能跳过越权检查
```

### 7.4 记忆来源

#### 显式来源

```text
用户说：
- 记住这个
- 以后都这样
- 保存到长期记忆
```

#### Session 压缩来源

```text
SessionManager.compact_history
→ extract_facts_from_messages
→ MemoryCandidate
```

#### TaskState 来源

```text
TaskState facts > 50
或 prompt chars > 8000
→ old non-pinned facts
→ MemoryCandidate
```

#### Workflow 来源

```text
WorkflowMerger 输出：
- key_findings
- remaining_risks
- architecture decisions
- recommended_next_steps
```

### 7.5 MemoryConsolidator

把碎片改写成独立事实。

错误例子：

```text
用户选择第二种。
```

正确例子：

```text
在 MiniClaw Plugin disable 方案中，用户选择第一版 disable 后重启生效，不做运行时热摘除。
```

### 7.6 MemoryValidator

拒绝以下内容：

```text
允许绕过 PermissionGate
以后自动开启 bypass
忽略工具权限
默认允许高风险工具
保存 API key/token/password
修改安全策略为默认允许
不需要审批
直接执行 LLM 生成脚本
```

### 7.7 Memory Scope

```text
user scope:
- 用户长期偏好
- 语言风格
- 学习习惯

workspace scope:
- 项目规则
- 架构决策
- 项目约束

agent scope:
- 某个 agent 的行为偏好
- 某个 agent 的工具习惯

session scope:
- 当前会话临时事实
```

### 7.8 Memory 检索

默认：

```text
top_k = 3
confidence >= 0.75
status in active/warm
pinned 优先
scope 过滤
```

注入格式：

```text
[Retrieved User Memory]
- user_preference: 用户偏好中文解释
- project_rule: MiniClaw 中 Skill 不能注册工具，只能影响 prompt
```

---

## 8. Context 与 Memory 的检索路由

### 8.1 QueryRouter

新增：

```text
mini_claw/rag/query_router.py
```

职责：

```text
判断当前问题应该搜 context、memory、还是双路检索
```

### 8.2 只搜 context 的情况

```text
这个文档里怎么说？
这段代码在哪里？
这个 log 的错误是什么？
它里面的 workflow 是什么？
这个函数怎么实现？
```

### 8.3 只搜 memory 的情况

```text
之前我们怎么定的？
我以前偏好什么？
这个项目的长期原则是什么？
之前为什么不用那个方案？
```

### 8.4 双路检索的情况

```text
结合这个文档和我们之前的设计原则评价一下
按照之前的项目规则检查这份代码
根据我之前的偏好总结这个文档
```

### 8.5 注入格式

```text
[Retrieved Context]
source: docs/LEARNING.md lines 120-180
content: ...

[Retrieved User Memory]
type: project_rule
content: Workflow 不能绕过 PermissionGate。
```

---

## 9. Active Context 设计

### 9.1 设置 active_context

触发：

```text
/context use <context_id>
/context index <path>
用户明确说“后续围绕这个文档”
```

### 9.2 active_context 只指向 context namespace

```text
active_context 不能指向 memory item
```

### 9.3 自动解析引用

如果用户说：

```text
它里面
这个文档
这段代码
上面那个 log
```

系统优先解析为 active_context。

### 9.4 多 active_context

允许多个 active context，但必须排序：

```text
most_recent
pinned
explicitly_used
```

如果歧义太大，系统应提示用户选择 context。

---

## 10. 生命周期管理

### 10.1 状态

```text
active：当前正在使用
warm：最近用过
archived：项目结束或暂时不用
cold：长期不用，只保留摘要和 metadata
stale：源文件内容变更
orphan：源文件删除
deleted：chunks/embeddings 已删除
```

### 10.2 自动转移

```text
active 7 天未访问 → warm
warm 30 天未访问 → archived
archived 90 天未访问 → cold
cold 180 天未访问 → deleted
log 类型 7 天未访问 → deleted
pinned 永不自动删除
```

### 10.3 文件变更处理

```text
文件内容变更：
content_hash 不一致 → stale

文件删除：
source_path 不存在 → orphan

文件移动：
/context rebind <id> <new_path>
hash 相同则更新 path
hash 不同则提示 reindex
```

### 10.4 删除策略

默认删除：

```text
删除 rag_chunks
删除 FTS 记录
删除 vector backend 向量
保留 rag_items tombstone
```

如果用户要求彻底删除：

```text
完全删除 rag_items / chunks / embeddings / active_context
写 security_audit
```

---

## 11. Vector Backend 成熟设计

### 11.1 Backend 抽象

```python
class VectorBackend(Protocol):
    def upsert_chunks(self, chunks: list[RagChunk]) -> None: ...
    def search(self, query_embedding, filters, top_k: int) -> list[VectorHit]: ...
    def delete_chunks(self, chunk_ids: list[str]) -> None: ...
    def delete_item(self, item_id: str) -> None: ...
    def health_check(self) -> VectorBackendHealth: ...
```

### 11.2 Backend 选择

成熟版支持：

```text
none
chroma
milvus
sqlite_vec
```

推荐默认：

```text
Chroma
```

原因：

```text
本地优先
低运维
Python 集成简单
适合个人 Agent Gateway
```

Milvus 作为后期大规模后端。

### 11.3 Collection 设计

Chroma collection：

```text
miniclaw_context_document
miniclaw_context_code
miniclaw_context_log
miniclaw_memory_user
miniclaw_memory_workspace
miniclaw_memory_agent
```

不要把 context 和 memory 放同一个 collection。

### 11.4 EmbeddingProvider

```python
class EmbeddingProvider(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, query: str) -> list[float]: ...
```

支持：

```text
local sentence-transformers
OpenAI embeddings
custom HTTP endpoint
```

默认：

```text
local sentence-transformers/all-MiniLM-L6-v2
```

---

## 12. Security Audit 事件

新增事件：

```text
rag_index_attempt
rag_index_completed
rag_index_failed
rag_search_performed
rag_context_activated
rag_context_archived
rag_context_deleted
rag_context_stale
rag_context_orphan
memory_candidate_created
memory_write_approval_required
memory_write_completed
memory_write_rejected
memory_search_performed
rag_lifecycle_cleanup
vector_backend_error
```

每个事件记录：

```text
event_type
debug_id
agent_id
chat_id
channel_name
workspace_dir
item_id
source_path
namespace
source_type
scope_type
scope_id
details_json
created_at
```

---

## 13. Workflow 接入

Workflow node 的 prompt 应提示：

```text
如果当前任务涉及长文档/代码库，请优先调用 search_context 获取相关片段。
不要直接 read_file 大文件。
如果 search_context 结果不足，再 read_file 精读小范围文件。
```

WorkflowPlanner 可在以下模板中自动加入 `search_context`：

```text
code_review
debug_fix
migration
security_review
architecture_review
```

PromptCompiler 工具交集规则也必须包含 RAG 工具：

```text
effective_tools = node.tools ∩ agent_cfg.tools ∩ role_profile.default_tools
```

如果 role 是 `researcher/security_reviewer/test_reviewer`，默认可用：

```text
search_context
read_file
list_directory
```

---

## 14. ResultProcessor 接入

当 `read_file` 返回超长内容时：

```text
不自动索引敏感文件
不在 bypass 下自动索引
不由 LLM 决定自动索引
```

处理策略：

```text
1. 如果文件超长，ResultProcessor 返回 index suggestion：
   “文件较长，可使用 /context index <path> 建立检索索引。”

2. 如果配置 allow_auto_index=true，且文件非敏感、在 workspace 内、工具权限允许：
   自动调用 index_context，并写 audit。

3. 默认 allow_auto_index=false。
```

配置：

```yaml
rag:
  auto_index:
    enabled: false
    max_file_size_mb: 5
    require_non_sensitive: true
```

---

## 15. 成熟实现测试计划

### 15.1 权限测试

```text
test_rag_permissions.py
- read_file allowed 不代表 index_context allowed
- bypass read 不代表 bypass index
- sensitive file index denied
- index_context 写 audit
- delete_context 是 L3
- memory_remember 是 L3
```

### 15.2 Context Index 测试

```text
test_context_indexer.py
- markdown chunk
- code chunk
- log chunk
- binary deny
- content_hash
- duplicate indexing skip
- source_path metadata
```

### 15.3 Retrieval 测试

```text
test_context_retriever.py
- FTS search
- vector search
- hybrid search
- active_context boost
- archived 默认过滤
- stale/orphan/deleted 过滤
```

### 15.4 Agent 隔离测试

```text
test_rag_agent_isolation.py
- Agent A 不能搜 Agent B context
- workspace sharing 默认关闭
- workspace sharing 开启后同 workspace 可读
- session scope 只在当前 session 可见
```

### 15.5 Lifecycle 测试

```text
test_rag_lifecycle.py
- active → warm
- warm → archived
- archived → cold
- cold → deleted
- pinned 不自动删除
- log TTL 删除
```

### 15.6 Stale/Orphan/Rebind 测试

```text
test_rag_stale_orphan.py
- 文件内容变更标 stale
- 文件删除标 orphan
- 文件移动 rebind
- hash 不同要求 reindex
```

### 15.7 Memory Candidate 测试

```text
test_memory_candidate.py
- 稳定性评分
- 复用价值评分
- 敏感性拒绝
- confidence 阈值
- 用户显式记忆仍检查敏感
```

### 15.8 Memory Consolidator 测试

```text
test_memory_consolidator.py
- 模糊对话改写为独立事实
- 越权语句拒绝
- source_chain 记录
- prompt injection 拒绝
```

### 15.9 Prompt 注入测试

```text
test_rag_injector.py
- Retrieved Context 独立 section
- Retrieved Memory 独立 section
- 双路检索不混
- top_k 限制
```

### 15.10 Vector Backend 测试

```text
test_vector_backend_chroma.py
- Chroma init
- collection 分离
- upsert/search/delete
- health check
- fallback to FTS
```

---

## 16. 实施顺序

### Step 1：Schema + RagStore

实现：

```text
rag_items
rag_chunks
rag_chunks_fts
rag_embeddings
active_contexts
memory_candidates
```

### Step 2：Chunker

实现：

```text
DocumentChunker
CodeChunker
LogChunker
```

### Step 3：权限和审计

实现：

```text
RAG tool permission levels
PermissionGate 规则
AuditLogger 事件
agent/workspace/session scope filtering
```

### Step 4：Context Indexer

实现：

```text
index_context
index_file
index_directory
content_hash
redaction
FTS 写入
```

### Step 5：Context Retriever

实现：

```text
search_context
active_context boost
status filtering
source_type filtering
```

### Step 6：Tools + Commands

实现：

```text
/context index
/context search
/context list
/context use
/context inspect
/context clear
/context archive
/context delete
```

### Step 7：AgentLoop / ResultProcessor 接入

实现：

```text
active_context prompt hint
search_context tool schema
long read_file index suggestion
```

### Step 8：Lifecycle

实现：

```text
cleanup_expired
stale/orphan/rebind
active/warm/archived/cold/deleted
```

### Step 9：Memory RAG

实现：

```text
MemoryCandidate
MemoryExtractor
MemoryConsolidator
MemoryValidator
memory_remember
memory_search
memory_compact_to_rag
```

### Step 10：Vector Backend

实现：

```text
EmbeddingProvider
ChromaBackend
HybridRetriever
MilvusBackend optional
```

### Step 11：Workflow RAG Enhancement

实现：

```text
Workflow role profile 增加 search_context
PromptCompiler 提示优先 search_context
Workflow node result 引用 retrieved chunks
```

---

## 17. 验收标准

### 17.1 Context RAG 成熟验收

```text
1. 用户可以索引长文档、代码、日志。
2. 系统按 source_type 选择 chunker。
3. /context search 能返回带路径和行号的 chunk。
4. active_context 能让后续问题围绕当前文档检索。
5. index_context 是独立 L2 写操作。
6. read_file 成功不自动允许索引。
7. bypass read 不自动允许索引。
8. 敏感文件默认不能索引。
9. 索引和检索写 audit。
10. Agent A 默认不能检索 Agent B 的 context。
```

### 17.2 Lifecycle 成熟验收

```text
1. RAG item 有完整生命周期状态。
2. 文件变更标记 stale。
3. 文件删除标记 orphan。
4. 文件移动可 rebind。
5. archived 默认不检索。
6. cold 可删除 chunks 只保留 metadata。
7. pinned 不自动删除。
```

### 17.3 Memory RAG 成熟验收

```text
1. memory_remember 是 L3。
2. MemoryCandidate 有评分机制。
3. MemoryConsolidator 能把碎片改写成独立事实。
4. 越权规则不能写入 memory。
5. 敏感信息不能写入 memory。
6. 每条 memory 有 source chain。
7. Memory 检索和 Context 检索分离。
8. Prompt 注入分为 Retrieved Context 和 Retrieved Memory。
```

### 17.4 Vector Backend 成熟验收

```text
1. FTS5 可用。
2. Chroma backend 可用。
3. collection 按 namespace/source_type 分离。
4. Chroma 不可用时可 fallback 到 FTS。
5. embedding 可缓存。
6. delete_context 会同步删除 vector backend 中的向量。
```

---


## 19. 最终结论

Phase 8 的成熟设计必须包含：

```text
Context RAG
Code RAG
Log RAG
Memory RAG
Active Context
Lifecycle
PermissionGate integration
AuditLogger integration
Agent/workspace/session isolation
FTS retrieval
Vector backend
Hybrid retrieval
Workflow integration
Prompt injection separation
```

核心边界：

```text
Context RAG ≠ Memory RAG
读取 ≠ 索引
检索 ≠ 越权
记忆写入 ≠ 普通摘要
向量库 ≠ 安全边界
```

只有把这些全部做进去，MiniClaw 的 RAG 才是一个成熟的 Agent Runtime 子系统，而不是一个容易污染上下文的新漏洞。
