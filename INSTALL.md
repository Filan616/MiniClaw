# MiniClaw 安装指南

## 快速安装

### 基础版（推荐）
```bash
pip install -r requirements.txt
mini-claw setup
mini-claw doctor
```

### 完整版（所有功能）
```bash
pip install -r requirements-full.txt
playwright install chromium  # 下载 Chromium 浏览器（~300MB）
mini-claw setup
mini-claw doctor
```

## 可选功能说明

### 向量检索（RAG）
- **功能**: 语义搜索、embedding 相似度检索
- **安装**: `pip install chromadb sentence-transformers`
- **何时需要**: `config.yaml` 中 `rag.embedding.enabled: true`

### 代码锚点（增量 reindex）
- **功能**: 基于 AST 的增量索引，只更新变化的函数
- **安装**: `pip install tree-sitter tree-sitter-language-pack`
- **何时需要**: 使用 `/context reindex <id> --incremental`

### 浏览器自动化
- **功能**: `/browser` skill 网页抓取
- **安装**: `pip install playwright && playwright install chromium`
- **为什么两步**？
  - `pip install playwright` 只装 Python 库（~10MB）
  - `playwright install chromium` 下载浏览器二进制（~300MB）
  - 浏览器存储在 `~/.cache/ms-playwright/`，不占 site-packages

## 常见问题

### `sentence-transformers not installed`
**解决**:
```bash
pip install chromadb sentence-transformers
```
或在 `config.yaml` 关闭 embedding：
```yaml
rag:
  embedding:
    enabled: false
```

### `tree-sitter` 相关错误
**解决**:
```bash
pip install tree-sitter tree-sitter-language-pack
```
或不使用 `--incremental` 标志。

### 飞书 WebSocket 连接失败
1. 检查 `app_id` / `app_secret` 是否正确
2. 确认应用已添加"接收消息"和"发送消息"权限
3. 查看 `logs/mini_claw.log` 详细错误

## 更多文档

- [README.md](README.md) - 架构和功能概览
- [LEARNING.md](LEARNING.md) - 187 个实现细节深度解析
