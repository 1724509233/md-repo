# server/vault/fs

Vault 文件系统层：Markdown 分段/分词、wikilink 解析、文件编辑与遍历。本模块**不碰数据库**——纯文本处理 + 磁盘 I/O，是 `db/`（索引）与 `orchestration/`（协调）之下的底层依赖，纯函数优先（三层拆分源于 db 层重构：spec `~/md-vault/projects/md-repo/specs/2026-10-10-db-layer-refactor-design.md`）。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `segmenter.py` | Markdown 分段（frontmatter/代码块/正文）+ jieba 分词，纯文本处理 |
| `link_parser.py` | wikilink 目标解析（Obsidian 短名）与链接展开为纯文本；模块级解析缓存 |
| `vault_ops.py` | 路径安全守卫（`safe_rel_path`）、精确字符串替换编辑（`edit_file`）、vault 遍历（`_iter_vault_md`）、原子写（`atomic_write_bytes`） |

## 公共 API

### segmenter.py — 分段与分词

#### `segment_markdown(content) -> list[dict]`

把 Markdown 拆成 `{"kind", "content", "heading_path"}` 分段：

- **kind**：`meta`（YAML frontmatter）、`code`（``` 代码块）、`text`（正文）
- **heading_path**：完整标题面包屑，如 `Python 异步编程 > 事件循环`（按 `#` 层级维护栈）
- 超过 400 字符的正文按空行切段（`_split_long_text`），保证 FTS 命中粒度

#### `_tokenize_for_index(text) -> str`

jieba `cut_for_search` 分词 → 小写 → 过滤内置中文停用词（约 60 个高频虚词）→ 空格拼接。**入库侧必须走这个函数**（调用点在 `orchestration/indexer._index_one_file`）；查询侧由 `db/segments_sql._query_tokens` 完成——同 jieba 模式、共享同一 `_STOPWORDS`，两侧 token 才可比较（见 `db/README.md` 分层召回）。

### link_parser.py — wikilink 解析

#### `_resolve_wikilink(vault_dir, target) -> str | None`

解析顺序：

1. vault 根相对路径精确命中（`notes/a.md`）
2. 按文件名全库 rglob（Obsidian 短名 `[[MOC]]`）——**仅唯一匹配时返回**，歧义返回 `None`

无扩展名的目标自动补 `.md`。rglob 结果按文件名缓存在模块级 `_wikilink_cache`（N 个链接只扫一次库）；全量重建入口（`rebuild_segments_all` / `graph_rebuild_all`）开头调用 `_clear_wikilink_cache()` 强制重扫。

#### `_expand_wikilinks(content) -> str`

把 `[[notes/target|alias]]` 展开为 `"notes target alias"` 纯文本，让链接目标与别名也参与 FTS 索引。

### vault_ops.py — 路径安全、编辑与遍历

#### `safe_rel_path(raw) -> Path | None`

**上传/下载路径安全的单入口**：反斜杠归一为 `/`；拒绝空路径、前导 `/`、任何 `..` 或空 path part；安全则返回 vault 相对 `Path`，否则 `None`。

#### `edit_file(vault_dir, path, old_string, new_string) -> str`

精确字符串替换，**无 replace_all**：

- 未找到 → 返回错误串（提示重新 `read_doc`）
- 多处匹配 → 返回错误串（要求补充上下文使匹配唯一）
- 恰好一处 → 替换写回，返回确认串；`new_string` 为空即删除

以 `read_bytes`/`write_bytes` 读写，避免 Windows 换行翻译破坏内容。错误/确认串直接面向 Agent 消费（`server/agent/execute.py`、`mcp_server.py` 的 `edit_doc` 工具）。

#### `_iter_vault_md(vault_dir) -> Iterator[Path]`

rglob 全部 `.md`，跳过 `.sync/`、`.obsidian/`，以及顶层 `suggestions/`、`index/`（`EXCLUDED_INDEX_DIRS`——memory-runtime v1 索引排除；只匹配顶层，更深层同名目录是用户内容）。全库扫描的唯一入口（indexer、graph 回退路径共用）。

#### `atomic_write_bytes(fp, data) -> None`

同目录 dot 前缀非 `.md` 临时文件 + `os.replace` 原子替换：两个 walker（client `scan_local_files` 与 vault walk，均 rglob("*.md")）永远看不到临时文件；崩溃残留是惰性的，无需清理。异常时清理临时文件后重新抛出。

## 关键设计

- **分段策略**：frontmatter / 代码块 / 正文三类分段，标题栈维护 heading_path 面包屑；长正文按空行聚合到 ≤400 字符，控制 FTS 命中粒度而不切碎段落。
- **FTS 分词输入**：入库侧 `_tokenize_for_index`（jieba `cut_for_search` + 停用词 + 小写，单字 token 丢弃）；查询侧 `db/segments_sql._query_tokens` 镜像同一规则（fuzzy 档例外保留单字 CJK 以 char\* 前缀兜底）。两侧必须同规则，否则 FTS5 匹配不上。
- **引用解析回退**：`_resolve_wikilink` 歧义返回 `None`（宁缺勿错）；rglob 结果模块级缓存，全量重建入口显式清缓存。`db/graph_sql._find_referrers` 在图谱未构建时回退本模块 `_iter_vault_md` 全库扫描——文档化的、被接受的唯一 db→fs 回退（引用维护设计：spec `~/md-vault/projects/md-repo/specs/2026-10-07-edges-based-ref-update-design.md`；详见 `db/README.md`）。

## 依赖方向

```
（举例——实际 import 面更大）
orchestration/indexer.py ──► segmenter / link_parser / vault_ops
orchestration/refs.py    ──► （经 db/graph_sql._find_referrers 间接依赖 vault_ops._iter_vault_md 回退）
db/graph_sql.py          ──► vault_ops._iter_vault_md   (唯一 db→fs 回退，见 db/README)
routes/*                 ──► safe_rel_path   (上传/下载路径守卫；另 bgagent/executor)
mcp_server               ──► edit_file
agent tools / bgagent / snapshots ──► vault_ops.edit_file / atomic_write_bytes / safe_rel_path
```

（base 层：只被 orchestration / db 回退 / 上层写入面 import，不反向依赖。）

## 注意事项

- **`.sync/` 与 `.obsidian/` 排除**：`_iter_vault_md` 与 `_resolve_wikilink` 的 rglob 均跳过这两类目录；顶层 `suggestions/`、`index/` 只做分段/图谱排除（files 表与 change_log 仍维护）。
- **纯 `.md` 遍历**：两个 rglob 面的 walker 只认 `.md`——`atomic_write_bytes` 的临时文件因此不可见（dot 前缀 + 非 `.md` 后缀）。
- **原子写**：先写 tmp 再 `os.replace`（同目录 = 同卷，替换原子）；不要在别处自造「直接 `write_bytes` 覆盖」的写路径来绕过它（Agent 快照/回滚、bgagent 写入均已走此函数）。
- **Windows 换行**：`edit_file` 用字节读写避免换行翻译；新增测试 fixture 里的文本文件同理用 `write_bytes`（哈希一致性，见仓库 AGENTS.md）。
