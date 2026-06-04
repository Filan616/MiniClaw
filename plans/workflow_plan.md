> **主 Agent 先判断任务是否复杂到需要工作流；如果需要，就临时生成一个 workflow plan，把任务拆成多个子任务，让多个 subagent 并行/串行执行，最后再汇总、验证、回写结果。**

Claude Code 官方现在确实有类似方向：它的 Dynamic Workflows 是让 Claude 写出可重跑的脚本，用来编排多个 subagents，适合代码库审计、大规模迁移、交叉验证研究等任务；Claude Code SDK 里的 subagents 也被定义为独立 agent 实例，可以隔离上下文、并行运行任务、使用专门指令。([Claude Code][1])

你的 MiniClaw 里可以这样做。

---

# 1. 先说人话：你的系统里应该加一个 Workflow Layer

现在 MiniClaw 的执行链路大概是：

```text
用户消息
↓
Gateway
↓
Agent Loop
↓
LLM 判断要不要调工具
↓
PermissionGate
↓
Tool Execute
↓
返回结果
```

要实现 Claude Code 那种“自动搭 workflow”，要变成：

```text
用户消息
↓
Gateway
↓
Workflow Decision
  ├── 简单任务：直接走原 Agent Loop
  └── 复杂任务：生成 WorkflowSpec
        ↓
     WorkflowRunner
        ├── 子 Agent A：代码阅读
        ├── 子 Agent B：测试分析
        ├── 子 Agent C：安全审计
        ├── 子 Agent D：修改实现
        └── 子 Agent E：验证总结
        ↓
     Merge / Review / Verify
        ↓
     主 Agent 输出最终答案
```

也就是说，你不是把原来的 Agent Loop 替换掉，而是在它外面加一层：

> **Workflow Orchestrator：判断是否需要工作流、生成工作流、执行工作流、合并结果。**

---

# 2. 这个能力应该放在你当前计划的哪个阶段？

你现在这份计划已经有：

* Phase 0：安全底座；
* Phase 1：AgentManager；
* Phase 2：ChannelManager；
* Phase 3：Skills；
* Phase 4：Plugin。

我建议新增：

```text
Phase 5：Dynamic Workflow Orchestrator
```

它应该放在 **Phase 1 AgentManager 之后**，因为 workflow 需要能临时创建/调用多个 subagent。

最低依赖：

```text
必须有：
- AgentManager
- ProviderManager
- PermissionGate
- ApprovalStore
- SecurityAuditLogger
- TaskState

最好有：
- Skills
- ChannelManager
- PluginManager
```

所以实际执行顺序建议是：

```text
Phase 0：安全底座
Phase 1：AgentManager
Phase 2：ChannelManager
Phase 3：Skills
Phase 5：Workflow Orchestrator
Phase 4：Plugin
```

Plugin 可以后置，因为 workflow 第一版不需要插件。

---

# 3. 核心设计：不要让 LLM 直接写 Python 脚本执行

Claude Code 的 Dynamic Workflows 是“Claude 写脚本来编排 subagents”，但你的 MiniClaw 第一版**不要直接让 LLM 生成 Python 脚本并执行**，因为这会把攻击面放大很多。

你应该让 LLM 生成一种受控的 JSON DSL：

```json
{
  "workflow_name": "codebase_review_and_fix",
  "reason": "任务需要先理解代码、再修改、再验证，适合拆成多个子任务",
  "execution_mode": "mixed",
  "nodes": [
    {
      "id": "scan",
      "type": "subagent",
      "agent_role": "code_reviewer",
      "task": "阅读项目结构，找出和用户问题相关的文件",
      "tools": ["read_file", "list_directory"],
      "depends_on": []
    },
    {
      "id": "plan",
      "type": "subagent",
      "agent_role": "planner",
      "task": "根据 scan 的结果制定修改计划，不实际修改文件",
      "tools": ["read_file"],
      "depends_on": ["scan"]
    },
    {
      "id": "implement",
      "type": "subagent",
      "agent_role": "implementer",
      "task": "根据 plan 修改代码",
      "tools": ["read_file", "write_file"],
      "depends_on": ["plan"]
    },
    {
      "id": "verify",
      "type": "subagent",
      "agent_role": "tester",
      "task": "运行相关测试，报告失败原因",
      "tools": ["run_shell", "read_file"],
      "depends_on": ["implement"]
    }
  ],
  "merge_strategy": "summarize_and_verify",
  "max_parallel": 3,
  "requires_approval": false
}
```

这样做的好处是：

```text
LLM 只负责规划
系统负责执行
权限仍然走 PermissionGate
工具仍然走 ToolRegistry
审计仍然走 SecurityAuditLogger
不会让模型随便生成脚本控制系统
```

这比直接执行 LLM 生成的 Python workflow 安全很多。

---

# 4. 需要新增哪些模块？

建议新增这些文件：

```text
mini_claw/workflow/
├── spec.py          # WorkflowSpec / WorkflowNode / WorkflowRun 数据结构
├── planner.py       # 判断是否需要 workflow，并生成 WorkflowSpec
├── runner.py        # 按 DAG 执行 workflow
├── scheduler.py     # 并发调度、依赖管理
├── merger.py        # 合并多个 subagent 结果
├── store.py         # workflow_runs / workflow_nodes 持久化
└── templates.py     # 常见 workflow 模板
```

---

# 5. 数据结构设计

## 5.1 WorkflowSpec

```python
@dataclass
class WorkflowSpec:
    name: str
    reason: str
    execution_mode: Literal["sequential", "parallel", "mixed"]
    nodes: list["WorkflowNode"]
    merge_strategy: str = "summarize"
    max_parallel: int = 3
    requires_approval: bool = False
```

## 5.2 WorkflowNode

```python
@dataclass
class WorkflowNode:
    id: str
    type: Literal["subagent", "tool", "merge", "verify"]
    agent_role: str | None
    task: str
    tools: list[str]
    depends_on: list[str]
    timeout: int = 300
    risk_level: Literal["low", "medium", "high"] = "low"
```

## 5.3 WorkflowRun

```python
@dataclass
class WorkflowRun:
    id: str
    chat_id: str
    agent_id: str
    status: Literal["planning", "running", "suspended", "done", "failed"]
    spec: WorkflowSpec
    node_results: dict[str, "WorkflowNodeResult"]
    created_at: int
    updated_at: int
```

## 5.4 Node Result

```python
@dataclass
class WorkflowNodeResult:
    node_id: str
    status: Literal["pending", "running", "done", "failed", "skipped"]
    summary: str
    artifacts: dict[str, Any]
    agent_run_id: str | None = None
    error: str | None = None
```

---

# 6. 数据库表设计

新增表：

```sql
CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS workflow_nodes (
    workflow_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    status TEXT NOT NULL,
    agent_run_id TEXT,
    result_json TEXT,
    started_at INTEGER,
    finished_at INTEGER,
    error TEXT,
    PRIMARY KEY (workflow_id, node_id)
);
```

如果后面接 Transcript，可以再让每个 workflow run 对应一个 transcript branch。

---

# 7. 最关键的一步：Workflow Decision

不是每个任务都要 workflow。否则系统会变慢、变贵、复杂化。

你要加一个 `WorkflowPlanner.should_use_workflow()`。

判断规则可以先用启发式 + LLM 混合。

## 7.1 启发式判断

适合 workflow 的任务：

```text
代码库级别分析
大规模重构
安全审计
迁移任务
多文件修改
需要先调查再实现再测试
用户明确说“全面检查/系统分析/完整改造”
任务超过一个模块
需要交叉验证
```

不适合 workflow 的任务：

```text
简单问答
读一个文件
改一个小 bug
解释一段代码
生成一段 prompt
单次 shell 命令
```

伪代码：

```python
def should_use_workflow(user_text: str, context: AgentContext) -> bool:
    keywords = [
        "完整", "全面", "重构", "迁移", "审计", "排查",
        "整个项目", "多模块", "系统性", "优化计划",
        "review", "refactor", "migration", "audit"
    ]
    if any(k in user_text.lower() for k in keywords):
        return True

    if len(user_text) > 500:
        return True

    return False
```

## 7.2 LLM 判断

再加一层轻量 LLM 判断：

```text
你是 Workflow Planner。
判断用户任务是否需要 workflow。

只能输出 JSON：
{
  "use_workflow": true/false,
  "reason": "...",
  "suggested_workflow_type": "code_review|debug_fix|migration|research|none",
  "risk_level": "low|medium|high"
}
```

这样就实现“自动判断是否搭建 workflow”。

---

# 8. Workflow Planner：自动生成工作流

如果判断需要 workflow，就让 LLM 生成 `WorkflowSpec`。

Prompt 大概是：

```text
你是 MiniClaw Workflow Planner。

目标：
根据用户任务生成一个安全、可执行、可审计的 workflow。

约束：
1. 只能输出 JSON。
2. 不能生成 Python 代码。
3. 每个 node 必须声明 depends_on。
4. 每个 node 必须声明 tools。
5. 高风险工具必须标记 risk_level=high。
6. 不允许节点直接绕过 PermissionGate。
7. 修改类任务必须包含 verify 节点。
8. 并行节点不能写同一文件。
9. 所有写操作必须依赖 read/plan 节点之后。
10. max_parallel 最大 3。

可用 node type：
- subagent
- tool
- merge
- verify

可用 agent_role：
- researcher
- code_reviewer
- planner
- implementer
- tester
- security_reviewer
- summarizer
```

输出后系统要校验：

```python
validate_workflow_spec(spec)
```

校验内容：

```text
必须是 DAG，不能有环
node id 唯一
depends_on 存在
tools 在 agent allowlist 内
max_parallel <= 配置上限
risk_level 合法
写操作节点不能并行写同一文件
高风险节点需要 approval
```

---

# 9. Workflow Runner：怎么执行多个 subagent？

每个 workflow node 都可以启动一个子 AgentRun。

注意：这里的 subagent 不一定是 Phase 1 里永久配置的 agent，也可以是临时 agent profile。

## 9.1 子 Agent 上下文

每个子任务单独构造 messages：

```python
sub_messages = [
    {
        "role": "system",
        "content": f"""
你是 {node.agent_role}。
你只负责当前 workflow node，不要做 node 范围外的事情。

Workflow 目标：
{workflow_goal}

当前节点任务：
{node.task}

上游节点结果：
{dependency_summaries}

输出格式：
1. 你做了什么
2. 发现了什么
3. 产出哪些 artifacts
4. 是否需要后续节点注意
"""
    },
    {
        "role": "user",
        "content": node.task
    }
]
```

每个子 AgentRun 用独立 `messages`，但共享：

```text
workspace_dir
PermissionGate
ToolRegistry
AuditLogger
TaskState
```

这样能做到：

```text
上下文隔离
执行环境共享
权限统一
审计统一
```

这和 Claude Code subagents 的主要价值很像：subagent 用独立上下文处理聚焦任务，避免污染主上下文，也可以并行分析。([Claude Code][2])

## 9.2 并行调度

DAG 执行逻辑：

```python
while not all_nodes_done:
    ready_nodes = [
        node for node in nodes
        if node.status == "pending"
        and all(dep.status == "done" for dep in node.depends_on)
    ]

    batch = ready_nodes[:max_parallel]

    await asyncio.gather(*[
        run_node(node) for node in batch
    ])
```

但注意：

```text
只读节点可以并行
写文件节点默认串行
run_shell 节点默认串行，除非标记 concurrent_safe
高风险节点必须审批
```

---

# 10. Workflow Merger：合并多个结果

多个 subagent 跑完后，不能直接把所有结果塞给主 Agent，会爆上下文。

要有一个 Merger。

## 10.1 Merge 输入

```json
{
  "workflow_goal": "...",
  "node_results": [
    {
      "node_id": "scan",
      "summary": "...",
      "artifacts": {
        "relevant_files": [...]
      }
    },
    {
      "node_id": "verify",
      "summary": "...",
      "artifacts": {
        "tests": "failed",
        "errors": [...]
      }
    }
  ]
}
```

## 10.2 Merge 输出

```json
{
  "final_summary": "...",
  "completed": true,
  "key_findings": [],
  "files_changed": [],
  "tests_run": [],
  "remaining_risks": [],
  "recommended_next_steps": []
}
```

最后主 Agent 用这个结果回复用户。

---

# 11. 自动搭多个 workflow 怎么做？

你说的“甚至同时搭建多个 workflow”，可以这样实现：

```text
WorkflowPlanner 不只返回一个 spec，而是返回 WorkflowBundle
```

数据结构：

```python
@dataclass
class WorkflowBundle:
    workflows: list[WorkflowSpec]
    execution_policy: Literal["parallel", "sequential", "choose_best"]
    merge_strategy: Literal["compare", "vote", "synthesize"]
```

比如用户说：

> 全面检查这个项目还有什么问题。

系统可以自动建 3 个 workflow：

```text
Workflow A：安全审计
Workflow B：架构审计
Workflow C：测试覆盖审计
```

它们可以并行跑，最后由 `BundleMerger` 汇总：

```text
安全问题：
架构问题：
测试问题：
优先级：
建议执行顺序：
```

这个很像你现在一直在做的事情：一边看安全，一边看架构，一边看执行可行性。只是现在把它系统化。

---

# 12. 要接入你现有 MiniClaw 的哪些模块？

## 12.1 接 Gateway

在 `Gateway.handle_message()` 里加：

```python
if self._workflow_planner.should_use_workflow(msg.text, ctx):
    workflow_spec = await self._workflow_planner.plan(msg.text, ctx)
    workflow_run = await self._workflow_runner.run(workflow_spec, ctx)
    final_answer = await self._workflow_merger.merge(workflow_run)
    await channel.send(msg.chat_id, final_answer)
    return
```

但建议不要一开始就自动执行。第一版可以：

```text
检测到需要 workflow
↓
生成 workflow plan
↓
发给用户确认
↓
用户点 approve
↓
执行 workflow
```

尤其是包含写文件 / shell / 多 agent 并行的时候，要走审批。

---

## 12.2 接 AgentManager

Workflow node 的 `agent_role` 可以映射到 agent profile：

```yaml
workflow_agents:
  researcher:
    tools: [read_file, list_directory]
    system_prompt: "你负责调查，不允许修改文件"

  implementer:
    tools: [read_file, write_file]
    system_prompt: "你负责最小改动实现"

  tester:
    tools: [run_shell, read_file]
    system_prompt: "你负责运行测试和分析失败"
```

第一版可以不真的创建多个长期 agent，而是创建临时 `AgentProfile`。

---

## 12.3 接 PermissionGate

所有 workflow node 的工具调用仍然必须走：

```text
PermissionGate
↓
ChainDetector
↓
TaskState constraint
↓
Tool execute
```

不要让 workflow runner 绕过工具执行链。

---

## 12.4 接 ApprovalStore

以下情况必须挂起审批：

```text
workflow 包含 high risk node
workflow 包含 write_file
workflow 包含 run_shell
workflow 节点数超过阈值
workflow max_parallel > 1 且涉及写操作
workflow 要跨 workspace
```

审批卡片可以显示：

```text
Workflow: codebase_migration
Nodes: 6
Tools: read_file, write_file, run_shell
Risk: medium
Will modify files: unknown yet
Requires approval: yes
```

---

## 12.5 接 Skills

Skills 可以帮助 planner 生成更好的 workflow。

例如：

```text
debug-fix skill
security-audit skill
migration skill
code-review skill
```

Workflow Planner 可以根据 task 激活对应 skill。

---

# 13. 第一版 MVP 怎么做？

不要一上来做完整 Dynamic Workflows。建议做 3 个模板 + 1 个动态 planner。

## MVP 1：debug_fix workflow

适合：

```text
报错
测试失败
traceback
bug 修复
```

流程：

```text
scan_error
↓
locate_files
↓
propose_fix
↓
apply_fix
↓
run_test
↓
summarize
```

## MVP 2：code_review workflow

适合：

```text
看这个项目怎么样
还有哪些问题
能不能写进简历
架构评审
```

流程：

```text
architecture_review
security_review
test_review
docs_review
↓
merge_findings
```

这里 4 个 review node 可以并行。

## MVP 3：migration workflow

适合：

```text
把 X 改成 Y
升级架构
重构模块
迁移 API
```

流程：

```text
inventory
↓
migration_plan
↓
apply_changes
↓
compatibility_check
↓
run_tests
↓
report
```

## MVP 4：dynamic workflow

如果模板匹配不到，就让 LLM 生成 WorkflowSpec。

---

# 14. 文件级改造计划

新增：

```text
mini_claw/workflow/spec.py
mini_claw/workflow/planner.py
mini_claw/workflow/runner.py
mini_claw/workflow/scheduler.py
mini_claw/workflow/merger.py
mini_claw/workflow/store.py
mini_claw/workflow/templates.py
```

修改：

```text
mini_claw/gateway/router.py
mini_claw/agent/manager.py
mini_claw/agent/loop.py
mini_claw/storage/db.py
mini_claw/config.py
mini_claw/audit/logger.py
mini_claw/permissions/approval_store.py
```

新增测试：

```text
tests/test_workflow_planner.py
tests/test_workflow_spec_validation.py
tests/test_workflow_runner.py
tests/test_workflow_parallel.py
tests/test_workflow_approval.py
tests/test_workflow_merger.py
```

---

# 15. 配置设计

```yaml
workflow:
  enabled: true
  auto_detect: true
  require_approval: true

  max_workflows_per_message: 3
  max_nodes_per_workflow: 8
  max_parallel_nodes: 3
  max_total_agent_runs: 12

  allow_dynamic: true
  allow_llm_generated_script: false

  templates:
    debug_fix:
      enabled: true
    code_review:
      enabled: true
    migration:
      enabled: true

  risk_policy:
    write_file: approval
    run_shell: approval
    multi_workflow: approval
    dynamic_workflow: approval
```

关键点：

```text
allow_llm_generated_script: false
```

第一版坚决不要让 LLM 生成脚本执行。

---

# 16. Prompt 设计

## 16.1 Workflow Decision Prompt

```text
你是 MiniClaw 的 Workflow Decision 模块。

判断用户任务是否需要 workflow。

适合 workflow：
- 多文件、多模块任务
- 需要先调查再修改再验证
- 需要并行审计
- 需要多个专业角色交叉检查
- 用户要求“完整”“全面”“系统性”

不适合 workflow：
- 简单问答
- 单文件解释
- 单次命令
- 小范围文本改写

只能输出 JSON：
{
  "use_workflow": true,
  "reason": "...",
  "workflow_type": "debug_fix|code_review|migration|dynamic|none",
  "estimated_risk": "low|medium|high"
}
```

## 16.2 Workflow Plan Prompt

```text
你是 MiniClaw Workflow Planner。

请根据用户任务生成 WorkflowSpec JSON。

硬性限制：
1. 只能输出 JSON。
2. 不能输出 Python/JS/Shell 脚本。
3. 节点数最多 8 个。
4. max_parallel 最多 3。
5. 所有节点必须是 DAG。
6. 写文件节点必须依赖计划节点。
7. 测试/验证节点必须在修改节点之后。
8. 每个节点必须声明 tools。
9. 不能使用 agent allowlist 之外的工具。
10. 如果使用 write_file 或 run_shell，risk_level 至少为 medium。
```

---

# 17. 安全边界

这个功能的风险比普通 Agent Loop 更高，因为它会：

```text
自动拆任务
自动并行
自动调用多个 agent
可能扩大工具调用次数
可能扩大文件修改范围
```

所以必须加这些限制：

```text
1. workflow 默认需要 approval，尤其是 dynamic workflow。
2. workflow 不允许绕过 PermissionGate。
3. workflow 不允许自动切 bypass。
4. workflow 不允许生成并执行脚本。
5. workflow 的 max_nodes / max_parallel / max_total_agent_runs 必须有限制。
6. workflow 所有 node 都要写 audit。
7. workflow 修改文件前必须先生成 plan。
8. workflow 完成后必须输出 files_changed / tests_run / remaining_risks。
```

---

# 18. 简历里怎么说这个能力？

可以写成：

> 设计并实现 Dynamic Workflow Orchestrator，使 Agent 能根据任务复杂度自动判断是否需要工作流，并生成受控 WorkflowSpec，将复杂任务拆分为多个可并行执行的 sub-agent 节点；通过 DAG 调度、权限审批、审计日志、结果合并和验证节点，实现复杂代码审计、debug 修复和迁移任务的自动化编排。

这个点非常高级，比单纯“多 Agent”更强。

---

# 最终建议

你这个想法**非常值得做**，但不要直接照抄 Claude Code 的“LLM 生成脚本并执行”。在 MiniClaw 里更适合做成：

```text
LLM 生成 WorkflowSpec JSON
系统校验 spec
WorkflowRunner 按 DAG 执行
每个节点启动 subagent
所有工具仍走 PermissionGate
结果由 Merger 汇总
高风险 workflow 需要 approval
```

一句话总结：

> **在你的系统里，实现这个能力的关键不是“多开几个 Agent”，而是新增一个 Workflow Orchestrator：它负责自动判断是否需要 workflow、生成受控 WorkflowSpec、调度多个 subagent、合并结果，并且全程受 PermissionGate、ApprovalStore、AuditLogger 和 TaskState 约束。**

[1]: https://code.claude.com/docs/en/workflows?utm_source=chatgpt.com "Orchestrate subagents at scale with dynamic workflows"
[2]: https://code.claude.com/docs/en/agent-sdk/subagents?utm_source=chatgpt.com "Subagents in the SDK - Claude Code Docs"
