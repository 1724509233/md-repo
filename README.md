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
server:
  vault_dir: D:/md-vault    # 你的 Obsidian vault 路径

client:
  server_url: http://192.168.1.100:9888
  vault_dir: ./vault
  device_name: mac

ai:                           # 可选，不配则 AI 功能不可用
  base_url: "https://api.deepseek.com/v1"
  api_key: "your-key"
  model: "deepseek-chat"
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
| `write_doc` | **需确认** | 创建/覆盖文档 |
| `delete_doc` | **需确认** | 删除文档 |
| `move_doc` | **需确认** | 移动/重命名文档 |

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
pytest tests/ -v          # 72 tests
```

## 项目结构

```
├── server/
│   ├── main.py          # 入口：config → db → app → uvicorn
│   ├── api.py           # FastAPI 路由工厂
│   ├── agent.py         # Agent 工具调用 + SSE 流
│   ├── ai.py            # RAG 搜索 + LLM 流式
│   ├── config.py        # YAML 配置解析
│   ├── db.py            # SQLite 索引 + change_log + FTS5 + 图谱
│   ├── watcher.py       # watchdog 文件系统监听
│   └── templates/
│       ├── index.html   # Web UI（单文件，Tailwind CDN）
│       └── style.css    # 自定义样式
├── client/
│   ├── main.py          # 轮询循环入口
│   └── sync.py          # 扫描 + 同步逻辑
├── tests/               # pytest 套件（63 tests）
└── docs/superpowers/    # 设计文档 + 实施计划
```

## 设计约束

- **无认证、无 HTTPS** — LAN 专用，不暴露公网
- **仅同步 `.md` 文件** — 不做版本历史
- **删除不同步** — 客户端删除的文件服务端保留，下次同步重新拉取
- **冲突策略** — 双方都修改时生成 `.conflict-{device}.md`，本地版本不动

## 技术栈

Python 3.10+ · FastAPI · uvicorn · SQLite (FTS5) · jieba · watchdog · httpx · openai SDK · Tailwind CSS · Flowbite · marked.js · highlight.js · mermaid
