# Spec：L0 adopted 卡 + 执行器失败分类 + 2 条卡住提案落地

分支：`feat/memory-runtime` · 2026-10-25 · 证据：本地复现（repro 脚本）+ 生产 `server.log` · 继承 `2026-10-24-event-driven-scheduler-design.md` 约束（零新增配置项 / 扫描判定逻辑零改动）

## 1. 背景：根因链（三层，均已本地复现）

现象：记忆控制台 L0 流水头部计数「2 待落地」，但流水里没有任何 adopted 卡；2 条 10-24 的 adopted 提案永久卡死，执行器每 30 分钟重试失败。

| 层 | 位置 | 性质 |
|---|---|---|
| 前端不可见 | `MemoryConsole.js:162-163` feed 只过滤 merged/rejected/failed 终态 + awaiting + agent 行 | adopted 无卡片形态——头部有计数、流水无卡 |
| 数据结构非法 | correction `payload: null`（内容在 body）；summary `source_refs: []` | v1 handler 严格校验结构化 payload → `ValueError` |
| 异常黑洞 | `executor.py:314-315` 通用 `except Exception` 只 append errors | 不记详情日志、不转状态 → 状态永远停在 adopted（server.log 21:32 / 22:11 `merged=0 expired=0 errors=2`） |

两条提案内容本身有价值：correction = todo.md 两处改动（旧条目仍在）；summary = 文件树右键菜单遮挡的完整根因分析（body 末尾自引 10-09 设计文档）。

## 2. 范围与决策

| 项 | 工作 | 改动面 |
|---|---|---|
| A 前端 adopted 卡 | L0 流水新增 adopted 卡（在途、不淡显） | `server/templates/js/components/MemoryConsole.js` |
| B 数据落地 2 条 | correction 手工落地 todo.md + advance merged；summary 补 1 个 frontmatter 字段、执行器自己落地 | vault 数据（repo 外） |
| C 执行器失败分类 | ValueError（结构性错误）→ failed + reject_reason + 详情日志；failed 卡 row2 显示理由（1 行） | `server/bgagent/executor.py`、`tests/test_executor.py`、`MemoryConsole.js`（1 行） |

决策记录：Q1 卡住提案处置 = **实际落地内容**；Q2 执行器加固 = **加**。两项均为 orchestrator 推荐项，直接执行，本 spec 送审时用户一并复核。

**范围外**（已报告 / 另立）：
- `_merge_summary` 输出 H1 重复（title 由 body H1 自动派生，handler 再拼一层；e2e 已复现，另立）
- flaky `tests/test_trigger.py::test_trigger_tick_runs_when_human_event`（tick 竞态，另立）
- 通知深链；详情面 adopted 支持（已存在，零改动）

## 3. A：L0 流水 adopted 卡

**数据**（`feedGroups` computed，MemoryConsole.js:137-230）：
- 新增 adopted 列表：`state.memoryProposals` 过滤 `status === 'adopted'`，映射为 `{ kind: 'adopted', key: 'd-' + relpath, relpath, type, title, status: 'adopted', created, row2: '等待执行器落地 · merged_at 待定', trail }`；**trail 与终态卡完全同口径**（API 返回是「新→旧」序，必须先 `[...p.trail].sort((a, b) => (a.seq || 0) - (b.seq || 0))` 再 `slice(-2)`，直接 slice 会取到最老 2 行且倒排）
- push 在 awaiting 之后、terminals 之前（line 199 前）；组 key = created 日期（与终态同口径 `keyOf(parseDay(created))`）
- `sortVal`（line 186）：agent 0 / awaiting 1 / **adopted 2** / term 3；组内次序复用现有 created 降序 tiebreak（line 205）
- FEED_CAP=8、f-more、lastItem 逻辑不动（lastItem 归全量末行，自动覆盖 adopted）
- 注释 line 126-128（feed 口径）同步更新为四形态：agent 细行 / 待审卡 / adopted 卡 / 终态卡

**模板**（feed 模板 505-551 区，在 awaiting 分支之后、终态分支之前插新 `v-else-if="it.kind === 'adopted'"` 分支）：
- 壳同终态卡：`f-item`（**带 `:class="{ last: it.lastItem }"` 绑定**，f-dot + f-time）+ `f-card`（点击 `pick(it)` 钻详情面——详情面已完整支持 adopted：虚线预告 line 328 / trailMsg 348 / 文案 412，零改动）
- row1 = `tchip`(type) + `f-title` + `spill adopted` pill（style.css:732 已有铜色样式）
- row2 = `等待执行器落地 · merged_at 待定`（与详情面 412 同口径）
- f-trail = 最近 2 条（同终态卡口径，trailMsg 复用）
- **不加 `.f-term` 类**：该类别名 `opacity: .82`（style.css:1069），语义「终态痕迹」；adopted 在途，不应淡显 → **视觉判断点，视觉验证阶段用户复核**
- 空态 `q-empty` 不动（adopted 卡计入 feedLen，「无待审提案」语义自然成立）

**CSS：零新增**（已核实：`.f-card` 通用壳、`.spill.adopted` 存在、adopted 不挂淡显类）。

## 4. C：执行器失败分类

现状（`executor.py:312-315`）：`except Exception` 只 append errors——不记详情、不转状态，状态永远停在 adopted。

改动 1（文件头）：`import logging` + `logger = logging.getLogger("md-vault.bgagent")`（该文件现无 logger；命名对齐 scheduler/runner/notify）。

改动 2（except 链展开 line 314-315）：

```python
except _MergeFailed as e:            # 已有：内容状态问题（old 缺失/多义等）
    advance(vault, conn, rel, "failed", actor="executor", reject_reason=str(e)[:200])
except Exception as e:
    if isinstance(e, ValueError):
        # 结构性校验错误 = 提案本身畸形（payload 缺失 / source_refs 空），非进程级崩溃
        logger.warning("[executor] merge failed (structural) %s: %s: %s", rel, type(e).__name__, e)
        advance(vault, conn, rel, "failed", actor="executor", reject_reason=str(e)[:200])
    else:
        # 环境/进程级（磁盘、DB 锁）：提案无辜，留 adopted 重试
        logger.warning("[executor] merge error %s: %s: %s", rel, type(e).__name__, e)
        errors.append(f"{rel}: {type(e).__name__}: {e}")
```

已核实前提（零签名改动）：
- handler 对结构性问题刻意抛 `ValueError`（correction payload / summary source_refs）——ValueError 语义 = 提案畸形
- **刻意语义（非疏漏，勿收窄分类）**：`_merge_link`「link source file not found」、`_merge_summary`「summary source file not found」（executor.py:141, 171）是数据状态错误而非提案畸形，同样归 structural→failed——源文件缺失重试无益（同步中/改名场景概率极低，且 reject_reason 可操作，与「异常黑洞」主题一致）
- `advance()` 已支持 `reject_reason` 参数 + frontmatter 持久化（proposals.py:246, 272-273）；adopted→failed 是合法迁移（现有 _MergeFailed 路径同款）
- 每次合并失败从此至少 1 行 warning 详情（修「只记 count 不记详情」）

**failed 卡可见性（1 行）**：`MemoryConsole.js:171` failed 分支 row2：有 `reject_reason` 时 = `理由：{reject_reason} · 提案保留待修`，否则保持现有 trail/详情文案。不加这条，C 的理由只存在于详情面与日志，feed 层仍不可见。

**测试**（`tests/test_executor.py` + 3 个现有用例更新）：
1. 畸形 correction（payload: null）→ `run_executor` → status=failed + reject_reason 含 `correction payload requires` + errors==[]
2. 畸形 summary（source_refs: []）→ failed + reason 含 `no source_refs`
3. handler 抛 RuntimeError（monkeypatch）→ status 保持 adopted + errors 有记录
4. **3 个现有用例断言的正是旧语义（ValueError 留 adopted + errors），必须更新为新预期**（status=failed、reject_reason 含对应文案、errors==[]）：
   - `tests/test_executor.py:65` `test_entity_index_bad_payload_errors`
   - `tests/test_executor.py:97` `test_link_missing_source_ref_errors`（source 文件缺失，R1 刻意语义）
   - `tests/test_archive_proposal.py:170` `test_archive_no_path_payload_stays_adopted`（v2 archive 原设计 retryable；payload 缺 path 永远不会成功 → failed，用例改名 `test_archive_no_path_payload_failed`）
5. 574 基线回归全绿（含上述更新）

## 5. B：2 条卡住提案落地

**顺序约束：B 必须在 C 代码重启前完成**——否则 C 重启后首个扫描把 2 条自动转 failed，落地路径被切断。B 本身不依赖 C（结构修好后新旧代码均可落地）。

**记账与并发注记**：
- `register_write` 是进程内 dict（TTL 10s，write_registry.py:13-15），脚本侧调用对运行中 server 不可见 → server SnapshotWatcher 会把 B1/B2 的文件写归因 `source="watchdog"`，每文件多一条 change_log 重复行（human + watchdog）。**纯外观**（merged 卡 f-trail 多一行、recent changes 重复），不触发 arming；**不加 workaround**（生产 change_log 10-24 21:30 已有同形重复对）。
- 脚本独立进程开 `D:\md-vault\.sync\index.db` 与 server 并发写：连接显式 `timeout=30`，apply_change 遇 "database is locked" 重试一次；仍失败则停下报告（文件已写但 ledger 缺失的半状态不允许静默继续）。

**B1 correction**（`suggestions/2026-10-24_correction_todo-filetree-menu-fixed.md`，payload: null，内容在 body）：
两处改动装不进单个 `{file, old, new}` payload → 脚本手工落地（`register_write` + 原子写 + `apply_change(source="human")`，与执行器同款记账）：
1. `D:/md-vault/notes/todo.md` 删除旧条目整行（line 15）：`- [ ] **移动端文件树操作面板被遮罩遮挡**（2026-10-22，历史遗留 bug）：…`
2. 「已完成」区块顶部插入：`- [x] 移动端文件树操作面板被遮罩遮挡（2026-10-22 记录，2026-10-24 根因确认 + 修复）：右键菜单是视口级浮层（fixed z-50）却渲染在面板子树内，被 .panel-shell 的 transform 包含块 + overflow 裁剪 + 层叠上下文困住（移动端裁进面板、桌面被 console/mwrap/preview 树序覆盖）；修复 = FileTree.js 菜单+遮罩包 <Teleport to="body">（已提交 2b97111）。详见 [[2026-10-24_summary_filetree-context-menu-layering]]`
   —— 提案原文「（未提交，待视觉验证后单独提交）」更新为「（已提交 2b97111）」反映实际状态（修复当天已提交）
3. `advance(vault, conn, rel, "merged", actor="human")`（merged_at 自动填，ledger source=human）
→ 卡片终态 = merged 终态卡（row2 = relpath · merged_at）

**B2 summary**（`suggestions/2026-10-24_summary_filetree-context-menu-layering.md`，source_refs: []）：
- 脚本补 1 个 frontmatter 字段（内容不动）：`source_refs: [projects/md-repo/spec/2026-10-09-webui-file-management-design.md]`（已确认存在；summary body 末尾自引）
- title 零增补：`parse_proposal` 从 body H1 自动派生（proposals.py:122, 126-131）=「文件树右键菜单遮挡：根因与修复（2026-10-24）」
- 执行器下一循环落地（≤30min backstop；frontmatter 变更不唤醒 executor，走兜底）：写 `projects/md-repo/spec/2026-10-09-webui-file-management-design-summary.md`（`# title\n\nbody`）
- 已知行为：输出 H1 重复（title = body H1，handler 再拼一层）——已报告 / 另立，本次不修

## 6. 实施顺序与验证计划

1. A 前端（数据 + 模板 + C 的 failed row2 1 行，同文件一次改完）
2. C executor + tests
3. **pytest 全量**（574 基线 + 新增）
4. B1 脚本 → API 核对 `by_status`：adopted 2→1，merged 2→3
5. B2 脚本 → 30min 内执行器落地 → adopted 0，merged 4，落地文件存在
6. **浏览器视觉**（用户，Ctrl+Shift+R 强刷）：
   - L0 流水：correction merged 卡 + summary merged 卡（终态形态，`f-term` 淡显）
   - 抛卡（adopted / failed 视觉证据，直写文件——**无创建 API**，`create_proposal` 仅 bgagent 进程内调用，直写不经过 validator；adopt 端点要求当前 status==awaiting_review，api.py:649-651，frontmatter 必须显式 `status: awaiting_review`，缺了 adopt 直接 400）：
     - **抛卡 1（adopted 视觉）**：`suggestions/2026-10-25_reminder_visual-check-adopted.md`，`type: reminder` + H1 body，无 payload。executor 对无 handler 的 reminder 只记 errors 不转状态（executor.py:306-308）→ **稳定停在 adopted**，adopted 卡长期可见（C 代码下也成立，不依赖重启前窗口）
     - **抛卡 2（failed 视觉，验证 C）**：`suggestions/2026-10-25_summary_visual-check-throwaway.md`，`type: summary`、`source_refs: []` → 执行器 ValueError → **转 failed** + 「理由：…no source_refs」row2 + structural 日志
     - 两张卡均 POST /api/memory/proposals/{rel}/adopt 驱动；验证后删除两个抛卡文件
   - 注：原计划「重启前旧代码窗口观察 adopted」已作废——实现中途 server 进程死亡（前会话后台进程被杀），重启即带 C 代码；reminder 抛卡方案不受影响，且窗口从 30min 延长到永久（验证期内）
7. **日志断言**：C 重启 ≤30min 内 → 抛卡 `[executor] merge failed (structural) … no source_refs`；B2 落地 merged=1 发生在**重启前**的旧代码扫描（≤30min backstop），与 structural 日志不是同一次扫描
8. **不提交**：repo 改动（2 文件 + 1 测试）留工作树，呈 diff 给用户；vault 数据改动（todo.md / suggestion frontmatter / DB）为数据操作，落盘即生效，不进 git

## 7. 约束

- 零新增配置项；style.css 零新增
- 扫描 / 判定函数零改动（事件驱动 spec 约束继承：`_executor_armed` / `scan` 不动）
- repo 改动文件：`MemoryConsole.js`、`executor.py`、`tests/test_executor.py`、`tests/test_archive_proposal.py`；`proposals.py` 零改动（advance 已支持 reject_reason）
- 执行器仅在用户方便时重启（B 数据修复不依赖 C 生效；重启前 errors=2 重试循环随 B 完成自然消失——B1 手工 merged 后 adopted 只剩 B2，B2 结构合法，旧代码即可合并）
