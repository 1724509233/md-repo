# 审查清单 — `e577156` feat(memory): v2 recall & lifecycle

- 分支: `feat/memory-runtime` · 提交: `e577156`（原占位 "unfinished commit"，2026-10-21 amend 补 message）
- 规模: 29 文件 +3035/−85 · 全量测试: **500 passed** + 1 已知平台断言失败（`test_snapshot_watcher`，父提交同样失败，非回归）
- 建议顺序: C（安全面）→ B（新端点）→ H（缺口确认）→ A/E（新逻辑正确性）→ 其余

---

## A. 归档通道（archive）

- [ ] **A1** `proposals.py:47-49, 275-278` — `VALID_TYPES`/`AUTO_TYPES` 含 archive；auto 型 adopted 强制 `reversible: true`。判定标准: archive 属自动通道（无需终审），与 docstring "v2 扩展 §5" 一致
- [ ] **A2** `executor.py:219+` `_merge_archive` 三态矩阵 — ① src+dest 双存在 → 拒（拒静默覆盖）② src 缺+dest 有 → 幂等 no-op（崩溃窗口恢复）③ src 缺+dest 缺 → failed ④ src 有+dest 缺 → rename。检查四态是否全覆盖
- [ ] **A3** 守卫链 — `payload.path` 过 `_vault_path`（穿越→failed）；`_ARCHIVE_SYSTEM_DIRS={suggestions,index,.sync,archive}` 拒归档（含双重归档）。检查: 嵌套子路径（`a/b/note.md`）归档后 dest 目录创建、move 记账 `from_path` 正确
- [ ] **A4** 旧路径索引清理 — archive 走 `apply_change(op="move")`，确认 move 分支删了旧 path 的 segments/graph/引用（`file_ops.py` move 分支）

## B. 记忆控制台（api.py）

- [ ] **B1** `_guard_proposal_relpath:101-120` — safe_rel_path + **恰好 2 段** + 首段 `suggestions` + `.md` 后缀 → 否则 400。检查: `adopt`/`reject` 两端点都过它（:568/:426），URL 编码 `..` 绕过无效
- [ ] **B2** `adopt:560-585` — 先 parse（malformed→400）→ status 必须 `awaiting_review`（其余 400）→ `advance(actor="human")` → **API 不应用变更**（执行器落地）。检查 docstring 与实际行为一致
- [ ] **B3** `reject` — reason 落 `reject_reason` 并记账（actor="human"）；检查 reason 为空时的行为
- [ ] **B4** `GET /api/memory/proposals` — malformed 条目降级不 500；**检查返回字段是否含 body 全文**（LAN 无鉴权设计下的暴露面评估）
- [ ] **B5** `GET /api/memory/status` — `by_status` 恒定 8 状态+malformed（轮询端 shape 稳定）；审查队列 oldest first；含 trigger 状态 + 召回需求

## C. MCP 免疫管线（mcp_server.py）

- [ ] **C1** `_gate_external_write:34+` — 首段 `suggestions` 放行，其余拒绝且**写未执行**；错误消息应含"写 proposals + correction 类型"引导
- [ ] **C2** 挂载完整性 — 两个调用点（:157/:175）覆盖**全部**写工具；核对 MCP import 列表当前只有 read/edit/search/related（若未来加 write_doc/move_docs 工具须同步挂 gate）
- [ ] **C3** 读接口不门控（read_doc/search_docs/get_related_docs 直通）
- [ ] **C4** `test_mcp_gate` 覆盖放行/拦截/消息内容三类

## D. 召回遥测 v2（recall.py）

- [ ] **D1** `_traffic:20-27` — `agent→on_demand`、`web→human`、**其余全 synthetic**（含 sync/watchdog/bgagent alias）。检查 source 词表是否与此映射一致，有无遗漏的人工通道
- [ ] **D2** `log_query` — 每查必落 1 行 recall_queries + 每命中 1 行 recall_log；**零命中也落账**（失败召回信号）；telemetry 异常不打断搜索响应（调用方 try/except）——核对 `/api/ai/search` 与 `search_docs` 两处调用
- [ ] **D3** 旧行回填 — schema 迁移 `CASE WHEN source='web'→human, 'agent'→on_demand, else→synthetic`；检查**幂等**（重跑不二次改写 traffic 列）
- [ ] **D4** sentinel 输入纪律 — sentinel 读 `recall_stats` 属旁路统计（不写遥测）；检查 prompt 是否要求 sentinel 调 `search_docs`（会写 synthetic 遥测；合成流量自排除已防武装回环，但语义要确认）

## E. trigger 扩展（scheduler.py + config.py）

- [ ] **E1** `source=recall` 双游标 — `last_recall_query_seq`/`last_recall_hit_seq` 独立推进 + `low_score` 阈值（:218-232）；检查双游标与 `scan_recall_arming` 的边界（同 seq 不漏不重）
- [ ] **E2** `source=change` seq 游标 — 替代 ts float，修复**同时钟滴落**（ts==last_armed_scan 被 `ts > ?` 吞）；核对 `get_changes_sources(since_seq=)` 实现
- [ ] **E3** recall 心跳跳过 human-activity idle 检查（:790-793，"复盘不靠自觉"）——确认 change 源仍受 deadline floor 约束
- [ ] **E4** config 解析 — `tools` 白名单（None=全集，旧配置零破坏）+ `escalation{min_interval_h, low_score}` 校验（test_config +147）；检查非法值报错含 index/alias 定位
- [ ] **E5** runner — `filter_tools` 空匹配=配置错误（fail-fast）；`_derive_kind`: awaiting_review 或 reversible=false → `review`，frontmatter 不可读 → `review`（over-notify 安全默认），非提案文件 → `batch`

## F. 恢复演练（recovery.py）

- [ ] **F1** 五步流程 — 备份（含 -wal/-shm）→ 冷丢失 → 生产启动路径重建（`init_db`+`ensure_indexed`）→ `_full_scan` 重种 files 表 → 对盘校验；检查每步失败的中止与报告
- [ ] **F2** 生产安全 — CLI 默认跑**逐字节临时副本**，不触碰在库（docstring 明示 DB 被 MCP 进程持有）；检查是否有任何路径能指向 live vault

## G. 新角色（prompts + config）

- [ ] **G1** `sentinel.md` — 铁律"主记忆区只读、唯一写=write_doc 到 suggestions/"；周心跳 + 低分召回提前唤醒；检查与 config.example.yaml 的 sentinel 条目（cron/trigger source=recall/escalation）一致
- [ ] **G2** `narrator.md` — 写 scope 仅 `weekly/`；确认 runner `write_scope=cfg.target_dir` 对 narrator 生效（target_dir=weekly/）
- [ ] **G3** `executor.md` — 纯占位（满足 prompt 存在校验 fail-fast，`is_executor=true` 不启 LLM cron）；检查注册表校验逻辑对三者的处理

## H. 已知缺口（本提交**未**覆盖，确认知悉）

- [ ] **H1** L1 闸门缺失 — `run_executor` 仍无条件合并 REVIEW_TYPES 的 `adopted`: **进程内** LLM 直写 status 的 bypass 敞开（MCP gate 只堵外部路径）
- [ ] **H2** L2 缺失 — `advance()` 对 `awaiting_review→adopted` 无 actor 限制
- [ ] **H3** L3 存量 — 未审 adopted review 提案无处置策略
- [ ] **H4** relates_to 外键未实现；P2/P3 八条 nit 未动

## I. 测试

- [ ] **I1** 全量: 500 passed + 1 已知平台断言失败（非回归）
- [ ] **I2** 八块对应文件抽查: `test_archive_proposal`(212) / `test_memory_api`(255) / `test_recall_full`(477) / `test_mcp_gate`(108) / `test_recovery`(133) / `test_notify_tiers`(141) / `test_trigger`(+187) / `test_config`(+147)
