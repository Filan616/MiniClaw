# MiniClaw 安装指南

## 系统要求

- **Python**: 3.11 或更高版本
- **操作系统**: Windows / macOS / Linux
- **磁盘空间**: 
  - 基础安装: ~200 MB
  - 完整安装（含向量模型）: ~2 GB

## 快速安装

### 方式 1: 基础安装（推荐新手）

只安装核心功能，不包含向量检索和代码锚点：

```bash
git clone <repo-url>
cd MiniClaw
pip install -r requirements.txt
mini-claw setup
mini-claw doctor
```

### 方式 2: 完整安装（所有功能）

包含向量检索、代码锚点、浏览器自动化：

```bash
git clone <repo-url>
cd MiniClaw
pip install -r requirements-full.txt
playwright install chromium
mini-claw setup
mini-claw doctor
```

### 方式 3: 开发模式（推荐贡献者）

使用 `pip install -e` 可编辑模式：

```bash
git clone <repo-url>
cd MiniClaw

# 基础安装
pip install -e .

# 或者按需添加可选功能
pip install -e '.[rag-vector]'      # + 向量检索
pip install -e '.[rag-code]'        # + 代码锚点增量 reindex
pip install -e '.[browser]'         # + 浏览器自动化
pip install -e '.[dev]'             # + 测试框架

# 完整功能（一次性安装）
pip install -e '.[browser,rag-vector,rag-code,dev]'
```

## 依赖说明

### 核心依赖（必需）

| 包名 | 版本 | 用途 |
|---|---|---|
| `lark-oapi` | ≥1.4 | 飞书 SDK（WebSocket 长连接） |
| `openai` | ≥1.40 | OpenAI / DeepSeek API 客户端 |
| `fastapi` | ≥0.110 | Webhook 服务器框架 |
| `uvicorn` | ≥0.29 | ASGI 服务器 |
| `httpx` | ≥0.27 | HTTP 客户端（异步） |
| `apscheduler` | ≥3.10 | 定时任务调度 |
| `sqlalchemy` | ≥2.0 | 数据库 ORM |
| `typer` | ≥0.12 | CLI 框架 |
| `pydantic` | ≥2.6 | 数据验证 |
| `pyyaml` | ≥6.0 | 配置文件解析 |
| `loguru` | ≥0.7 | 日志框架 |

### 可选依赖

#### RAG 向量检索（`[rag-vector]`）

**功能**: 语义搜索、embedding 相似度检索

**依赖**:
- `chromadb` ≥0.4 — 向量数据库
- `sentence-transformers` ≥2.2 — 本地 embedding 模型

**何时需要**:
- `config.yaml` 中 `rag.embedding.enabled: true`
- `rag.vector_backend: chroma`
- 使用 `/context search <query>` 进行语义搜索

**安装**:
```bash
pip install -e '.[rag-vector]'
```

#### RAG 代码锚点（`[rag-code]`）

**功能**: 基于 AST 的增量 reindex，只更新变化的函数/类

**依赖**:
- `tree-sitter` ≥0.22 — 语法树解析器
- `tree-sitter-language-pack` ≥0.7 — 多语言支持包

**何时需要**:
- 使用 `/context reindex <id> --incremental`
- 对大型代码仓库做增量索引（避免全量 rechunk）

**安装**:
```bash
pip install -e '.[rag-code]'
```

#### 浏览器自动化（`[browser]`）

**功能**: `/browser` skill，网页抓取和交互

**依赖**:
- `playwright` ≥1.44

**安装**:
```bash
pip install -e '.[browser]'
playwright install chromium
```

#### 开发测试（`[dev]`）

**功能**: 运行测试套件

**依赖**:
- `pytest` ≥8.0
- `pytest-asyncio` ≥0.23

**安装**:
```bash
pip install -e '.[dev]'
pytest tests/ -v
```

## 配置

### 1. 生成配置文件

```bash
mini-claw setup
```

会在当前目录生成 `config.yaml`，包含：
- Agent 配置（workspace、system_prompt、model）
- Provider 配置（OpenAI、DeepSeek API keys）
- Channel 配置（飞书 app_id / app_secret）
- RAG 配置（embedding、vector_backend、lifecycle）
- Permission 配置（L1-L5 权限分级）

### 2. 配置飞书（可选）

如果需要飞书入口：

1. 在飞书开放平台创建企业自建应用
2. 获取 `app_id` 和 `app_secret`
3. 在 `config.yaml` 中配置：
   ```yaml
   channels:
     feishu:
       enabled: true
       app_id: "cli_xxx"
       app_secret: "xxx"
   ```
4. 飞书使用 **WebSocket 长连接**，无需公网域名或 Webhook URL

### 3. 配置 Provider

在 `config.yaml` 的 `providers` 中添加 API key：

```yaml
providers:
  - name: deepseek
    provider_type: openai
    api_key: "sk-xxx"
    base_url: "https://api.deepseek.com"
    default_model: "deepseek-chat"
```

### 4. 自检

```bash
mini-claw doctor
```

会检查：
- 配置文件是否存在
- Provider API key 是否有效
- 可选依赖是否已安装
- 数据库是否可写
- 飞书 WebSocket 是否可连接

## 运行

### CLI 模式（本地对话）

```bash
mini-claw chat
```

走完整 Gateway 流程（权限、审计、会话、RAG），与飞书共享后端。

### 服务模式（飞书 + Webhook）

```bash
mini-claw run
```

启动：
- 飞书 WebSocket 长连接（如果 `feishu.enabled: true`）
- Webhook 服务器（8000 端口，用于卡片回调）

## 常见问题

### 1. `sentence-transformers not installed`

**原因**: 开启了 `rag.embedding.enabled: true`，但未安装向量依赖。

**解决**:
```bash
pip install -e '.[rag-vector]'
```

或在 `config.yaml` 关闭 embedding：
```yaml
rag:
  embedding:
    enabled: false
```

### 2. `tree-sitter` 相关错误

**原因**: 使用 `/context reindex <id> --incremental` 但未安装代码锚点依赖。

**解决**:
```bash
pip install -e '.[rag-code]'
```

或不使用 `--incremental` 标志（使用全量 reindex）。

### 3. 飞书 WebSocket 连接失败

**原因**: `app_id` / `app_secret` 错误，或应用未开通"消息与群组"权限。

**解决**:
1. 检查飞书开放平台应用配置
2. 确认已添加"接收消息 v2.0"和"发送消息"权限
3. 查看日志 `logs/mini_claw.log` 中的详细错误

### 4. 权限被拒（L3/L4/L5）

**原因**: 工具调用触发高风险权限，需要 `/bypass` 授权。

**解决**:
```bash
/bypass allow <tool_name>
/bypass allow <tool_name> --session  # 仅当前会话有效
```

查看当前 bypass 规则：
```bash
/bypass list
```

### 5. 测试失败

**解决**:
```bash
pip install -e '.[dev]'
pytest tests/ -v --tb=short
```

如果是向量/代码相关测试失败，确认已安装对应可选依赖。

## 升级

```bash
cd MiniClaw
git pull
pip install -r requirements.txt  # 或 requirements-full.txt
```

数据库迁移（如果需要）：
```bash
# MiniClaw 使用 SQLite，schema 变更会自动 ALTER TABLE
# 如果遇到兼容性问题，可以备份并重建：
cp data.db data.db.backup
rm data.db
mini-claw run  # 自动创建新 schema
```

## 卸载

```bash
pip uninstall mini-claw
rm -rf ~/.miniclaw  # 删除用户数据（可选）
```

## 下一步

- 阅读 [README.md](../README.md) 了解架构和功能
- 阅读 [LEARNING.md](LEARNING.md) 深入理解 187 个实现细节
- 查看 `examples/` 目录中的使用示例
- 加入社区讨论（如果有）

## 技术支持

- 问题反馈: GitHub Issues
- 文档问题: 提交 PR 改进本文档
- 安全漏洞: 私下联系维护者（不要公开提 issue）
