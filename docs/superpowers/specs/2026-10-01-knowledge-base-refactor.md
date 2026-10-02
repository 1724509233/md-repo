# md-sync 重构 Spec：知识底座演进

## 背景

当前系统是一个 LAN Markdown 同步工具，核心场景已演变为**知识沉淀 → 检索召回 → 关联发现**的数据飞轮。文件量小（<200）、单人、单 agent，但会持续增长。

本 spec 只覆盖**有明确场景驱动**的重构项，拒绝过度设计。

---

## 做与不做的判断

| 项目 | 判断 | 理由 |
|------|------|------|
| change_log 升级 | **做** | 事件总线，所有消费者的基础设施 |
| FTS5 检索 | **做** | 飞轮瓶颈：知识沉淀了但搜不到 = 飞轮转不起来 |
| 知识图谱（轻量） | **做** | 飞轮"关联"环节：FTS5 搜不到的，图谱能带出来 |
| 增量同步 | **缓做** | 文件少时全量扫盘无感，vault > 500 再做 |
| Agent 管理模块 | **不做** | 单 agent，权限矩阵/注册/审计表是过度设计 |
| PWA / 离线队列 | **不做** | 浏览器打开够用，无场景驱动 |
| 向量检索 + RRF | **不做** | 无 embedding API，FTS5 够用 |
| 目录结构大改 | **不做** | 7 个文件拆 20+ 个，认知负担 > 收益 |
| tombstone / 冲突审批队列 | **不做** | 单人使用，删除不同步 by design |

---

## Phase 1：change_log 升级

### 目标

把当前 `file_changes` 表升级为完整的事件溯源日志，作为所有消费者的统一事件源。

### 现状

```sql
CREATE TABLE file_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  path TEXT NOT NULL,
  action TEXT NOT NULL,   -- created | modified | deleted | moved
  from_path TEXT
);
```

### 目标 schema

```sql
CREATE TABLE change_log (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL    NOT NULL,
  source      TEXT    NOT NULL,   -- agent | web | watchdog | client
  op          TEXT    NOT NULL,   -- create | update | delete | move
  path        TEXT    NOT NULL,
  old_path    TEXT,               -- move 时记录
  new_hash    TEXT                -- SHA-256，用于增量判断
);
CREATE INDEX idx_changelog_ts ON change_log(ts);
CREATE INDEX idx_changelog_path ON change_log(path);
```

### 变更点

| 项 | 说明 |
|----|------|
| `id` → `seq` | 语义更明确，单调递增用于 offset 管理 |
| `action` → `op` | 统一命名（create/update/delete/move） |
| `from_path` → `old_path` | 统一命名 |
| 新增 `source` | 谁触发的变更（agent/web/watchdog/client） |
| 新增 `new_hash` | 文件内容哈希，消费者可判断是否需要重新索引 |
| 表名 `file_changes` → `change_log` | 语义升级 |

### 向后兼容

- 现有 `log_change()` / `get_changes()` 函数签名不变，内部映射到新表
- Web 前端 `pollChanges()` 接口不变（`GET /api/docs/changes?since=<ts>`）
- 迁移：启动时检测旧表存在 → `ALTER TABLE` 或 `CREATE + INSERT SELECT`

### 价值

- **事件总线**：FTS5、图谱、审计、分析都订阅同一个日志
- **审计**：谁在什么时候改了什么，天然持久化
- **选择性重建**：从某个 seq 开始重放，不全量扫描
- **变更分析**：哪些文档活跃、agent 贡献度、增长趋势

### 改动范围

- `server/db.py` — schema 迁移 + `log_change()` / `get_changes()` 适配
- `server/agent.py` — `log_change()` 调用加 `source="agent"`
- `server/api.py` — web save 加 `source="web"`
- `server/watcher.py` — 加 `source="watchdog"`
- `tests/test_db.py` — 更新测试

---

## Phase 2：FTS5 检索

### 目标

用 SQLite FTS5 替代当前 TF 关键词重叠，解决中文召回差的核心痛点。

### 现状

`server/ai.py` 的 `rag_search()`：
- 手动 tokenize（按字符/空格切）
- TF 重叠评分
- 中文召回差（"FastAPI 路由冲突" 搜不到 "FastAPI 路径匹配踩坑"）

### 方案

#### 2.1 FTS5 虚拟表

```sql
CREATE VIRTUAL TABLE docs_fts USING fts5(
  path,
  title,
  body,
  tokenize='unicode61 remove_diacritics 2'
);
```

#### 2.2 中文分词

- 写入前用 `jieba` 分词，空格拼接后入库
- 查询同样 jieba 分词
- 新增依赖：`jieba>=0.21`

#### 2.3 索引更新（change_log consumer）

```python
def update_fts(conn, event):
    if event.op == "delete":
        conn.execute("DELETE FROM docs_fts WHERE path = ?", (event.path,))
    else:
        content = read_file(event.path)
        tokens = " ".join(jieba.cut(content))
        conn.execute("DELETE FROM docs_fts WHERE path = ?", (event.path,))
        conn.execute("INSERT INTO docs_fts(path, title, body) VALUES (?, ?, ?)",
                     (event.path, extract_title(content), tokens))
```

触发点：
- 启动时全量索引（首次）
- change_log 增量更新（watchdog / agent / web save 后）

#### 2.4 搜索接口

`rag_search()` 签名不变，内部替换：

```python
def rag_search(conn, query, top_k=5):
    tokens = " ".join(jieba.cut(query))
    rows = conn.execute(
        "SELECT path, snippet(docs_fts, 2, '[', ']', '...', 32) as snip, "
        "rank FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?",
        (tokens, top_k)
    ).fetchall()
    return [{"path": r[0], "snippet": r[1], "score": -r[2]} for r in rows]
```

#### 2.5 Agent 工具

`search_docs` 工具 schema 不变，底层自动切到 FTS5。

### 价值

- **飞轮瓶颈解除**：知识沉淀了能搜到，飞轮才能转
- **中文召回**：jieba 分词 + FTS5 BM25 排序，远优于 TF 重叠
- **snippet 高亮**：FTS5 内置 `snippet()` 函数，搜索结果带上下文
- **Agent 受益**：`search_docs` 工具召回质量直接提升

### 改动范围

- `server/db.py` — FTS5 表 + 索引函数
- `server/ai.py` — `rag_search()` 内部替换
- `server/watcher.py` / `server/agent.py` / `server/api.py` — 变更后触发 FTS5 更新
- `pyproject.toml` — 加 `jieba`
- `tests/test_ai.py` — 更新 RAG 测试

---

## Phase 3：知识图谱（轻量抽取）

### 目标

从 Markdown 中抽取 wikilink/tag/related 关系，构建文档关联图谱。解决 FTS5 做不到的"你没搜但相关的也给你"。

### 场景

- 写了 "FastAPI 路由冲突踩坑"，里面 `[[wikilink]]` 了 "FastAPI 中间件"
- 三个月后搜 "路由"，图谱带出关联的 "FastAPI 中间件" 和 "路径匹配规则"
- Agent 回答时能引用关联文档，而不只是关键词命中的那一篇

### 方案

#### 3.1 表设计

```sql
CREATE TABLE entities (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  name  TEXT NOT NULL,
  type  TEXT NOT NULL,   -- doc | tag | concept
  path  TEXT             -- doc 类型时指向文件路径
);
CREATE UNIQUE INDEX idx_entities ON entities(name, type);

CREATE TABLE edges (
  src_id  INTEGER NOT NULL,
  dst_id  INTEGER NOT NULL,
  type    TEXT NOT NULL,   -- wikilink | tag | related
  path    TEXT,            -- 来源文档路径
  PRIMARY KEY (src_id, dst_id, type)
);
CREATE INDEX idx_edges_src ON edges(src_id);
CREATE INDEX idx_edges_dst ON edges(dst_id);
```

#### 3.2 抽取规则（写入时同步做）

| 模式 | 抽取 |
|------|------|
| `[[target]]` 或 `[[target\|alias]]` | edge(src=当前文档, dst=target, type=wikilink) |
| `#tag`（行首或空格后） | edge(src=当前文档, dst=tag, type=tag) |
| frontmatter `related: [a, b]` | edge(src=当前文档, dst=a/b, type=related) |

抽取逻辑在 FTS5 consumer 里一起跑（同一个 change_log subscriber）。

#### 3.3 API

```
GET /api/graph/backlinks?path=notes/a.md
→ [{"path": "notes/b.md", "type": "wikilink"}]

GET /api/graph/related?path=notes/a.md&hops=1
→ [{"path": "notes/b.md", "type": "wikilink", "score": 0.8},
   {"path": "notes/c.md", "type": "tag", "score": 0.5}]
```

#### 3.4 Agent 工具

新增 `get_related_docs`：

```json
{
  "name": "get_related_docs",
  "description": "获取与指定文档关联的其他文档（wikilink/tag/related）",
  "parameters": {
    "path": {"type": "string", "description": "文档路径"}
  }
}
```

自动执行（read-only），无需确认。

#### 3.5 Web UI（可选，第二版）

文档预览底部显示 "相关文档" 链接列表。第一版可以不加。

### 价值

- **飞轮"关联"环节**：FTS5 是"你搜什么给你什么"，图谱是"你没搜但相关的也给你"
- **Agent 上下文增强**：回答时带出关联文档，知识密度更高
- **知识发现**：沉淀的经验通过 wikilink 串联，形成知识网络

### 改动范围

- `server/db.py` — entities/edges 表 + 抽取函数
- `server/api.py` — 2 个 GET 端点
- `server/agent.py` — 1 个工具 schema + 执行
- FTS5 consumer 里加图谱抽取（共享 change_log subscriber）
- `tests/` — 图谱测试

---

## 依赖关系

```
Phase 1 (change_log)
    │
    ├──→ Phase 2 (FTS5) ──→ Phase 3 (图谱)
    │         │                    │
    │         └── 共享 consumer ──┘
    │
    └──→ 审计（免费获得，不需要额外开发）
```

- Phase 1 是 Phase 2 和 3 的**前置依赖**（事件源）
- Phase 2 和 3 共享同一个 change_log consumer，但**独立交付**
- Phase 3 依赖 Phase 2 的 consumer 框架（在 FTS5 更新逻辑旁边加图谱抽取）

---

## 执行顺序

| 阶段 | 内容 | 交付物 | 预估改动量 |
|------|------|--------|-----------|
| 1 | change_log 升级 | 新表 + 迁移 + 调用点适配 | ~100 行 |
| 2 | FTS5 检索 | FTS5 表 + jieba 分词 + consumer + rag_search 替换 | ~200 行 |
| 3 | 知识图谱 | entities/edges + 抽取 + API + Agent 工具 | ~250 行 |

每阶段独立可交付，TDD。

---

## 非目标

- 不做增量同步（vault > 500 文件时再考虑）
- 不做 Agent 管理模块（单 agent）
- 不做 PWA / 离线队列
- 不做向量检索 / RRF
- 不做目录结构大改
- 不做 tombstone / 冲突审批队列
- 不做 LLM 重型图谱抽取（第一版只做规则抽取）

---

## 风险

| 风险 | 缓解 |
|------|------|
| jieba 分词器版本升级改变索引 | 固定版本号，升级时全量重建 FTS5 |
| FTS5 首次全量索引慢（大 vault） | 启动时异步索引，不阻塞服务启动 |
| 图谱 wikilink target 不存在 | 抽取时检查路径是否存在，不存在则跳过 |
| change_log 膨胀 | 当前规模无感；未来可加归档（>N 天合并为快照） |
