# Workspace 配置指南

## 问题场景

**症状**：用户在飞书中请求读取 `D:\Learning\MiniClaw\LEARNING.md`，系统进行了 9-10 次 API 调用但仍无法完成，最终返回模糊的错误消息。

**根本原因**：
1. 飞书用户的 workspace 默认是 `{data_dir}/workspaces/default`
2. 用户请求的文件 `D:\Learning\MiniClaw\LEARNING.md` 在 workspace 外部
3. `ensure_inside()` 检测到路径逃逸，返回 `[ERROR] Path outside workspace`
4. LLM 不理解这是永久性错误，尝试不同路径格式，陷入循环
5. 最终达到 MAX_ITERATIONS (10次)，返回 ABORTED 状态

## 解决方案

### 方案 1：配置更宽松的 workspace（推荐用于开发环境）

在 `config.yaml` 中为 agent 指定更宽松的 workspace：

```yaml
agents:
  - id: default
    system_prompt: "你是一个高效的个人助手，能调用工具帮用户完成各种任务。"
    workspace: "../.."  # 相对于 {data_dir}/workspaces 的路径
    tools:
      - run_shell
      - read_file
      - write_file
      - list_directory
```

这样 workspace 会是项目根目录，用户可以访问整个项目。

**警告**：这会让 agent 能访问整个项目目录，包括 `.git`、`.env` 等敏感文件。只在开发环境使用。

### 方案 2：使用 bypass 模式（临时访问）

用户可以在对话中使用 `/bypass` 命令临时解除路径限制：

```
用户: /bypass
系统: [sandbox 模式已临时解除]
用户: 读取 D:\Learning\MiniClaw\LEARNING.md
```

bypass 模式会在单次对话后自动失效。

### 方案 3：将文件复制到 workspace（最安全）

```
用户: 请把 D:\Learning\MiniClaw\LEARNING.md 复制到当前工作目录
系统: [通过 run_shell 执行 cp 命令]
用户: 现在读取 LEARNING.md
```

### 方案 4：为特定 chat 配置专用 agent

为需要访问项目根目录的 chat 创建专用 agent：

```yaml
agents:
  - id: default
    workspace: "default"  # 隔离的工作区
    tools: [...]
  
  - id: dev-assistant
    workspace: "../.."  # 项目根目录
    route_chat_ids:  # 只有这些 chat 使用此 agent
      - "oc_d302ad8dc8da56b3e73a908fb84b331b"  # 你的飞书 chat ID
    tools: [...]
```

## 架构说明

### Workspace 目录结构

```
{data_dir}/
├── mini_claw.db
├── config.yaml
└── workspaces/
    ├── default/        # 默认 agent 的工作区
    ├── dev-assistant/  # 开发助手的工作区
    └── ...
```

### 路径解析逻辑

1. **相对路径**：相对于 workspace 解析
   - `LEARNING.md` → `{workspace}/LEARNING.md`
   - `../README.md` → `{workspace}/../README.md`（如果在 workspace 内）

2. **绝对路径**：
   - **普通模式**：必须在 workspace 内，否则拒绝
   - **bypass 模式**：允许访问任意路径

3. **敏感路径检查**：即使在 workspace 内，以下路径也会被拒绝：
   - `.env`, `.env.*`
   - `*.pem`, `*.key`, `id_rsa`
   - `.ssh/*`, `.git/config`, `.aws/*`
   - `*secret*`, `*token*`, `credentials.json`

## 错误消息改进

**修复前**：
```
[ERROR] Path outside workspace
```

**修复后**（Phase 9.1 hotfix）：
```
[ERROR] Path outside workspace. The requested file is not accessible 
because it's outside the allowed workspace directory. 
Only files within the workspace can be read. debug_id=abc123
```

这个更详细的错误消息可以帮助 LLM 理解这是永久性错误，避免无意义的重试。

## 调试建议

### 查看实际 workspace 路径

```bash
# CLI 模式
mini-claw chat
> /tasks  # 查看当前任务状态，会显示 workspace_dir

# 或查看配置
grep -A 5 "agents:" config.yaml
```

### 查看审计日志

```sql
SELECT event_type, details 
FROM security_audit 
WHERE event_type = 'path_escape_attempt' 
ORDER BY created_at DESC 
LIMIT 10;
```

### 测试路径解析

```python
from pathlib import Path
from mini_claw.utils.paths import ensure_inside

workspace = Path("D:/Learning/MiniClaw/workspaces/default")
test_path = "D:/Learning/MiniClaw/LEARNING.md"

try:
    resolved = ensure_inside(test_path, workspace)
    print(f"✓ {resolved}")
except Exception as e:
    print(f"✗ {e}")
```

## 相关文件

- `mini_claw/utils/paths.py` - 路径安全检查
- `mini_claw/tools/builtin.py` - read_file/write_file 实现
- `mini_claw/agent/workspace.py` - WorkspaceManager
- `mini_claw/agent/loop.py` - Agent loop (MAX_ITERATIONS)

## 未来改进

1. **智能路径建议**：当检测到路径逃逸时，建议用户使用 `/bypass` 或复制文件
2. **早期循环检测**：如果连续 3 次相同工具调用失败，提前 ABORT 并给出明确建议
3. **配置验证**：启动时检查 workspace 配置的合理性，警告过于宽松的设置
4. **用户友好的错误**：在 Channel 层面将技术错误消息转换为用户友好的建议
