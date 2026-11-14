# 回滚 UI（2026-11-14）

> 落位拍板（2026-11-14）：监控页（L1）新块⑦「回滚」，通栏。v5 设计稿原方案为控制台 tab③；v5 剪枝（11-04）后控制台定为纯审查队列，入口移至监控页。

## 背景

- P1 可逆层（e741004，10-26）后端齐全且已测：`GET /api/memory/rollback-calls` + `POST /api/memory/rollback`（verify-all-then-write-all；人独占——非 agent 工具、非 bgagent 暴露）。
- v5 设计稿「回滚/维护」tab 未落地；删除类已被回收站接管（P4-4，2caa75d），30 天保留期被取代。
- 本 spec 主体是 UI 接线：最近调用列表 + 调用粒度回滚 + 409 冲突裁决。后端唯一改动：route 的 409 分支补透传 `restored`（原实现丢弃 Phase 2 半态的已恢复部分，见「边界」半态条）。

## 方案

- **落位**：监控页新块⑦「回滚」，通栏，置于⑥采纳提案之后（组 2「系统」尾部）；零态 = 常驻淡显（.zline：「无最近调用」）。控制台三面不动。
- **数据**：app 既有 30s 轮询周期（app.js L659，与 pollMemory 同拍）追加 `GET /api/memory/rollback-calls?limit=20` → `state.rollbackCalls`。`recent_calls` 为单条索引聚合，成本可忽略；列表是快照，符合页面 30s 轮询口径，不引入 L2 按需层。
- **行**：op 徽章（create/update/delete/move）+ actor + 相对时间（relRun 口径）+ 文件数（title = 全路径列表）+ 回滚按钮。后端序 = 最新在前。
- **动作**：点「回滚」→ 该行按钮进入确认态（确认回滚 / 取消，5s 自动复位）→ `POST /api/memory/rollback {call_id}`。
- **结果**：
  - 200 `{ok, restored}`：行内提示「已恢复 N 个文件」；立即重拉列表 + `loadDocs()`（恢复写入经 route 内 `ledger.register("web")` 落 change_log，不等下一轮轮询）。
  - 409 `{ok: false, conflicts}`：行下出冲突卡，逐条展示 conflicts（服务端人读字符串，如 `notes/x.md: modified after the call`）；唯一动作「保留现状」（关卡）。后端整批 all-or-nothing：任一文件冲突 → 整批拒写、磁盘不动。人解决冲突文件后重试回滚。
  - 409 半态（Phase 2 恢复失败：磁盘错误 / 快照 blob 已被 GC，极罕见）：body 含 `restored`（已恢复部分）+ `conflicts`（失败原因）→ 卡内两段都展示，动作同「保留现状」。
  - 其余非 2xx：alert（代码库惯例）。
- **api.js**：新增 `getRollbackCalls(limit = 20)`（GET，!ok 抛错）与 `rollbackCall(callId)`（POST）。`rollbackCall` **不复用 `_jsonOrDetail`**：该 helper 对 409 抛错且丢弃 body（409 body 为 `{ok, conflicts}`，无 `detail` 字段，只剩 "HTTP 409"）→ 409 时解析 body 返回可区分结果 `{ok: false, conflicts, restored?, status: 409}`，其余非 2xx 抛错。
- **红线**（设计稿 §四，不变）：
  - 回滚人独占：不进 agent 工具表、不进 bgagent 配置面、无 cron/事件触发。
  - 调用粒度：回滚单位 = call_id，无 run 级批量。
  - 无盲写：verify 阶段是服务端不变量，UI 不提供 force / 跳过校验。
  - 控制台只呈现：本块在监控页，控制台不动。

## 判据

- 监控页⑦块列出 ≤20 条最近调用（最新在前）；无调用时淡显零态。
- 回滚成功后 1s 内列表与文件树刷新（立即重拉，不等轮询）。
- 409 冲突卡逐条展示服务端 conflicts 原文，唯一动作「保留现状」；无「回滚其余文件」按钮。
- 已回滚的调用仍在列表；再次点回滚 → 409（状态已偏离 post-state），卡内说明原因。
- agent 工具表与 bgagent 配置面不出现 rollback 入口。

## 边界

- **设计稿「回滚其余文件」不实现**：后端整批 all-or-nothing（任一冲突 → 整批拒写），无 per-file 端点。部分回滚破坏调用粒度不变量（call 是回滚单位，部分回滚留下混合态）。如未来需要 = 后端扩展（新端点），非 UI 范畴。
- 恢复写入归属 `source="web"`（route 内 `ledger.register`），watcher 恰好记一行；UI 不直接写 change_log。
- 删除调用回滚 = 从快照 blob 恢复 pre-image 字节；回收站 batch 不动。回收站 restore 与本回滚互为 409 面（app.js trashAct 注释 design §11-6 已覆盖该交互）。
- 删除类快照 blob 有 30 天保留（DELETION_RETENTION_S）；blob 被 GC 后回滚该调用 → Phase 2 失败 → 半态卡说明。
- 列表不区分在途 / 已被覆盖：409 即裁决面。

## 明确不做的事

- run_id 级批量回滚。
- per-file 回滚 / 「回滚其余文件」。
- force / 跳过 verify 参数。
- 控制台回滚入口（纯审查队列保留）。
- 回滚通知推送（恢复写入走常规 change_log；通知纪律不变）。
- 维护子面（legacy summary / 终态投影清理已被 D25 / 回收站取代）。

## 测试

- 后端既有覆盖有效：`tests/test_snapshots.py`（rollback 语义 + 端点 + rollback-calls 形状）、`tests/test_routes_order.py`（路由表序）。
- 新增 `test_rollback_endpoint_half_state_surfaces_restored`（test_snapshots.py）：blob 缺失模拟 Phase 2 中途失败 → 409 body 同时含 `conflicts` 与 `restored`（已恢复部分），磁盘上已恢复文件 = pre-image。
- `tests/test_ui.py::test_api_js_exports_all_endpoints`：api.js 新增两个导出后同步更新该 pin。
- 前端视觉确认（AGENTS.md rule 3）：浏览器确认⑦块布局 / 零态 / 确认态 / 409 冲突卡，用户确认后方可提交。
