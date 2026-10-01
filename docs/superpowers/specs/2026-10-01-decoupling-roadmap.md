# 解耦路线图

> 状态：规划中，待 agent 功能验证稳定后实施。

## 当前耦合点

```
server/
├── api.py        ← 路由 + 会话管理 + 业务逻辑混在一起
├── agent.py      ← 工具定义 + 执行 + LLM loop（直接操作 vault_dir）
├── ai.py         ← RAG 搜索 + LLM streaming（直接操作 vault_dir）
├── db.py         ← SQLite 索引
├── sync.py       ← 文件同步
└── config.py     ← 配置解析
```

核心问题：agent 和 ai 直接持有 `vault_dir` + `db_conn`，未来加知识图谱、换 RAG 引擎都要改它们。

## 目标分层

```
server/
├── api.py          ← 纯路由，只做参数解析 + 调用 service 层
├── config.py       ← 不变
│
├── vault/          ← 数据访问层（唯一接触文件系统 + SQLite 的地方）
│   ├── __init__.py
│   ├── store.py    ← VaultStore: read/write/list/delete/move/search (统一接口)
│   └── index.py    ← SQLite 索引操作（从 db.py 迁入）
│
├── services/       ← 业务逻辑层（无 HTTP 概念，纯 async）
│   ├── __init__.py
│   ├── sync.py     ← 同步逻辑（从 client/sync.py 的服务端部分）
│   ├── agent.py    ← agent loop（依赖 VaultStore，不直接碰文件系统）
│   ├── rag.py      ← 检索（可替换：keyword → vector → graph）
│   └── llm.py      ← LLM 客户端封装（OpenAI-compatible）
│
└── templates/
```

## 关键接口

```python
# vault/store.py
class VaultStore:
    async def read(self, path: str) -> str
    async def write(self, path: str, content: str)
    async def delete(self, path: str)
    async def move(self, from_path: str, to_path: str)
    async def list(self, dir: str = "") -> list[str]
    async def search(self, query: str, top_k: int = 5) -> list[SearchResult]
```

## 收益

- Agent 只依赖 `VaultStore`，不关心底层是文件系统还是数据库
- RAG 可独立替换（keyword → embedding → 知识图谱），agent 无感知
- 知识图谱可以作为 `VaultStore.search` 的增强实现，或独立的 `services/graph.py`
- 测试时 mock `VaultStore` 即可，不需要真实文件系统

## 迁移成本

中等。主要是把 `agent.py` 和 `ai.py` 中的文件操作替换为 `VaultStore` 调用，`api.py` 瘦身为纯路由。现有测试大部分可以复用。

## 实施前提

- [ ] Agent 功能浏览器验证通过
- [ ] 前端修复提交
- [ ] 确认无紧急功能需求插入
