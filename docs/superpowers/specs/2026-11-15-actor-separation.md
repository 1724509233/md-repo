# 删除恢复通道按 actor 分离（2026-11-15）

## 背景

2026-11-15 上午实施了「trash × rollback 整合」（trash 通道整体退役，回滚成为唯一恢复路径），未提交。用户随后否决该方向：**回滚与回收站不应合并**——

- 回滚 = 对 **agent 侧错误**的兜底（按快照，机制没有问题）
- 回收站 = 对 **人的错误**的兜底（仅针对删除这种不可逆操作）

整合 WIP 已整体备份至分支 `backup/trash-rollback-consolidation-2026-11-15`（8a7040d，30 文件），main 回退至 cfd9982。本 spec 取代整合 spec（`2026-11-15-trash-rollback-consolidation.md`，仅存于备份分支）。

## 冲突回顾（为什么之前要动）

已提交状态（cfd9982）下**每次删除都是双通道**：web 文件/文件夹、agent 全部 `capture`（快照）+ `move_to_trash`（回收站）。四个冲突的根源 = 同一次删除同时进入两个生命周期：

- **P0**：回滚恢复 → trash purge 跑 `remove_dead_links`（无存活检查）剥离存活链接
- **P1**：purge 剥链 → 回滚恢复，链接清理不恢复
- **a/b**：双入口（回收站 UI + 块⑦）对同一删除的 409 UX

## 方案：按「人是否在工作台」分离删除恢复通道

| 通道 | write_source | 删除流程 | 恢复路径 | 过期边界 |
|---|---|---|---|---|
| web（console.py） | `web` | `move_to_trash`，**无 capture** | 回收站 UI | 30 天 purge |
| 交互 agent（delete_doc） | `agent` | `move_to_trash`，**无 capture** | 回收站 UI | 30 天 purge |
| bgagent（delete_doc） | `cfg.alias` | **直接 unlink** + `capture`，无 trash | 块⑦ 回滚 | 30 天 GC |
| mcp（mcp_server → delete_doc） | `mcp` | **直接 unlink** + `capture`，无 trash | 块⑦ 回滚 | 30 天 GC |

（executor 若存在删除路径，按人不在工作台归入快照侧；实施时核实。）

两个生命周期从此**不在同一文件上交叉** → P0/P1/a/b 结构性消失：不需要双通道协调（B+C），不需要退役 trash，块⑦与回收站 UI 各管各的、零重叠。

**分支判据**（delete_doc 内）：`ctx.write_source == "agent"` → 工作台（trash）；其余（bgagent alias、`mcp`）→ 快照侧。约束：bgagent alias 不得为 `agent`（config 校验或文档化）。

## 改动清单

### 1. 删除入口路由（核心新改动）

- `server/routes/console.py`（web 文件 + 文件夹）：移除 `capture`/`finalize`/`gc_after_capture`，保留 `move_to_trash` + `ledger.record(..., "deleted", "web", remove_links=False, pre_hash=...)`。
- `server/agent/tools/delete_doc.py`：按 `ctx.write_source` 分支：
  - `"agent"`：`move_to_trash`（保留），移除 capture。
  - 其余：直接 `unlink` + `capture(op="delete", run_id=ctx.run_id)`（bgagent run 级分组 → 批量恢复，用户拍板 Q1；mcp 无 run_id → 单文件单调用）+ `ledger.record(..., "deleted", ctx.write_source, remove_links=False, pre_hash=...)`。顺序 `capture → unlink → record → finalize`；unlink 失败 → `discard` 快照；record 失败 → 文件已删但快照可回滚（快照即兜底）。
  - 文案按分支：agent → 「Moved to trash: X (restorable via the trash UI)」；快照侧 → 「Deleted: X (restorable via rollback within 30 days)」。

### 2. pre_hash 进 change_log（取自备份分支，仍有效）

- `change_log` 增 `pre_hash` 列（单源 DDL + 幂等 `_ensure_*` 迁移，保留存量行；模式见 `_ensure_change_log_restore_op`）。
- 所有删除入口（两分支）在删除点（文件尚存）计算删除前 SHA-256 并随 `ledger.record` 落行。

### 3. sync `deleted_hashes` 改从 change_log 派生（取自备份分支，仍有效）

- `server/routes/sync.py`：`deleted_hashes = {原路径: pre_hash}` 从 change_log delete 行派生（每路径最新行胜，**无期限**）——替换 `trash.deleted_hash_map`。
- 关键：快照侧删除（bgagent/mcp）**不入 trash**，若 sync 仍从 trash 派生，客户端本地副本会判 `to_push` → **agent 删的文件被推回服务器复活**。change_log 派生堵住此洞；顺带修 D5（trash 派生 30 天 purge 后条目消失 → 客户端长期离线时 stale 副本复活）。
- `compare_files` 规则不变：`recent_deletes` 内 → `to_delete`，除非 `deleted_hashes` 有 pre_hash 且本地 hash 不同（删除后重建 → `to_push`）；move 规则优先。无 pre_hash 的外部删除恒 stale（既有文档化行为）。

### 4. `remove_dead_links` 存活检查（refs.py，取自备份分支）

目标路径在盘上 → 返回 0 不剥链。trash purge（工作台删除的延迟清理）与 GC 到期（快照删除的清理）两条调用方都安全。

### 5. GC 到期边界清理（snapshots.py，取自备份分支）

delete 类快照行过期（blob + 行删除）时，对路径跑 `remove_dead_links`——路径集从 **change_log** 派生（`op='delete' AND ts < cutoff`，最新在前，LIMIT 1000；快照行同批被删故重新发现是设计内，幂等重处理 = no-op），stat 存活预检（存活路径跳过 O(vault) 扫描、不占额度），每次 GC 上限 `GC_DEADLINK_CAP=5` 计实际清理。

### 6. 回滚补全（routes/memory.py + snapshots.py，取自备份分支）

- **G1**：delete 类回滚成功后重抽取引用者图边（edges 查询优先、O(vault) 内容扫描兜底、逐引用者 `graph_extract_file`；不恢复引用者内容——无 pre-image）。
- 回滚路由同步 `ledger.record(..., "restore", "web")`（替代 register + watcher，消除 UI 读 files 表的 stale 窗口）；op = `restore` 非 `created`（bgagent arm 面 `ops=("create",)` 不得见人工恢复）。

### 7. 不动的部分

- **前端零改动**：回收站 UI（TrashPanel）、块⑦（recent 50 全 op——分离后 delete 类天然 = 仅 bgagent/mcp）、FileTree 文案、api.js/app.js 全部保持已提交状态。
- trash 机制本体（move_to_trash/restore/purge/meta.json/.trash 排除守卫）不动。
- 红线不动：调用内 all-or-nothing、无 force、无 run 级批量端点。批量恢复 = bgagent run 级 call 的既有语义（Q1 拍板）。

### 8. AGENTS.md

Trash gotcha 条目改写为分离语义：web+agent 删除 = trash（无快照）；bgagent+mcp 删除 = unlink + 快照（无 trash）；sync 从 change_log 派生（无期限，堵快照侧复活洞）；purge = 工作台删除的链接清理、GC 到期 = 快照删除的链接清理（均带存活检查）；块⑦ delete 类 = 仅人不在工作台的删除。

## 测试

- web 删除：入 trash、**无快照行**、change_log 行带 pre_hash
- agent 删除：同上（write_source="agent"）
- bgagent 删除（write_source=alias）：**无 trash**、unlink、快照行、change_log 带 pre_hash；同 run 两文件 = 一个 call（批量恢复）
- mcp 删除（write_source="mcp"）：无 trash、unlink、快照行、单文件单 call
- sync：快照侧删除 → 客户端 stale 副本 `to_delete`（复活洞回归测试）；删除后重建 → `to_push`；外部删除无 pre_hash → 恒 `to_delete`；30 天窗口后仍 `to_delete`（无期限）
- refs 存活检查：目标在盘 → 0 文件被改
- GC 到期清理：存活目标零剥离 + 上限
- 回滚：delete 类恢复后引用者边重抽取；路由记 op `restore`（arm 回归：`ops=("create",)` 不含）
- 更新既有 trash/rollback/sync 测试中受路由影响的断言

## 边缘与过渡

- **今天的 3 个文件**（在未提交整合代码下删除 = unlink + 仅快照，actor=web）：回退后无 trash 条目，但快照在 → 块⑦可见可恢复（过渡产物，无需处理）。
- 11-01 的 72 个文件在 .trash + 快照（整合代码前的双通道产物）：trash UI 可恢复；快照侧块⑦也可恢复（双通道存量，purge 后走快照、GC 后走 trash 语义，互不干扰）。
- 外部直接磁盘删除 → watchdog 行无 pre_hash → 恒 stale（既有行为）。

## 状态

已实施（2026-11-15，后端单线 @fixer，前端零改动）。

### 实施记录

- 路由：console.py 移除 capture（trash-only）；delete_doc 按 `write_source == "agent"` 分支（agent → trash；bgagent/mcp → capture→unlink→record→finalize，`call_id=run_id` 批量恢复）
- 移植项 2-6 全部落地（pre_hash 迁移、sync 派生、refs 存活检查、GC 边界清理、回滚 G1 + 同步 restore 记账）
- **GC source 过滤**（实施中修正）：清理查询加 `AND source NOT IN ('web','agent')`——trash 通道未 purge 的删除若被 GC 剥引用者链接文本，trash 恢复时内容重抽取找不回 → 链接永久丢失；workbench 删除的清理只由 trash purge 执行，外部删除（watchdog）仍由 GC 清理
- 核实结论：executor 无删除路径（无需改动）；mcp_server 连接对已注册（capture 可用），但 **MCP 工具面当前不暴露 delete_doc**（McpExternalGate 拦主区写入，既有行为）——快照分支已实现并测试，MCP 删除需 gate 放行后生效
- bgagent alias `agent` 在 config load 时拒绝（ValueError，fail-fast）
- 测试：test_deletion_rollback.py 20 个（两分支删除流、批量恢复、sync 复活洞回归、GC 含 workbench 不剥离回归、回滚 G1 + arm 回归）+ 既有测试更新。全量 1046 passed + 8 failed = 已知预存集合
- 待用户功能验证（web 删除 → 回收站可见、块⑦不再出现 web 删除）后提交
