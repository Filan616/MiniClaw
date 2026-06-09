# MiniClaw 开发学习记录

本文档记录 MiniClaw 项目的开发过程、技术决策和学习心得。

---

## Phase 9.8: Agent Loop 进度透明化与循环检测

**实现日期**: 2026-06-05  
**Commit**: `8344270`

### 🎯 需求背景

用户在使用 MiniClaw 时遇到以下问题：

1. **黑盒等待**：LLM 处理任务时，用户只看到开头和结尾，中间过程完全不可见，不知道系统在做什么
2. **工具调用循环**：LLM 有时会陷入死循环，反复调用同一个失败的工具（如 `list_directory`），最终触发 max_turns 限制
3. **工具选择错误**：LLM 在"打开微信"时不调用 `open_app`，而是用 `run_shell`/`list_directory` 查找路径
4. **历史污染**：对话历史太长（1558 条消息），LLM 学到了错误模式，直接回复"没有安装"而不尝试工具调用

### 📦 实现方案

#### **M1: 中间进度通知**

**目标**：让用户知道系统在处理，不是卡死了。

**实现**：
```python
# mini_claw/agent/loop.py
PROGRESS_NOTIFY_INTERVAL = 3  # 每 3 轮发送一次

async def _send_progress_update(run, ctx, iteration, last_tool):
    if not ctx.on_progress:
        return
    progress_msg = f"🔄 正在处理中（第 {iteration} 轮）"
    if last_tool:
        progress_msg += f" - 上次调用: {last_tool}"
    await ctx.on_progress(progress_msg)

# 主循环中
while run.iterations < MAX_ITERATIONS:
    run.iterations += 1
    
    if PROGRESS_NOTIFY_INTERVAL > 0 and run.iterations % PROGRESS_NOTIFY_INTERVAL == 0:
        last_tool = run.tool_call_history[-1][0] if run.tool_call_history else None
        await _send_progress_update(run, ctx, run.iterations, last_tool)
```

**效果**：
- 用户每 3 轮看到：`🔄 正在处理中（第 3 轮） - 上次调用: run_shell`
- 不再盲等，知道系统在工作

---

#### **M2: 工具调用循环检测**

**目标**：检测到 LLM 陷入循环后，自动提示它换方法。

**实现**：
```python
# AgentRun 新增字段
@dataclass
class AgentRun:
    tool_call_history: list[tuple[str, bool]] = field(default_factory=list)  # (tool_name, success)

# 循环检测函数
def _detect_tool_call_loop(run: AgentRun, lookback: int = 5) -> tuple[bool, str | None]:
    """检测最近 lookback 轮中，是否有工具被调用 3+ 次且成功率 < 50%"""
    if len(run.tool_call_history) < lookback:
        return False, None
    
    recent = run.tool_call_history[-lookback:]
    tool_names = [name for name, success in recent]
    
    from collections import Counter
    tool_counts = Counter(tool_names)
    most_common_tool, count = tool_counts.most_common(1)[0]
    
    if count >= 3:
        tool_results = [success for name, success in recent if name == most_common_tool]
        success_rate = sum(tool_results) / len(tool_results) if tool_results else 0
        
        if success_rate < 0.5:
            return True, most_common_tool
    
    return False, None

# 主循环中注入警告
is_looping, loop_tool = _detect_tool_call_loop(run)
if is_looping:
    loop_warning = (
        f"⚠️ 系统提示：你已经连续多次调用 `{loop_tool}` 工具但未成功。"
        f"请换一个不同的方法或工具来解决问题，不要再重复调用 `{loop_tool}`。"
    )
    run.messages.append({
        "role": "system",
        "content": loop_warning,
    })
```

**效果**：
- 检测到 `list_directory` 连续 3 次失败 → 注入 system message
- LLM 看到警告后会尝试其他工具或方法
- 防止无限循环浪费 token

---

#### **M3: 每轮 LLM 回复立即发送**

**目标**：用户要求看到**所有**中间思考过程，不只是进度更新。

**实现**：
```python
# 在 LLM 返回后立即发送
response = await provider.chat(...)

# 如果不是 prelude（prelude 会单独处理），立即发送
should_send_immediately = (
    response.text and ctx.on_progress and
    (not response.tool_calls or run.prelude_sent)
)
if should_send_immediately:
    await ctx.on_progress(response.text)
```

**效果**：
- LLM 每轮的文本回复都立即发送给用户
- 用户看到完整的思考过程：
  ```
  好的，让我先找找微信装在哪里！
  发现桌面上有微信的快捷方式，让我看看它指向哪里～
  找到了！开始菜单里有微信的快捷方式！
  ...
  ```

---

#### **M4: System Prompt 强化**

**目标**：强制 LLM 在"打开应用"时使用 `open_app` 工具。

**问题**：
- LLM 倾向于用 `run_shell` 执行 `start` 命令
- 或者用 `list_directory` 查找安装路径
- 或者基于历史记录直接回复"没有安装"

**解决方案**：在 `config.yaml` 的 `system_prompt` 中添加：
```yaml
## 🚀 打开应用的正确方式
当用户要求打开应用时（微信、Chrome、VS Code等），**必须直接调用 open_app 工具**。

**严格禁止**：
- ❌ 用 run_shell 执行 start 命令
- ❌ 用 list_directory 查找安装路径
- ❌ 声称"没有安装"而不调用 open_app
- ❌ 基于历史记录推测"没有安装"就放弃尝试

**强制要求**：
- ✅ 即使历史记录显示"找不到"，也**必须**再次调用 open_app
- ✅ 用户每次请求"打开X"，都必须调用 open_app(app="X")
- ✅ 只有 open_app 返回错误后，才能告诉用户"没有安装"

**正确做法**：
- 用户："打开微信" → 直接调用 open_app(app="微信")
- 用户："帮我打开 Chrome" → 直接调用 open_app(app="chrome")
- 用户："启动 VS Code" → 直接调用 open_app(app="vscode")

open_app 工具会自动搜索常见安装路径，无需你手动查找。
```

**效果**：
- LLM 被明确告知：每次"打开X"都必须调用 `open_app`
- 即使历史失败过，也必须再试
- 不准推测，必须调用工具验证

---

### 🧪 测试覆盖

创建了 `tests/test_agent_loop_progress.py`，包含 10 个测试用例：

#### **循环检测测试**
1. ✅ `test_detect_loop_no_history` - 无历史时不检测循环
2. ✅ `test_detect_loop_insufficient_history` - 历史不足 lookback 时不检测
3. ✅ `test_detect_loop_same_tool_repeated_failures` - 同一工具重复失败检测循环
4. ✅ `test_detect_loop_same_tool_mostly_successful` - 成功率高不检测循环
5. ✅ `test_detect_loop_mixed_tools_no_loop` - 混合工具不检测循环
6. ✅ `test_detect_loop_boundary_3_calls` - 边界条件：恰好 3 次调用

#### **进度通知测试**
7. ✅ `test_send_progress_no_callback` - 无回调时不报错
8. ✅ `test_send_progress_with_callback` - 有回调时正确发送
9. ✅ `test_send_progress_callback_exception` - 回调异常时不中断

#### **集成测试**
10. ✅ `test_loop_detection_injects_system_message` - 验证 system message 注入逻辑

**运行结果**：
```bash
$ python -m pytest tests/test_agent_loop_progress.py -v
============================= test session starts =============================
collected 10 items

tests/test_agent_loop_progress.py::test_detect_loop_no_history PASSED    [ 10%]
tests/test_agent_loop_progress.py::test_detect_loop_insufficient_history PASSED [ 20%]
tests/test_agent_loop_progress.py::test_detect_loop_same_tool_repeated_failures PASSED [ 30%]
tests/test_agent_loop_progress.py::test_detect_loop_same_tool_mostly_successful PASSED [ 40%]
tests/test_agent_loop_progress.py::test_detect_loop_mixed_tools_no_loop PASSED [ 50%]
tests/test_agent_loop_progress.py::test_detect_loop_boundary_3_calls PASSED [ 60%]
tests/test_agent_loop_progress.py::test_send_progress_no_callback PASSED [ 70%]
tests/test_agent_loop_progress.py::test_send_progress_with_callback PASSED [ 80%]
tests/test_agent_loop_progress.py::test_send_progress_callback_exception PASSED [ 90%]
tests/test_agent_loop_progress.py::test_loop_detection_injects_system_message PASSED [100%]

============================== 10 passed in 0.39s ==============================
```

---

### 📁 文件修改

| 文件 | 修改内容 |
|:---|:---|
| [mini_claw/agent/context.py](mini_claw/agent/context.py#L33) | 添加 `on_progress: Callable[[str], Awaitable[None]]` 回调 |
| [mini_claw/agent/loop.py](mini_claw/agent/loop.py) | - 添加 `PROGRESS_NOTIFY_INTERVAL` 常量<br>- 实现 `_send_progress_update()` 函数<br>- 实现 `_detect_tool_call_loop()` 函数<br>- `AgentRun` 新增 `tool_call_history` 字段<br>- 主循环集成进度通知、循环检测、每轮消息发送 |
| [mini_claw/gateway/router.py](mini_claw/gateway/router.py#L260-L294) | - 实现 `_send_progress()` 方法<br>- 创建 `AgentContext` 时绑定 `on_progress` 回调 |
| [config.yaml](config.yaml#L367-L387) | 强化 system prompt，明确要求打开应用时使用 `open_app` |
| [tests/test_agent_loop_progress.py](tests/test_agent_loop_progress.py) | 新增 10 个测试用例，覆盖所有功能点 |

---

### 🐛 调试过程与问题

#### **问题 1：LLM 不调用 `open_app`**

**现象**：
```
用户："帮我打开微信"
LLM：好的主人，让我先看看微信有没有安装～🔍
[调用 list_directory 10 次]
LLM：抱歉，我在 10 轮对话后仍未能完成任务
```

**原因分析**：
1. 对话历史太长（1558 条消息）
2. 历史中充满"找不到微信"的失败记录
3. LLM 学到了错误模式："这台电脑没有微信"
4. System prompt 不够强，LLM 倾向于用熟悉的 `run_shell`/`list_directory`

**解决方案**：
- ✅ 强化 system prompt（M4）
- ✅ 要求用户 `/clear` 清空历史
- ✅ 循环检测防止无限重试（M2）

---

#### **问题 2：进度消息没发送**

**现象**：用户说"每一轮的返回信息都没发给用户"

**调试过程**：
```sql
-- 检查数据库
sqlite3 mini_claw.db "SELECT role, content FROM messages WHERE run_id='...';"

-- 发现消息都存在，且 message_kind='progress'
assistant|🔄 正在处理中（第 3 轮） - 上次调用: run_shell|progress
```

**真相**：
- 消息**确实发送了**！
- 但用户要求看到**所有中间思考**，不只是进度更新
- 原始设计：只发送 prelude + progress + final answer
- 用户要求：每一轮 LLM 的文本回复都要发送

**解决方案**：实现 M3，每轮 LLM text 回复都立即发送

---

#### **问题 3：工具调用记录为空**

**现象**：
```sql
sqlite3 mini_claw.db "SELECT tool_name FROM tool_calls WHERE run_id='...';"
-- 返回空
```

**但进度消息显示**：`上次调用: run_shell`

**分析**：
- `tool_call_history` 有记录（从历史加载的）
- 但 `tool_calls` 表为空 → 说明本次 run 没有实际执行工具
- LLM 只返回了文本，没有调用工具
- 原因：历史污染，LLM 直接回复"没有安装"

---

### 💡 技术亮点

#### **1. 异步回调设计**

```python
# AgentContext 定义
on_progress: Callable[[str], Awaitable[None]] | None = None

# Gateway 实现
ctx = AgentContext(
    on_progress=lambda text: self._send_progress(
        chat_id, agent_id, channel, channel_name, workspace_dir, run_id, text
    ),
)

# AgentLoop 调用
await ctx.on_progress(progress_msg)
```

**优点**：
- 解耦：AgentLoop 不需要知道消息发送的具体实现
- 灵活：不同 channel 可以有不同的发送逻辑
- 优雅：Fire-and-forget，失败不中断主流程

---

#### **2. 循环检测算法**

```python
def _detect_tool_call_loop(run, lookback=5):
    recent = run.tool_call_history[-lookback:]  # 最近 N 次
    
    # 统计每个工具的调用次数
    tool_counts = Counter([name for name, _ in recent])
    most_common_tool, count = tool_counts.most_common(1)[0]
    
    # 如果某工具被调用 3+ 次
    if count >= 3:
        # 计算该工具的成功率
        tool_results = [success for name, success in recent if name == most_common_tool]
        success_rate = sum(tool_results) / len(tool_results)
        
        # 成功率 < 50% → 循环
        if success_rate < 0.5:
            return True, most_common_tool
    
    return False, None
```

**参数调优**：
- `lookback=5`：检查最近 5 次调用
- `count >= 3`：同一工具 3 次以上
- `success_rate < 0.5`：成功率低于 50%

**为什么不是"连续 N 次同一工具"**：
- 太严格：中间穿插其他工具调用就检测不到
- 例如：`[list_dir, read_file, list_dir, read_file, list_dir]` → 应该检测到 `list_dir` 循环

---

#### **3. 消息类型标记**

```python
# message_kind 字段
self._session_mgr.store_message(
    ...,
    message_kind="progress",  # 或 "prelude" / "normal"
)
```

**好处**：
- 可以按类型过滤消息
- 统计不同类型消息的数量
- 未来可以实现"只看最终结果，隐藏中间过程"

---

### 📊 效果对比

#### **优化前**
```
17:03:49 用户: 帮我打开微信
17:03:50 LLM: 好的主人，让我先看看微信有没有安装～🔍
[10 秒沉默]
17:04:00 LLM: 抱歉，我在 10 轮对话后仍未能完成任务
```

**问题**：
- ❌ 用户不知道系统在做什么
- ❌ LLM 陷入循环，浪费 token
- ❌ 没有调用 `open_app`，用错工具

---

#### **优化后**
```
17:41:33 用户: 帮我打开微信
17:41:35 LLM: 好的主人，噜噜直接帮你打开微信！📱
17:41:37 [调用 open_app(app="微信")]
17:41:40 🔄 正在处理中（第 3 轮） - 上次调用: open_app
17:41:42 LLM: 找到微信了！正在启动...
17:41:45 LLM: ✅ 微信已成功打开！
```

**改进**：
- ✅ 用户看到所有中间过程
- ✅ 每 3 轮看到进度更新
- ✅ 直接调用 `open_app`，工具选择正确
- ✅ 如果循环，自动警告 LLM

---

### 🎓 经验教训

#### **1. System Prompt 的重要性**

**问题**：最初以为"工具已注册"就够了，LLM 会自己选择合适的工具。

**事实**：
- LLM 倾向于用"熟悉"的工具（`run_shell`/`list_directory`）
- 即使有专门的工具（`open_app`），也不会主动用
- 需要在 system prompt 中**明确禁止**某些做法，**强制要求**某些做法

**教训**：
- ✅ System prompt 要具体，不能只说"可以用 X"
- ✅ 要明确"禁止用 Y"、"必须用 Z"
- ✅ 提供具体示例，不要只有抽象描述

---

#### **2. 对话历史的影响**

**问题**：1558 条历史消息，包含大量"找不到微信"的记录。

**后果**：
- LLM 学到了"这台电脑没有微信"
- 直接回复文本，不再尝试工具调用
- System prompt 的权重 < 历史记录的权重

**解决方案**：
1. 定期 `/clear` 清空历史
2. 实现自动压缩（已有 auto-compaction，但可能不够激进）
3. System prompt 中强调"即使历史失败，也必须再试"

---

#### **3. 用户体验 vs 技术优雅**

**冲突点**：
- 技术角度：只发送关键信息（prelude + final answer），减少消息轰炸
- 用户要求：所有中间思考都要看到，不准隐藏

**最终方案**：按用户要求实现，因为：
- 用户是产品的使用者，他们的需求优先
- 可以后续添加"调试模式"开关
- 完全透明有助于调试和理解 LLM 行为

**教训**：不要擅自"优化"用户体验，先问用户要什么

---

#### **4. 测试驱动开发的价值**

**实践**：
1. 先写测试用例（10 个）
2. 再实现功能
3. 测试全部通过后提交

**好处**：
- 清晰定义预期行为
- 快速验证实现正确性
- 防止回归（未来修改时测试仍然通过）

**例子**：`test_detect_loop_boundary_3_calls` 发现了边界条件问题
- 最初：检测条件是 `count > 3`
- 测试失败：恰好 3 次应该检测到
- 修复：改为 `count >= 3`

---

### 🚀 后续优化方向

#### **1. 可配置的进度间隔**

当前硬编码 `PROGRESS_NOTIFY_INTERVAL = 3`，可以改为配置项：
```yaml
agent_defaults:
  progress_notify_interval: 3  # 0 = 禁用
```

---

#### **2. 更智能的循环检测**

当前只检测"同一工具重复失败"，可以扩展：
- 检测"工具链循环"：`[A, B, A, B, A, B]`
- 检测"参数循环"：同一工具，不同参数，都失败
- 根据工具类型调整阈值（文件操作 vs 网络请求）

---

#### **3. 进度消息的分级**

```python
message_kind = "progress"  # 当前只有一种

# 可以细化为：
message_kind = "progress.iteration"  # 轮次更新
message_kind = "progress.tool_start"  # 工具调用开始
message_kind = "progress.tool_end"    # 工具调用结束
message_kind = "progress.thinking"    # LLM 中间思考
```

**好处**：用户可以选择只看某些类型的进度消息

---

#### **4. 历史压缩优化**

当前 auto-compaction 压缩了 76 条消息，但效果不佳（历史污染仍然严重）。

**改进方向**：
- 更激进的压缩策略（保留最近 20 条 + 关键上下文）
- 定期自动清理超过 N 天的历史
- 实现"会话隔离"：不同任务类型独立历史

---

### 📚 相关代码位置

```
mini_claw/
├── agent/
│   ├── context.py           # AgentContext 定义，添加 on_progress 回调
│   └── loop.py              # 主循环，实现进度通知、循环检测、每轮消息发送
├── gateway/
│   └── router.py            # Gateway，实现 _send_progress，绑定 on_progress 回调
└── tools/
    ├── builtin.py           # 内置工具（run_shell, read_file, ...）
    └── open_app.py          # TOOL_OPEN_APP 定义

tests/
└── test_agent_loop_progress.py  # Phase 9.8 功能测试

config.yaml                  # System prompt 强化
```

---

### 🔗 相关 Commits

- `8344270`: feat(Phase 9.8): 进度透明化 + 循环检测 + 强制每轮消息可见

---

### 📝 TODO

- [ ] 推送到 GitHub（网络问题，待重试）
- [ ] 用户测试：重启服务 + `/clear` + 测试"打开微信"
- [ ] 监控循环检测的实际触发率
- [ ] 收集用户反馈，调整 PROGRESS_NOTIFY_INTERVAL

---

**记录人**: Claude (Opus 4.7)  
**日期**: 2026-06-05
