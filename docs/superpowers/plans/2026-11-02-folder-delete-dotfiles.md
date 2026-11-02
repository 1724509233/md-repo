# 删除「UI 空文件夹」失败 → /api/docs 304（待办 8978a337）

> Goal: 删除仅含点文件（如 macOS `.DS_Store`）的文件夹时真正删除该文件夹；后续 /api/docs 刷新返回 200（body 变化）而非 304，文件树同步消失该目录。
> 待办原文：「删除空文件夹返回304」

## Architecture

### 根因（已核到行级）

1. 前端删除流：`FileTree.js:248 manage('delete', {path})` → `app.js:316-333 manage()` → `api.manage`（POST /api/docs/manage，200）→ `loadDocs()`（app.js:80-83）→ GET /api/docs 带 `If-None-Match`（api.js:3-11：`lastTreeHash` 缓存，**304 → return null → state.dirs 保持旧值**）。
2. 服务端删除 `server/routes/console.py:116-208 delete()`：.md 文件逐个 unlink（快照组）后自底向上 rmdir 清扫（L190-200），**`except OSError: pass  # non-empty → keep`**（L199-200）；target 消失才 `record_folder`（L201-202）。
3. 缺陷：文件夹仅含点文件（`.DS_Store`）时——UI 树显示为空（dirs 来自 os.walk、files 只列 .md，点文件两端均不可见）——`rmdir` 因目录非空失败被静默吞掉，**文件夹留在磁盘**；随后 GET /api/docs 的 body（{files, dirs}，console.py:233-247，ETag 为 body SHA-256）不变 → **304** → 前端保持旧树 → 删除表现为静默失败。「删除空文件夹返回304」即此。
4. 注意：pathlib `Path.rglob("*")` 包含点文件（与 shell 不同），L191 的清扫列表里其实枚举到了 `.DS_Store`，只是被 `if not d.is_dir(): continue` 跳过——修复点天然就在这个循环内。
5. 既有 pinned 行为（不得破坏）：真正空文件夹 → 删除 + 记 deleted 事件（tests/test_api.py:707-729）；含非-.md **普通文件**（b.txt/img.png）→ 保留文件夹、无目录级事件（test_api.py:673-684、719-729）。

### 目标设计（钉死）

- `delete()` 文件夹分支的自底向上清扫循环（console.py:190-200）内，对每个目录 `d`：先 unlink 其**直接子文件中名字以 `.` 开头的点文件**（`.DS_Store` 等系统杂项），再 `rmdir`；仍然失败（含普通文件）→ 维持现状 keep。
- 语义：点文件 = 系统杂项，随文件夹删除；普通非-.md 文件 = 用户内容，继续保护（pinned 测试不动）。
- 已知边界（接受，注记即可）：仅含「点目录」（如嵌套 `.obsidian/` 及其内容）的文件夹仍被保留——罕见，不在本次范围。
- 响应体、路由、ETag/304 机制、前端 JS 均**不改**（304 是正确 HTTP 语义；bug 是删除没发生）。

## Tech Stack

Python 3.12、FastAPI、pytest（client fixture）、sqlite 双库（ledger_conn/db_conn 拆分已就位，本改动不动 ledger 调用）。

## Spec / 依据

- 待办 8978a337 原文
- `~/md-vault/projects/md-repo/specs/2026-10-16-empty-folder-tree-design.md`（/api/docs 返回 {files, dirs} + ETag/304 契约：「304 省的是响应体，不是扫描」）
- `~/md-vault/projects/md-repo/specs/2026-09-27-md-sync-design.md`（web 文件管理契约；删除不同步语义不变）

## Global Constraints

- 快照纪律：点文件 unlink **不**走快照 capture（系统杂项，无内容价值；快照只保护 .md 用户内容——与现 .md 文件 capture 路径区分）。
- ledger 写纪律：点文件 unlink 不单独记 change_log 行（目录级 `record_folder` 已覆盖该操作的用户可见语义）；.md 文件的 `ledger.record` 逻辑不动。
- Windows 兼容：点文件处理不引入平台特定 API；测试 fixture 用 `write_bytes`。
- `tests/test_routes_order.py` 路由表顺序不动。

## Review Focus

- 「点文件 = 系统杂项可随删」的语义边界（用户确认：含隐藏用户文件如 `.foo.md` 的文件夹将因此可删——`.foo.md` 是点文件；若用户有以 `.` 开头的内容文件，此行为改变其删除语义。实施前向用户确认一次）。
- 嵌套点文件（子目录里的 .DS_Store）：自底向上顺序保证子目录先清理，target 最终可删。

## Tasks

### Task 1：服务端点文件清扫（TDD）

- [ ] 1.1 红：`tests/test_api.py` 新增（fixture 用 `write_bytes`）：
  - `test_manage_delete_folder_with_dsstore`：`notes/empty/.DS_Store`（b"junk"，无 .md）→ POST delete 200、`deleted_count == 0`、目录不存在、`.DS_Store` 不存在、change_log 有目录级 deleted 事件（source=web）。
  - `test_manage_delete_folder_nested_dots`：`notes/x/a.md` + `notes/x/sub/.DS_Store` → 200、deleted_count == 1、`notes/x` 全消失。
  - `test_manage_delete_folder_dots_and_user_file_keeps`：`notes/y/.DS_Store` + `notes/y/b.txt` → 200、目录保留（普通文件仍保护）、`.DS_Store` 已被清（目录里只剩 b.txt）。
- [ ] 1.2 绿：console.py `delete()` L190-200 循环内加：`for p in d.iterdir(): if p.is_file() and p.name.startswith("."): p.unlink(missing_ok=True)`（OSError 容忍，尽力而为），然后原 rmdir 逻辑不变。
- [ ] 1.3 既有回归：`pytest tests/test_api.py -q -k "delete or folder"` 全绿（重点：test_manage_delete_folder_keeps_nonmd、test_manage_delete_empty_folder_logs_change 不回归）。

### Task 2：304 症状钉死 + 验证

- [ ] 2.1 `tests/test_api.py` 新增 `test_delete_dots_folder_changes_docs_etag`：建 `empt/.DS_Store` → GET /api/docs 记 Etag1 → POST delete → GET /api/docs 应 200（非 304）且 dirs 不含 `empt`。（直接钉住用户可见症状：修复前此测试拿到 304。）
- [ ] 2.2 全量 `pytest tests/ -q` 通过。
- [ ] 2.3 人工冒烟：dev 服务器 + 浏览器（或 agent-browser）实际删除一个含 `.DS_Store` 的文件夹，确认树中消失、无「操作失败」告警。

### Task 3：收尾

- [ ] 3.1 用户冒烟确认后提交（commit 含待办号 8978a337）。
- [ ] 3.2 关单：待办 8978a337 标记完成。

## 执行方法

单 lane @fixer（小改动：1 个循环 + 4 个测试）。lane brief = 本文件 + console.py:116-208 现状。串行派发。
