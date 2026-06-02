# Mini-Claw

> 从 0 到 1 用 Python 搭建的个人 AI Agent 助手

以飞书为主交互界面，背后是一个 LLM agent loop，能调用工具（shell、文件、HTTP、定时任务等）替你执行任务，支持多个隔离的 agent 实例，并通过权限系统保证安全。

## 快速开始

### 1. 安装

```bash
pip install -e .
```

### 2. 初始化配置

```bash
mini-claw setup
```

这会在**当前目录**生成 `config.yaml`，编辑它填入你的 API Key。后续命令默认从当前目录读取这份配置；也可以用 `--config /path/to/config.yaml` 指定其它路径。

### 3. 自检

```bash
mini-claw doctor
```

### 4. 本地对话测试（无需飞书）

```bash
mini-claw chat
```

### 5. 启动服务（接入飞书）

```bash
mini-claw run
```

> 飞书使用**长连接（WebSocket）模式**：服务启动时由 SDK 主动连接飞书服务器，不需要公网域名、Webhook URL 或加密 Key。在 `config.yaml` 里把 `channels_feishu.enabled` 设成 `true` 并填入 `app_id` / `app_secret` 即可。

## 架构概览

```
飞书消息 → Channel Adapter → Gateway → Agent Loop → Provider (DeepSeek/OpenAI)
                                ↓
                          Tool Registry → [shell|file|http|cron|...]
                                ↓
                        Permission Layer
```

## 模块说明

| 模块 | 职责 |
|------|------|
| `providers/` | LLM provider 抽象（DeepSeek/OpenAI/Ollama） |
| `channels/` | Channel 适配器（飞书/CLI） |
| `gateway/` | 路由、会话管理、事件总线 |
| `agent/` | Agent Loop 核心、工作空间管理 |
| `tools/` | 工具注册表 + 内置工具（shell/file/http） |
| `permissions/` | 权限分级、命令拦截、审批流 |
| `storage/` | SQLite 持久化 |
| `scheduler/` | APScheduler 定时任务 |
| `skills/` | 插件式技能加载 |

## 配置

复制 `config.example.yaml` 到当前目录的 `config.yaml`：

```bash
cp config.example.yaml config.yaml
```

主要配置项：
- `provider` — LLM 提供商（deepseek/openai/ollama）
- `channels_feishu` — 飞书应用凭证
- `permissions` — 权限分级策略
- `agents` — 多 agent 配置与路由

数据库（`mini_claw.db`）会与 `config.yaml` 同目录创建。

## 权限模型

| 级别 | 含义 | 行为 |
|------|------|------|
| L0 | 只读 | 自动放行 |
| L1 | 受限写 | 自动放行 |
| L2 | Shell | 黑名单拦截，其余放行 |
| L3 | 网络副作用 | 弹审批卡片，用户确认后执行 |
| L4 | 高危 | 默认拒绝，不提供确认按钮 |

## 开发

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
