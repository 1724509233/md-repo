# md-sync

LAN 多设备 Markdown 文档同步系统。Win PC 跑 FastAPI 服务端，Mac/手机通过 Python 客户端或 Web UI 访问。

## 功能

- **文件同步** — SHA-256 哈希比对，冲突自动保留双方版本（`.conflict-{device}.md`）
- **Web 工作台** — 文件树浏览、Markdown 编辑/预览（marked.js + highlight.js + mermaid）、深色模式
- **AI Agent** — 基于 OpenAI 兼容 API 的工具调用 agent，可读写/搜索/移动/删除文档，写操作需用户确认
- **FTS5 检索** — SQLite FTS5 + jieba 中文分词，BM25 排序，增量索引
- **知识图谱** — wikilink/tag/related 关系抽取，关联文档发现
- **变更日志** — SQLite 事件溯源 change_log（seq/source/op），驱动 FTS5 + 图谱增量更新

## 架构

```
┌─────────────────────────────────────────────────┐
│  Win PC (Server)                                │
│  ┌───────────┐  ┌──────────┐  ┌─────────────┐  │
│  │ FastAPI   │  │ SQLite   │  │ Watchdog    │  │
│  │ :9888     │──│ index.db │──│ (fs events) │  │
│  └─────┬─────┘  └──────────┘  └─────────────┘  │
│        │                                        │
│  ┌─────▼──────────────────────────────────┐     │
│  │ Vault (Obsidian-compatible .md files)   │     │
│  └────────────────────────────────────────┘     │
└─────────────────────────────────────────────────┘
        ▲                    ▲
        │ HTTP               │ HTTP (polling)
  ┌─────┴─────┐       ┌─────┴──────┐
  │ Web UI    │       │ Python     │
  │ (browser) │       │ Client     │
  └───────────┘       │ (Mac/PC)   │
                      └────────────┘
```

## 快速开始

### 1. 安装

```bash
pip install -e ".[dev]"
```

### 2. 配置

复制 `config.example.yaml` 为 `config.yaml`，修改：

```yaml
# 服务端配置
server:
  host: 0.0.0.0
  port: 9888
  vault_dir: ./vault
  log_level: INFO  # DEBUG for verbose watcher event/IO logging

# 客户端配置
client:
  server_url: http://192.168.1.100:9888
  vault_dir: ./vault
  device_name: mac
  poll_interval: 300

# AI 配置 (可选) 
ai:
  base_url: "https://api.deepseek.com/v1"  # or http://localhost:11434/v1 for Ollama
  api_key: "your-api-key"
  model: "deepseek-v4-flash"
  max_tokens: 8192
  context_window: 262144
  temperature: 0.3

```

### 3. 启动服务端

```bash
python -m server.main config.yaml
# → http://localhost:9888
```

首次启动自动扫描 vault 中的 `.md` 文件建立索引。

### 4. 启动客户端（可选）

```bash
python -m client.main config.yaml
# → 每 300s 轮询同步
```

### 5. 打开 Web UI

浏览器访问 `http://<server-ip>:9888`，即可使用文件树、编辑器、AI 助手。

## Web UI 功能

| 功能 | 说明 |
|------|------|
| 文件树 | 递归目录结构，搜索过滤 |
| 编辑/预览 | 分段按钮切换，默认预览模式 |
| Markdown | marked.js 渲染 + Tailwind 样式 + highlight.js 代码高亮 + mermaid 图表 |
| AI 助手 | 右侧滑出面板，SSE 流式响应，工具调用卡片，写操作确认按钮 |
| 深色模式 | 🌙 切换，hljs 主题联动 |
| 自动保存 | 编辑后切换文档自动保存 |
| 变更轮询 | 10s 增量 + agent 工具执行后立即刷新 |

## AI Agent 工具

| 工具 | 权限 | 说明 |
|------|------|------|
| `read_doc` | 自动 | 读取文档内容 |
| `search_docs` | 自动 | FTS5 + jieba 中文搜索 |
| `list_files` | 自动 | 列出 vault 文件 |
| `get_related_docs` | 自动 | 图谱关联文档（wikilink/tag/related） |
| `recent_changes` | 自动 | 变更日志查询（谁改了什么，按 source 过滤） |
| `recall_stats` | 自动 | 召回遥测统计（sentinel 治理用） |
| `write_doc` | **需确认** | 创建/覆盖文档 |
| `edit_doc` | **需确认** | 精确文本替换 |
| `delete_doc` | **需确认** | 删除文档 |
| `move_docs` | **需确认** | 移动/重命名文档（批量） |

安全边界：路径不能越出 vault、仅限 `.md` 文件、`.sync/` 目录受保护。

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/docs` | 文件列表 |
| GET | `/api/docs/changes?since=<ts>` | 增量变更日志 |
| GET | `/api/docs/{path}` | 读取文档 |
| POST | `/api/docs/{path}` | 上传文档 |
| PUT | `/api/docs/{path}` | 保存文档 |
| POST | `/api/sync` | 客户端同步 |
| POST | `/api/ai/search` | FTS5 搜索 |
| GET | `/api/graph/backlinks?path=<p>` | 反向链接（谁引用了这篇） |
| GET | `/api/graph/related?path=<p>&hops=1` | 关联文档（N-hop） |
| POST | `/api/agent/chat` | Agent SSE 流式对话 |
| POST | `/api/agent/confirm` | 确认/拒绝写操作 |

## 测试

```bash
pytest tests/ -v          # 574 tests
```

## 项目结构

```
├── server/
│   ├── main.py              # 入口：config → db → app → watcher → uvicorn
│   ├── api.py               # FastAPI 路由工厂（create_app）
│   ├── sync.py              # manifest 对比（compare_files，无 IO 纯函数）
│   ├── snapshot_watcher.py  # SnapshotWatcher：doorbell + 脏集合 + 快照对比
│   ├── write_registry.py    # 写归因注册表（变更 source 归因）
│   ├── events.py            # 进程内事件总线（change / recall / 等）
│   ├── ai.py                # RAG 搜索 + LLM 流式
│   ├── mcp_server.py        # MCP stdio server（AI 助手接入）
│   ├── proposals.py         # 提案状态机（suggestions/ 提案区）
│   ├── recovery.py          # 恢复演练：证明 index.db 是可重建缓存
│   ├── logrotate.py         # 客户端旧日志定期清理
│   ├── config.py            # YAML 配置解析
│   ├── vault/               # 索引子系统：db/（SQLite+FTS5+图谱）· fs/（切块+链接解析）· orchestration/（索引+refs+移动检测）；公共 API = service.py
│   ├── agent/               # LLM agent：tools/（10 工具）· runtime · session · policy · reminders
│   ├── bgagent/             # 后台 agent：scheduler（cron+arming）· runner · notify · executor
│   ├── prompts/             # bgagent 角色提示词（sentinel / consolidator / narrator）
│   └── templates/
│       ├── index.html       # Web UI（单文件，Tailwind CDN）
│       └── style.css        # 自定义样式
├── client/
│   ├── main.py              # 轮询循环入口
│   └── sync.py              # 扫描 + 同步逻辑
├── tests/                   # pytest 套件（574 tests）
└── docs/                    # 设计文档 + 实施计划（superpowers/specs · plans）
```

## MCP Server（接入 OpenCode 等 AI 助手）

MCP server 是独立的 stdio 进程，给 AI 助手提供 `search_docs` / `read_doc` / `write_doc` / `edit_doc` / `get_related_docs` 等工具，直接读写 vault。

### 1. 安装

```bash
pip install -e ".[mcp]"        # 生成 md-vault-mcp 命令
which md-vault-mcp             # 确认在 PATH 中（如 .venv/bin/md-vault-mcp）
```

### 2. 注册到 OpenCode

编辑 `~/.config/opencode/opencode.json`，在 `mcp` 段追加：

```json
{
  "mcp": {
    "md-vault": {
      "type": "local",
      "command": ["md-vault-mcp"],
      "enabled": true
    }
  }
}
```

> 若 `md-vault-mcp` 不在系统 PATH（如装在 venv 里），把 `command` 改成绝对路径：
> `["/path/to/.venv/bin/md-vault-mcp"]`。

### 3. 追加 AGENTS.md 提示词

在项目 `AGENTS.md`（或 `~/.config/opencode/AGENTS.md`）加入，让 agent 优先查 vault 而非 web 搜索：

````markdown
## MD Vault

A personal knowledge base synced across devices. Use the `md-vault` MCP tools
BEFORE web search or memory recall when you need project context, prior
decisions, or development notes:

- **search_docs**: Find relevant documents by keyword (FTS5 + Chinese support)
- **read_doc**: Read full document content
- **get_related_docs**: Discover related documents via wikilinks/tags
- **write_doc**: Create or update a knowledge note
- **edit_doc**: Precise text replacement in an existing document

If the MCP server is not available, skip vault access — do not fall back to
reading vault files directly.

### MCP Server Restart

The `md-vault` MCP server is a long-running stdio process launched by OpenCode
(`command: ["md-vault-mcp"]` in `~/.config/opencode/opencode.json`). Code changes
to `server/mcp_server.py` or `server/agent.py` do NOT take effect until the
process is restarted. To restart:

1. Run `/mcp` in OpenCode to see the server list and reconnect, OR
2. Restart the OpenCode session entirely.

After a restart, verify with a quick `write_doc` + `read_doc` round-trip.
````

### 4. 重启生效

MCP server 是 OpenCode 启动时拉起的长驻进程，改完配置或代码后需重启：OpenCode 里跑 `/mcp` 重连，或整个会话重启。用一次 `write_doc` + `read_doc` 往返验证。

## 待办

- [ ] 拆分 server / client 为独立发布的包（当前 `pip install md-sync` 一并安装两者，client 用户被迫带上 fastapi/openai 等 server 依赖）

## 设计约束

- **无认证、无 HTTPS** — LAN 专用，不暴露公网
- **仅同步 `.md` 文件** — 不做版本历史
- **删除不同步** — 客户端删除的文件服务端保留，下次同步重新拉取
- **冲突策略** — 双方都修改时生成 `.conflict-{device}.md`，本地版本不动

## 技术栈

Python 3.10+ · FastAPI · uvicorn · SQLite (FTS5) · jieba · watchdog · httpx · openai SDK · Tailwind CSS · Flowbite · marked.js · highlight.js · mermaid
