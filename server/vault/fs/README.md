# server/vault/fs

Vault 文件系统层：Markdown 分段/分词、wikilink 解析、文件编辑与遍历。本模块**不碰数据库**——纯文本处理 + 磁盘 I/O，是 `db/`（索引）与 `orchestration/`（增量调度）之下的底层依赖。

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `segmenter.py` | Markdown 分段（frontmatter/代码块/正文）+ jieba 分词，纯文本处理 |
| `link_parser.py` | wikilink 目标解析（Obsidian 短名）与链接展开为纯文本；模块级解析缓存 |
| `vault_ops.py` | 精确字符串替换编辑（`edit_file`）与 vault 遍历（`_iter_vault_md`） |

## segmenter.py — 分段与分词

### `segment_markdown(content) -> list[dict]`

把 Markdown 拆成 `{"kind", "content", "heading_path"}` 分段：

- **kind**：`meta`（YAML frontmatter）、`code`（``` 代码块）、`text`（正文）
- **heading_path**：完整标题面包屑，如 `Python 异步编程 > 事件循环`（按 `#` 层级维护栈）
- 超过 400 字符的正文按空行切段（`_split_long_text`），保证 FTS 命中粒度

### `_tokenize_for_index(text) -> str`

jieba `cut_for_search` 分词 → 小写 → 过滤内置中文停用词（约 60 个高频虚词）→ 空格拼接。
**入库和查询两侧都必须走这个函数**，否则 FTS5 匹配不上（见 `db/segments_sql.py`）。

## link_parser.py — wikilink 解析

### `_resolve_wikilink(vault_dir, target) -> str | None`

解析顺序：

1. vault 根相对路径精确命中（`notes/a.md`）
2. 按文件名全库 rglob（Obsidian 短名 `[[MOC]]`）——**仅唯一匹配时返回**，歧义返回 `None`

无扩展名的目标自动补 `.md`。rglob 结果按文件名缓存在模块级 `_wikilink_cache`（N 个链接只扫一次库）；全量重建入口（`rebuild_segments_all` / `graph_rebuild_all`）开头调用 `_clear_wikilink_cache()` 强制重扫。

### `_expand_wikilinks(content) -> str`

把 `[[notes/target|alias]]` 展开为 `"notes target alias"` 纯文本，让链接目标与别名也参与 FTS 索引。

## vault_ops.py — 编辑与遍历

### `edit_file(vault_dir, path, old_string, new_string) -> str`

精确字符串替换，**无 replace_all**：

- 未找到 → 返回错误串（提示重新 `read_doc`）
- 多处匹配 → 返回错误串（要求补充上下文使匹配唯一）
- 恰好一处 → 替换写回，返回确认串；`new_string` 为空即删除

以 `read_bytes`/`write_bytes` 读写，避免 Windows 换行翻译破坏内容。错误/确认串直接面向 Agent 消费（`server/agent/execute.py`、`mcp_server.py` 的 `edit_doc` 工具）。

### `_iter_vault_md(vault_dir) -> Iterator[Path]`

rglob 全部 `.md`，跳过 `.sync/` 与 `.obsidian/`。全库扫描的唯一入口（indexer、graph 回退路径共用）。

## 依赖方向

```
orchestration/indexer.py ──► segmenter / link_parser / vault_ops
db/graph_sql.py          ──► vault_ops._iter_vault_md   (唯一 db→fs 回退，见 db/README)
server/ai.py             ──► segmenter._tokenize_for_index (查询侧分词)
```
