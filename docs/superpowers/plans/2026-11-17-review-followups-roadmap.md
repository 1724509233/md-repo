# 审查后续修复路线图（F5–F15）

> 状态基线：2026-11-16 全量审查产出 F1–F15；**F1–F4 已完成**（分支 `fix/sync-lane-baseline-resurrection`，commits 41ac81b..16a59a8，终审 Critical 已修，1078 passed / 4 既有基线失败）。本文件把剩余发现拆成三份计划 + 一份积压，**Plan 2（F5/F13）已写成可执行计划**：`2026-11-17-path-guard-and-scan-surfaces.md`。执行惯例沿用同步 lane 计划：分支上 TDD、文件集不相交时可并行委派、终审派全新子代理。

## 状态表

| 发现 | 级别 | 状态 | 归属 |
|---|---|---|---|
| F1 同步修改不传播 | P1 | ✅ 16a59a8 等 | 同步 lane 计划 |
| F2 200 行窗口复活 | P1 | ✅ b8bad36/b2caa8f | 同步 lane 计划 |
| F3 move→delete 复合复活 | P1 | ✅ b8bad36/b2caa8f | 同步 lane 计划 |
| F4 drill 清空 change_log | P2 | ✅ 951c13e | 同步 lane 计划 |
| **F5 盘符路径逃逸** | **P1** | 📋 计划已写 | **Plan 2（可执行）** |
| F13 降级检索扫 .trash | P2 | 📋 计划已写 | **Plan 2（可执行）** |
| F6 upload/save register 后置 | P2 | 📋 大纲 | Plan 3 |
| F9 rename 失败不回滚引用改写 | P2 | 📋 大纲 | Plan 3 |
| F10 非 UTF-8 往返损坏 | P2 | 📋 大纲 | Plan 3 |
| F11 用户正文非原子写 | P2 | 📋 大纲 | Plan 3 |
| F12 trash 回滚 rmtree 销毁唯一副本 | P2 | 📋 大纲 | Plan 3 |
| F7 rollback create 记账错误 + skip 压制 | P2 | 📋 大纲 | Plan 4 |
| F8 move 回滚不逆转 refs/relatedness | P2 | 📋 大纲 | Plan 4 |
| F14 4 个基线失败测试 | P3 | 📋 大纲 | Plan 5 |
| F15 杂项（6 项） | P3 | 📋 大纲/待决策 | Plan 5 |

## 建议执行顺序

**Plan 2 → Plan 4 → Plan 3 → Plan 5**。理由：F5 是唯一 P1 且小；Plan 4（回滚语义）小而独立；Plan 3 最大、触碰 refs/move 热路径，放在小计划验证守卫纪律之后；Plan 5 是卫生 + 决策项，随做随清。各计划独立分支，可并行度：Plan 2 与 Plan 4 文件集不相交（前者 vault_ops/search/list_files，后者 routes/memory + snapshots.py + trash.py），可同波派发；Plan 3 与两者共享 refs.py/vault_ops.py，**不并行**。

---

## Plan 3：写面纪律（F6 + F9 + F10 + F11 + F12）

**Goal:** 收编所有用户正文写面到同一纪律：register-before-write、原子写（tmp+os.replace）、编码保真、失败可回滚。这是审查的架构级观察「refs 改写是账本外的写」的落地。

**Files:** `server/routes/console.py`、`server/vault/orchestration/refs.py`、`server/vault/fs/vault_ops.py`、`server/vault/orchestration/trash.py`、`client/sync.py`。

**Task 大纲**（执行前用 writing-plans 细化成 RED→GREEN 步骤）：

1. **F6 register-before-write**：`console.py` upload_doc（:268-283）/save_doc（:285-298）改为 `mkdir → ledger.register → write`。测试：client fixture 上传后 `simulate_consumer` 驱动 consume，断言 change_log 行 `source='web'` 且注册表已清空（若 register 后置，misattribution 为 watchdog 即被抓）。
2. **F11 原子写统一**：`refs.py` 两处 `md_file.write_bytes`（:118/:197）→ `atomic_write_bytes`；`vault_ops.edit_file`（:107）→ 同；`client/sync.py` 的 to_pull/conflict 写盘（client 侧自实现 tmp+`os.replace`，仅 stdlib）。测试：写入后 vault 目录无 `.*.writing-tmp` 残留；既有全部内容断言保持。
3. **F10 编码保真**：`edit_file` 改 strict decode——`UnicodeDecodeError` → 返回 Error 串（文件不动）；`refs.py` 两个改写循环 decode 失败 → 跳过该文件并 log（不写回）。测试：预置含非法 UTF-8 字节的夹具 → edit_doc 返回 Error 且文件**逐字节**不变；move 触发的引用改写跳过该 referrer 且其内容不变。
4. **F9 rename 失败不回滚引用改写**：拆分 `update_references_batch` 的「唯一性判定（需 rename 前做）」与「改写落盘（须 rename 全部成功后做）」——新增参数或返回 prepared-rewrites 由调用方在 rename 成功后应用；`console.py move` 与 `move_docs.py` 两处调用点同步改。测试：monkeypatch 第二个 rename 抛 OSError → 断言 referrer 内容未被改写、已成功子项的改写正常落盘、异常上抛。
5. **F12 trash 回滚非破坏**：`trash.py:207-215`——任一 rename-back 失败 → 保留 batch 目录、抛带 batch 路径的 TrashError；绝不 `rmtree`。测试：monkeypatch rename 在回滚段失败一次 → 断言 batch 目录与文件仍在、TrashError 携带 batch 名。

**风险注记**：Task 4 改 refs 调用时序，`tests/test_move_detect.py`/`test_trash.py`/`test_agent.py` 的 move 用例面广——细化为计划时先跑这三个文件定基线。

## Plan 4：回滚语义（F7 + F8）

**Goal:** 人工回滚（block ⑦）的记账与副作用与真实写语义对齐：按 snapshot op 分派记账 op、create 类不再被自身 skip 标记压制、move 回滚逆转 refs 与 relatedness。

**Files:** `server/routes/memory.py`（rollback 路由 :231-267）、`server/snapshots.py`（rollback :494-558）。

**Task 大纲**：

1. **F7 按 op 分派记账**：路由读 `snapshots.by_call` 取每路径 op——`create` 类 → `ledger.record(..., "deleted", "web")`（apply_change delete 分支不查盘、可处理已 unlink 的路径）；`delete/update` → `record("restore")`；`move` → 旧路径 `record("restore")` **加** 新路径 `record("deleted")`（消除 watchdog 兜底归因）。测试：回滚一个 create-op 调用 → change_log 有 `source='web'` 的 delete 行、files 表行消失、（`ledger._pop` 断言）skip 标记不残留压制 watcher；回滚 move-op → 两个路径各行其是。
2. **F8 move 回滚逆转**：`snapshots.rollback` move 分支（:530-534）在恢复后调 `_update_references(new→old)` + `relatedness.rewrite_paths(new→old)`（经 `get_index_conn`/`get_ledger_conn` 模块句柄，与文件_ops move 分支同口径）。测试：agent move（referrer 被改写）→ 回滚 → referrer 内容含 `[[old]]`、relatedness 行路径指回 old、无死链。

**依赖注记**：Task 1 的 move 分支记账会改变 Task 2 的断言环境——先 1 后 2，同计划顺序执行。

## Plan 5：测试卫生 + P3 积压（F14 + F15）

**Goal:** 基线恢复全绿；清理死代码与漂移温床；显式列出需要产品决策的项。

**Task 大纲**：

1. **F14 时间炸弹 ×3**：`tests/test_bgagent_runner.py:425-466` 三例 monkeypatch `runner` 的时间源（或改为「最新一份带 `{alias}_` 前缀」的结构断言）。测试即修复。
2. **F14 机器状态 ×1**：`tests/test_config_legal_layer.py:141` 改断言结构性质（别名唯一、legal layer 通过、executor 唯一），不再钉具体别名集合。
3. **F15 死代码**：删 `trash.deleted_hash_map`（:114-131）+ `tests/test_trash.py:447-459`（change_log 派生已全面取代；两处同规则漂移温床）。
4. **F15 agent_cache 泄漏**：`routes/agent.py` 的 `agent_cache`/`_locks` 与 `SessionManager.cleanup` 的 TTL 对齐（cleanup 回调里同步淘汰 agent）。
5. **F15 sync_lock 释放验 pid**：`release()` 读锁文件内容、pid 不符则不 unlink（防 300s 后误删接管者的锁）。
6. **F15 float(last_sync) 防御**：`routes/sync.py` 对非数字 last_sync 按 0.0 处理（400 或降级二选一，倾向降级——旧客户端兼容）。

**终审新增积压（2026-11-17 Plan 2 终审发现，均为主干既有、非本分支回归）**：

- **F16 MCP `get_related_docs` 无路径校验**（`server/mcp_server.py:291-295`）：`path` 参数裸 join + `is_file()`，win32 上构成 vault 外存在性探测。修法：过 `_check_md_path`/policy。
- **F17 `list_files` `dir` 含 `..` 时的兜底路径**（`server/agent/tools/list_files.py:39` + `agent/policy.py:31-53`）：L2 `_extract_paths` 不覆盖 `dir` 参数；`_resolve_dir_case_insensitive` 的 fallback 返回带 `..` 的基路径，rglob 在 `relative_to` 抛 ValueError（未处理异常）之前已枚举 vault 外。修法：`dir` 进 `_extract_paths` 覆盖面 + fallback 改为拒绝而非透传。

**需要用户决策（做之前问）**：

- **F15 MCP 死写面**：`_mcp_write/_mcp_edit` + `tests/test_mcp_writes.py` 全套机械当前经 MCP 表面必被双层 gate 拒绝。二选一：删除工具与测试（承认真隔离），或给 gate 开真写面（推翻 v2a「物理隔离」决定）。
- **F15 客户端 `.obsidian` 不对称**：客户端 `scan_local_files` 不排 `.obsidian` → 插件文档每轮 poll 永久重推。排（对齐服务端）还是承认并文档化「.obsidian 同步」？（倾向排——Obsidian 插件文档不该跨设备同步。）
- **F15 事件循环阻塞**（runtime.py:357 同步工具执行）：架构级，牵动工具执行模型，单独设计卡，不进修复计划。
- **终审 declined #1**（change_log 全量扫描性能）：触发式积压——sync 延迟可观测（>万行历史 + 实测变慢）时再立「seq 游标/op 索引」设计卡。
