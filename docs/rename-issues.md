# Vault 重命名处理流程 — 已知问题

> 关联：`docs/superpowers/specs/2026-09-27-md-sync-design.md`、watchdog-design（vault 笔记）

> 2026-10-06 梳理 vault 文件/文件夹重命名完整处理流程时发现。

## 问题清单

| # | 问题 | 严重度 | 类型 | 状态 | 修复提交 |
|---|------|--------|------|------|----------|
| 1 | Agent move_doc 双重调用 _update_references | 低 | 性能冗余 | fixed | `9c87bcf` |
| 2 | 短名 wikilink 文件名变更时不修复 | 中 | 功能缺陷 | fixed | `d3308b9` |
| 3 | 目录重命名 N 次全 vault 扫描 | 中 | 性能 | fixed | `4dbfd2c` |
| 4 | 客户端重命名同步 → 重复文件 | **高** | 数据完整性 | fixed | `470b9a6` |
| 5 | _update_references 纯文本替换无边界检查 | 低-中 | 内容安全 | fixed | `8fb168e` |
| 6 | _self_heal 非唯一匹配静默跳过 | 低 | 边缘场景 | fixed | 见下方 |

**修复优先级**：#4 > #2 > #3 > #5 > #1 > #6（已全部修复）

---

## #4 客户端重命名同步 → 重复文件（高）

**位置**：`client/sync.py:117-145` + `server/api.py:126-143`

**流程**：
```
重命名后 DB: files = {new_path: hash}
客户端本地:  local_files = {old_path: hash}  # 客户端不知道重命名

compare_files(local, server):
  to_pull = [new_path]   # 服务端有、客户端没有 → 拉取
  to_push = [old_path]   # 客户端有、服务端没有 → 推送
```

客户端执行：
1. pull new_path → 下载新路径文件到本地 ✓
2. push old_path → `POST /api/docs/{old_path}` → 服务端 `upload_doc` 在 vault 中**重新创建旧路径文件**

```python
# server/api.py:133-134 — upload_doc 无条件写文件
file_path.parent.mkdir(parents=True, exist_ok=True)
file_path.write_bytes(content)  # ← 旧路径文件被重新创建
```

**结果**：
- vault 中同时存在 `old_path` 和 `new_path`（内容相同）
- watchdog 收到 `on_created(old_path)` → `apply_change("modified")` → files 表插入旧路径
- 下次同步：旧路径又在 `to_pull` 中
- **重复文件永久存在**，除非用户手动删除

**根因**：`compare_files` 只做路径集合对比，无法识别"同内容不同路径 = 重命名"。AGENTS.md 中 "Deletion is not synced" 设计只覆盖了删除场景，没有覆盖重命名。

**修复**（`470b9a6`）：
- `compare_files` 增加 `recent_moves` 参数：用 change_log 中客户端上次同步后的 moved 事件识别重命名，将 `to_push` 中的旧路径移入 `to_delete`
- `/api/sync` 读请求体 `last_sync`，查 change_log moved 事件传给 `compare_files`
- 客户端请求带 `last_sync`，处理 `to_delete`（删除本地旧文件）
- `main.py` 新增 `_detect_duplicates()` 启动扫描，log warning 发现重复内容
- 向后兼容：旧客户端不传 `last_sync` / 不处理 `to_delete` 时行为同现状

---

## #2 短名 wikilink 文件名变更时不修复（中）

**位置**：`db.py:847-886` `_update_references`

**现状**：
```python
# 只替换完整路径形式的 wikilink
content = re.sub(
    r"\[\[(" + escaped_old + r")(\|[^\]]+)?\]\]",  # [[notes/a.md]] → [[docs/b.md]]
    ...
)
# 纯文本路径替换（仅含 / 时）
if "/" in old_path:
    content = content.replace(old_path, new_path)
```

**不覆盖的场景**：
- Obsidian 短名链接 `[[a]]`（无路径）→ 文件名从 `a.md` 改为 `b.md` 后，`[[a]]` 不会被替换为 `[[b]]`
- `_resolve_wikilink`（db.py:587）在查询时做 vault-wide filename search，重命名后 `[[a]]` 解析不到 `b.md`（文件名变了）
- 结果：短名链接**静默失效**，不报错、不修复

**影响**：用户用 Obsidian 短名链接（`[[MOC]]`、`[[a]]`）的场景下，重命名后链接断裂，无感知。

**修复**（`d3308b9`）：
- 新增 `_is_filename_unique()`：检查文件名在 vault 中是否唯一
- `_update_references` 增加短名 wikilink 替换：`[[old_stem]]` → `[[new_stem]]`，仅当旧文件名唯一且 stem 变化时执行
- 目录重命名（同 stem）不替换短名链接（仍然有效）

---

## #3 目录重命名 N 次全 vault 扫描（中）

**位置**：`watcher.py:146-153` + `db.py:813`

**现状**：
```python
# watcher.py:146 — 目录重命名展开为 N 个文件级 apply_change
for md in new_root.rglob("*.md"):
    apply_change(conn, vault_dir, new_rel, "moved", source=source, from_path=old_rel)
    # ↑ 每个文件内部都调 _update_references → 全 vault rglob + 全文读取
```

**影响**：
- 目录含 N 个 `.md` 文件 → N 次 `vault_dir.rglob("*.md")` + N 次全文读取 + N 次正则替换
- 100 个文件的目录重命名 = 100 次全 vault 扫描
- 大 vault（500+ 文件）下单次目录重命名可能耗时数秒到数十秒
- 全部在 watchdog 线程内同步执行，阻塞后续事件处理

**修复**（`4dbfd2c`）：
- `apply_change` 增加 `update_refs=True` 参数，目录重命名时设为 False
- 新增 `_update_references_batch()`：单次全 vault 扫描，预编译所有 move 的正则，一次替换所有引用
- `watcher.py` 目录重命名分支：收集 moves → `apply_change(update_refs=False)` → 一次 `_update_references_batch`
- N 个文件从 N 次扫描降为 1 次

---

## #5 _update_references 纯文本替换无边界检查（低-中）

**位置**：`db.py:879-880`

**现状**：
```python
if "/" in old_path:
    content = content.replace(old_path, new_path)  # 字符串直接替换，无边界检查
```

**影响**：
- `content.replace("notes/a", "docs/b")` 会替换所有出现 `notes/a` 的位置
- 如果文档中有 `notes/a.md`、`notes/a.md.bak`、`see notes/a for details`，全部被替换
- 没有词边界检查，可能误替换非路径引用
- 例如：`notes/abc.md` 包含子串 `notes/a`（如果 old_path 是 `notes/a`），会被误替换

**修复**（`8fb168e`）：
- `str.replace` 改为 `re.sub` + 负向前瞻 `(?![\w./])`，确保匹配后不紧跟路径字符
- `_update_references` 和 `_update_references_batch` 同步修复

---

## #1 Agent move_doc 双重调用 _update_references（低）

**位置**：`agent.py:360` + `db.py:813`

**现状**：
```python
# agent.py:358-360 — 第一次调用（立即）
register_write(args["to_path"], "agent")
src.rename(dst)
ref_count = _update_references(vault_dir, args["from_path"], args["to_path"])

# db.py:813 — 第二次调用（watchdog on_moved → apply_change 内部）
_update_references(vault_dir, from_path, path)
```

**影响**：
- 第一次调用扫描全 vault 替换引用，写文件
- watchdog 收到 `on_moved` 后，`apply_change` 内部又扫描全 vault
- 第二次扫描时引用已被替换，正则无匹配，不写文件 → 幂等但冗余
- 大 vault 下两次全量 rglob + 全文读取的 IO 开销翻倍

**修复**（`9c87bcf`）：
- `agent.py move_doc` 删掉显式 `_update_references` 调用，引用更新由 watchdog `on_moved → apply_change` 统一处理（毫秒级延迟）
- 返回值不再包含 ref count（异步处理，无法同步获取）

---

## #6 _self_heal 非唯一匹配静默跳过（低）

**位置**：`main.py:76-78`

**现状**：
```python
candidates = live_by_hash.get(row[0], [])
if len(candidates) != 1:
    logger.warning("self-heal: no unique match for %s (%d candidates)", old, len(candidates))
    continue  # ← 跳过，stale 路径留在 DB 中
```

**影响**：
- 服务器停机期间，如果两个文件内容相同（如模板文件、重复笔记），重命名后哈希匹配到多个候选
- stale 路径永久留在 DB 中，磁盘上不存在
- 客户端同步时该路径在 `server_files` 中但磁盘无文件 → `get_doc` 返回 404
- 没有自动修复机制，需要手动清理 DB

**修复**：
- `candidates == 0`（文件在停机期间被删除）：清理 stale DB 条目（`delete_file` + `delete_segments` + `graph_delete_file`）
- `candidates > 1`（歧义匹配）：改进 warning 日志，列出所有候选路径方便手动排查
- 返回值包含 migrated + cleaned 总数
