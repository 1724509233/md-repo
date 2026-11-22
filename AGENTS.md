# AGENTS.md

## agent-browser CDP (Windows)

Windows 上碰 CDP 前先读 `docs/superpowers/notes/2026-10-13-agent-browser-cdp-windows.md`（agent-browser 0.39.0 实测全记录）。红线速记：**绝不对宿主启动的 Chrome 跑 `agent-browser --cdp <port> close`**（会杀宿主进程树）；端口无监听时的「Operation timed out」是误导信息——先 netstat 查端口、清残留 `agent-browser*` 进程再重试。

## Commands

```bash
pip install -e ".[dev]"          # install (do this first)
pytest tests/ -v                 # run all tests
pytest tests/test_api.py::test_health -v  # single test
python -m server.main config.yaml   # start server (default port 9888)
python -m client.main config.yaml   # start client polling loop
md-vault-mcp                     # MCP stdio server (installed via [mcp] extra)
```

## Architecture

- **server/** — FastAPI app. `api.py` has the `create_app(vault_dir, db_conn, ledger_conn, ai_config, bg_targets)` factory (wiring only); routes live in `server/routes/` (one module per URL-prefix domain: console/notifications/sync/recall/memory/agent), each registering via `register(app, ctx)` on the `WebContext` (exposed at `app.state.ctx`). Route-table order is pinned by `tests/test_routes_order.py`. `main.py` is the entry point that wires config → db → app → `build_runtime()` (named runtime assembly: event bus + publisher filters, the four ledger callbacks, snapshot watcher, bgagent tasks; returns a `Runtime` with `stop()`) → uvicorn.
- **server/vault/** — Index subsystem, split into submodules: `db/` (SQLite schema, conn, file_index, segments_sql, fts_query, graph_sql, changes, sessions, recall, relatedness, trigger_state, notifications, proposal_records, snapshot_records), `fs/` (segmenter, link_parser, vault_ops), `orchestration/` (indexer, move_detect, refs, file_ops). Import directly from the real modules (e.g. `from server.vault.db.file_index import get_all_files`) — the `service.py` facade is gone (card 5).
- **server/agent/** — LLM agent system: `agent.py` (Agent class), `runtime.py` (LoopRuntime streaming loop), `runhub.py` (RunHub 运行体：独立 asyncio task + seq 环形缓冲 + 边界回放 + 服务端队列——运行脱离连接，铁律与法源索引见 Gotchas「投递/订阅分离」条), `session.py` (SessionManager), `tools/` (15 tools, one module each: read_doc / search_docs / list_files / get_related_docs / recent_changes / recall_stats / propose / list_proposals / get_proposal auto; write_doc / edit_doc / delete_doc / move_docs / adopt_proposal / reject_proposal ask), `policy.py` (channel policy), `prompts.py`, `reminders.py`.
- **server/bgagent/** — Background agents with independent model configs: `scheduler.py` (cron + event arming), `runner.py` (run_once), `notify.py` (in-app notifications), `executor.py` (framework role, not an LLM agent).
- **client/** — Python polling client. `sync.py` has `scan_local_files()`, `sync_once()`, `run_polling_loop()`. `main.py` is the entry point.
- **tests/** — pytest suite. `conftest.py` provides `vault_dir`, `db_conn` (index domain), `ledger_conn` (ledger domain, 2026-10-31 split), `app`, `client`, `ai_config` fixtures.

**Out-of-registry 开连铁律（2026-11-21 连接策略评审定稿）**：`open_index_db`（`server/vault/db/conn.py`）是注册单例对之外唯一具名开连面，失败 raise（调用方 owns 降级），调用方**必须** `contextlib.closing` 包裹——R7 零容忍 AST 钉在 `tests/test_architecture.py`。连接池方案已被评审否决（建连成本占比 <5%；池的闲置句柄正撞 Windows rename 锁；既定演进位是 thread-local，见 conn.py 头注）。

The vault directory is both the data store and the Obsidian vault. `.sync/` inside it holds **two SQLite files** (2026-10-31 ledger-file split, L4): `index.db` (rebuildable cache — files/segments/change_log/…; freely rebuildable, see `server/services/recovery.py`) and `ledger.db` (authoritative, not rebuildable — proposals/proposal_transitions/snapshots/relatedness/sessions; **the recovery drill only ever touches index.db**). Backing up the system must include **both** files. First-boot migration of these tables from index.db → ledger.db is in-path (`migrate_ledger` + `promote_sessions`, idempotent, per-machine); `logs/` also lives in `.sync/`. **sessions 升格铁律（2026-11-20）**：会话行是权威数据——恢复演练必须核查其存活，且演练即宣告 rename 回滚面关闭；index 侧 DDL 绝不得再长出 sessions zombie 表（违者幂等前提恒假 → 永久启动失败环）；迁移入口 `promote_sessions`（main + MCP 双入口同站位；目标非空 kept-both 人工裁决，不静默并）。法源：`docs/superpowers/specs/2026-11-20-sessions-ledger-promotion.md`。

## Gotchas

> 本文件只留「违者必炸」的铁律句与法源指针；机制全文一律进 `docs/superpowers/` 或 README（内联链接即索引）。

### 设置页（config 写面，2026-11-22）

- **设置页热生效铁律（spec `docs/superpowers/specs/2026-11-22-settings-page-design.md`，设计/评审/计划全链在该文件与 plans 同名文件）**——违者必炸：① PUT 落盘前必须在 **config 同目录** tmp 上跑真加载器（bgagent prompt 校验锚 = config 目录；tmp 去 `%TEMP%` 即假阴性），校验不过零落盘；`os.replace` 成功后 apply 抛 **不得反滚文件**（500 `saved:true`，回滚只会造第三态）。② `agent_cache` 失效只许 **`_get_agent` 读侧代际重建**（gen 落后 + 不忙），**永不外部清缓存**——RunHub persist 在持久化时刻取引用，外清 = 丢本轮对话（M1）。③ `BgSupervisor.apply` 是 change 过滤器与活别名集**唯一写入点**；环圈首时序 `reload.clear()` → 读 cfg/retired/别名集 → 进 wait 之间**零 await**（apply 同步同环是承重契约），违反即忙环或丢更新（M2/M4）。④ per-alias `asyncio.Lock` 归 supervisor，**跨 slot 重建复用**——发新锁即让在跑 run 与新环双写同 target_dir（M3）。⑤ `tasks()` 恒含缓退中任务，`shutdown()` 后置死、死后 `apply()` raise（M5）。回归钉家族：`test_settings_routes.py`、`test_bgagent_scheduler.py`（M2-M5 系）、`test_settings_hotapply.py`、`test_settings_doc_io.py`。完整性新增面备案见 spec §7（LAN 访客可改 config——用户拍板；token 化列为演进位）。

### 同步

- **Sync lock is a cross-process file lock**（`server/core/sync_lock.py`，挂 `app.state.ctx.sync_lock`）：原子 `os.open(O_CREAT|O_EXCL)` 排他（stdlib only——无 fcntl/msvcrt），同 vault 双进程不得并发 sync；锁龄 >300s 视为 stale 可接管（崩溃不得 wedge vault），内容不可读/空按 stale 处理（fail-open）。内存 `locked_at` 属性永远先查且是测试 seam：测试模拟持锁直接设它——拒绝路径不碰文件系统。文件内容格式等细节代码自明。

- **Deletion is not synced**: if a client deletes a file, it simply won't appear in `local_files`. The server keeps it. On next sync it shows up in `to_pull` (server has it, client doesn't). This is by design.

- **Conflict files**: saved as `{stem}.conflict-{device_name}{suffix}` (e.g. `notes/test.conflict-mac.md`). The local version is kept untouched.

- **Config**: both server and client read a single `config.yaml` with both `server:` and `client:` sections. Each side only reads its own section. Copy `config.example.yaml` to `config.yaml` and edit.

- **Sync lane（2026-11-16 review F1–F4 + 2026-11-17 C1；机制全文、三层择级、re-seed 与残余盲区 → `docs/superpowers/specs/2026-11-16-sync-lane-baseline.md`）**——铁律：① 三方分类基于客户端 baseline（缺省/畸形 = 旧 conflict 行为）。② 客户端 baseline_files 只存 server-AGREED hash——**绝不用周期收尾重扫 hash 充当 agreed**（push/pull 之后文件可能又被编辑）。③ 三层择级顺序承重：非 agreed 守卫必须先于 untouched 判定评估（颠倒即让 C1 从冲突入口复活）。④ 删除门全量回放 change_log move/delete 行（seq ASC 无界——不受 last_sync/200 行窗口约束），move 链归并到原路径。⑤ `to_delete` 路径中途被编辑 → 只跳过不删（下周期再分类；外部删除 `""` 条目恒删除外）。⑥ move 规则先判，两规则同时认领同一路径时 move 赢。

### 写入与审计

- **Change ledger (server/services/ledger.py)**: the sole write surface for `change_log` + `files`. Agent write tools (write/edit/delete/move) call `ledger.record(...)` synchronously AFTER the write — the row (files + change_log) must land before the `tool_result` frame (2026-11-20 起经 `/api/agent/stream` 事件通道) reaches the UI, whose fast-path `loadDocs()` reads the `files` table (regression 2026-11-04: async `register` left a ~1.6s window → stale tree; vault issue `agent-write-file-tree-stale`); the skip mark makes the later watcher event book nothing (exactly one row per write). Other in-process channels (executor, bgagent runner, web upload/save) call `ledger.register(path, source[, batch_id])` BEFORE writing — the watcher consumer (`ledger.consume`, the four `on_*` callbacks in main.py's `build_runtime`) books exactly one correctly-attributed row. Web direct writes (manage_docs, where the console feed must respond instantly) call `ledger.record(...)` — synchronous row, consumer books nothing. Folder rows go through `ledger.record_folder(...)` (the watcher has no folder callbacks — no double-row risk). `apply_change` / `apply_folder_change` (file_ops.py) are the row mutators: **no module other than ledger imports them**; the only in-module caller is `reconcile` (offline startup, no watcher running). 兜底与词表归一细节（unregistered writes → watchdog consumer 兜底、ACTION_TO_OP 四元归一、勿在 callers 修 op）→ `server/services/README.md`。

- **First-run indexing**: `ensure_indexed` (server/vault/orchestration/indexer.py) is a two-level check: ① segments content table empty → full rebuild of segments + graph; ② `segments_fts_docsize` row count ≠ segments row count → FTS-only rebuild (re-reads the content table, does not touch disk). `count(*) FROM segments_fts` cannot detect desync (on an external-content table it scans the content table). Not DB emptiness — the old `_self_heal` pattern is gone.

### 删除与恢复

- **Trash + actor separation（P4-4，2026-11-15；机制全文 → `docs/superpowers/specs/2026-11-15-trash-actor-separation.md`）**——铁律：① 删除恢复按 actor 分流——workbench（web/agent）走 `move_to_trash` 不拍快照，bgagent/mcp 直删 + 全量 pre-image 快照不进回收站；分支判据 `ctx.write_source == "agent"`，bgagent 起 `agent` 别名在配置加载期即拒。② 每个删除入口必须在删除点 book pre_hash（change_log 列）。③ `.trash` 必须排除于一切索引/扫描面——新扫描面照抄 `".trash" in parts` 检查。④ purge/GC 清 link 走 `remove_dead_links`（liveness-checked），且**不产新 change_log 行**（inbound 已记账）；GC 路径集从 change_log 推导并滤 `source NOT IN ('web','agent')`（理由：trash 还原的内容可逆性依赖 referrer link 文本，详见 doc）。⑤ 同步门：to_push 路径命中 `deleted_hashes` → to_delete，例外仅 (pre_hash 且本地内容已变) 或路径在服务端仍存活；`""` 条目恒 stale。

### agent 会话

- **Agent chat = 投递/订阅分离（RunHub，2026-11-20 合入）**：`POST /api/agent/chat` 是 202 纯投递（不再回 SSE），事件一律 `GET /api/agent/stream`（snapshot=1 恢复 / cursor 增量 / run_id 不符或游标过老 → 单 `resync{}` 帧即终）。铁律清单（任一违背即回归）：① 终帧关流不退避——前端投递后必须做订阅存活检查并重开（实证 bug：挂载时无 Run 订阅被终帧正常关闭 → 投递后面板全程失明）；② 两级持久化 = 出队即前缀（persist_user 推进回放边界：snapshot 前缀含 user 行、回放不重复吐泡；缺它运行中会话被列表空行滤除、done 才现身）+ 队列项完成时全量（save→边界→终帧顺序钉）；③ busy 驱动源 = snapshot 订阅对未终止 Run 的合成 `run_status{true}`，真实帧会被边界/修剪划走；④ 中断半截文本三落点（cancel/流中 APIError/崩溃）落 content-only `interrupted:true` 行（无 tool_calls——b621b5b4 不变式保留；无 id/citations；`interrupted` 出不了 LLM_FIELDS 白名单；随带 `message_id` 身份戳（反馈键 + 通道二 join 锚双职，与 done 行同形态，模型不可见；**r8 已落地**：interrupted 行可点赞点踩——前端守卫 `!msg.streaming && msg.messageId` 放行中断轮、feedback 路由寻址接受 message_id；`_append_partial` 盖戳并留痕 `agent.last_partial_message_id`，细节见通道一 spec；**2026-11-21 修订**：产行条件扩为「双空白才不产」——content 空白但本轮 reasoning 非空也产行、content 归一 `""`（reasoning 条件键不变）；空正文 interrupted 行经 LLM 边界投影结构性滤除（重放排除：模型永不见空轮，采集/join/反馈键不受影响；带 tool_calls 的 None-content 中间行不可滤））；reasoning 全文随附 assistant 行（done / 带 tool_calls 中间行 / interrupted 三落点，空则无键，旧行零迁移）——**LLM_FIELDS 白名单是唯一出域防线（r6 已落地）**，思考永不回喂 LLM。⑤ 确认挂起 300s 超时自动拒（config `ai.confirm_timeout`；pending_confirms finally 出表 + 同帧双裁决已落定者优先）。⑥ DELETE 会话必须 `runhub.discard`；活跃 Run 持 session 锁（`_evict_stale_agents` 既有 lock 检查天然覆盖，勿加跳过）；列表 `running` 由 `is_running` 现场注入（仅活跃 Run，终止保留期不算）；`app.state.runhub` 为测试 seam。**行为法源与细节（勿在此堆砌，读文档）**：`docs/superpowers/specs/2026-11-20-agent-run-detach-stream.md`（§2/§3/§6 + §9 备案）、`docs/superpowers/specs/2026-11-20-partial-output-retention.md`（通道一）、思考沉淀 `docs/superpowers/notes/2026-11-20-interruption-semantics-and-signals.md`（通道二草案在案）。回归钉：`tests/test_runhub.py`、`tests/test_agent_stream_routes.py`、test_web_ui_audit 钉家族。

- **Background agents**: configured under `background_agents:` in config.yaml. Each has independent cron, model config, target_dir scope, and prompt file. Alias must be unique; target_dirs must not overlap.

### 安全与路径

- **Path traversal guard**: `safe_rel_path()` in `server/vault/fs/vault_ops.py` (aliased `_safe_rel_path` in the route modules) rejects paths with `..` or leading `/` (checked on the backslash-normalized form — UNC/extended-length shapes included), any drive-qualified start (`^[A-Za-z]:`; on win32 a pathlib join with a drive-qualified argument discards or swaps the vault base), and a first segment of `.trash`/`.sync` (system dirs are unreachable via the web routes — upload/download/manage must never touch them; the `.sync` half is bug 0eac2e40). Deeper same-named dirs are user content.

- **No auth, no HTTPS**: LAN-only by design. Don't add authentication unless the spec changes. Exception (2026-11-17 user decision): the MCP write channel authenticates the stdio process via MD_VAULT_MCP_WRITE_TOKEN matching config.yaml mcp.write_token (constant-time compare); unauthenticated MCP stays read-only + propose. LAN HTTP surface remains auth-free.

### UI 与交互

- **移动端键盘与视口（2026-11-17 华为 Mate60 双内核复盘，详见 `issues/2026-11-17-mobile-keyboard-viewport-shift.md`）**: 应用壳高度必须走 `html,body,#app{height:100%}` 链（Tailwind `h-full`）——`h-screen/100vh` 壳在「键盘压布局视口」型内核（Edge/安卓）下不随键盘收缩，overflow-hidden 文档仍会被 scrollIntoView 程序化滚走（顶部被遮、触摸不可恢复；fixed 抽屉不受文档滚动影响，「只有主区偏」即此指纹）。所有会弹键盘的聚焦调用必须 `focus({preventScroll:true})`（防「只压可视视口」型内核平移 visualViewport，原生浏览器复现过）。安卓 Chromium 无 iOS 式 <16px 自动缩放，勿按 iOS 思路修。回归钉：`test_shell_uses_layout_viewport_height` + `test_keyboard_focus_preventscroll`。

- **锚点激活行为（2026-11-17 bug 4a2249fc 返工复盘，详见 `issues/2026-11-17-tree-anchor-activation-jump.md`）**: 文件树文件行是 `<a href="#doc/…">`（P1-4 深链），行内交互元素（rename input、⋯ 按钮）的 click 只写 `@click.stop` 仍会跳文档——点击链接后代元素照样激活祖先 `<a>` 默认行为（hash 变 → hashchange → openDoc），该旁路不经任何 Vue handler。必须 `@click.stop.prevent`。泛化红线：任何有默认行为的容器（`<a href>`/submit 按钮/label）内的交互子元素都要 `.prevent`；跳转/滚动类 bug 先按层列监听全集（Vue handler / 原生默认 action / window 级 hashchange-scroll）再看触发元素在每层之上还是之下。回归钉：`test_rename_row_click_guard`（双层断言，单层放行过半成品修复）。

- **label 容器内禁放 labelable 后代（2026-11-22 设置页隐私目录误删）**：`<label>` 内空白/标签文字/hint 的点击会被浏览器**转发激活第一个 labelable 后代**（button/input/select/textarea 皆是）——不经任何 Vue handler，`.prevent` 救不了「点在 label 空白处」那一路。设置页字段（label.st-field）需含按钮时必须 `div.st-field` + `label[for]`/`aria-labelledby` 补语义（全页 15 处审计唯一漏网即隐私目录，点非×区域删标签）。回归钉：`test_web_ui_audit.py`（label.st-field 内禁 button 后代）。

### 平台与测试备忘

- **Windows newline translation**: test fixtures write sample files in binary mode (`write_bytes`) so SHA-256 hashes match. If you add test fixtures with text files, use `write_bytes` not `write_text`, or hashes will differ on Windows.

- **跨语言搬运校验规则：搬语义不搬函数名（2026-11-22 casefold 事故）**：把加载器（Python）的校验文案直译进前端 JS 会带出 `String.prototype.casefold()` 这类 Python 原生 API——语法合法、`node --check` 放行、**Vue computed 求值即抛**，功能全灭且页面无一表象（现场只在浏览器 console，静态检查与 pytest 全绿放行）。前端 JS 禁 Python 串 API（casefold/`in` 词组/rstrip/lstrip 等），等价语义用 toLowerCase/includes。回归钉：`test_no_python_string_apis_in_frontend_js`。

- agent-browser CDP（Windows）：见文首同名节——全文已迁 `docs/superpowers/notes/2026-10-13-agent-browser-cdp-windows.md`，红线速记在彼处。

## 安全与隐私红线（2026-11-18 外部 MCP 通道评审定稿，常驻约束）

背景定性：本系统的安全当前依赖**事实性前提**（LAN≈可信、MCP stdio=本机进程、LLM 端点可指本地）——事实会随功能演进破裂，下列红线约束一切破裂前提的改动；核心哲学一句话：**把隐式信任显式化为契约**（提案通道、人审闸门、actor 信任戳、写 token 全是同一哲学的切面，新维度按此推演，不引入新哲学）。

1. **信任边界必须从事实走向契约**：把 stdio/LAN 信任面开放给网络或多租户前（HTTP transport、远程端点、设备接入），先立显式契约——设备配对、per-device credential、可吊销面。无鉴权的新暴露面 = 红线违例，功能再小也不行。
2. **禁止单点加固**：给任何面加鉴权/隔离前，先对照系统整体水位（Web 面是刻意无鉴权的，见 Gotchas「No auth」条）。不允许造出「上了锁的门挨着没门的门」的错觉；整体安全水位的迁移是用户的显式决策，不是单功能的副产品。
3. **身份由通道背书（trusted stamp）**：`actor` / `run_id` / `write_source` 一律由通道入口代码注入，永不来自模型或请求参数。任何新传输面（尤其多租户）必须**从连接凭证在服务端解出身份再注入**——trusted stamp 在传输层破功 = 全部归因与审计失效。
4. **prompt 不是安全机制**：隐私/注入边界只在工具层做成硬过滤时才存在。内容进入模型上下文的瞬间即已成事实（出域/污染），系统提示词里的行为约束（如「不要复述敏感内容」）不得作为任何保护手段写进设计。
5. **索引层也是数据，隔离必须三点齐全**：任何读取/隐私隔离必须同时覆盖 policy 读边界、读工具、索引面（FTS 摘要片段、relatedness 边、recall 台账）——缺一即筛子。注意现状：**读边界今天是刻意全库**（`ChannelPolicy` 只约束 ask 工具，reads stay unrestricted），敏感分区是 0→1 施工图，不是给现有机制打补丁。
6. **默认保守，放开显式**：网络监听默认 localhost（开 LAN 须配置显式声明并写明后果）；写通道默认关（`mcp.write_token` 空=关闭是范式，新凭证一律同构）。
7. **出域必须可知**：任何把 vault 内容送往域外端点（云端 `base_url`，含内置 AI 与 bgagent）的通道要有台账行（仿 recall 的 source 模式）+ 云端端点启动/首用提示。事实性前提破裂必须是**可见事件**，不是静默事实。
8. **快照是兜底不是第一道防线**：snapshots 每路径仅保留最近一份 pre-image（被取代行即时 GC，`snapshot_records.gc_sweep`）——「反正能回滚」不得作为放行危险写入的前提；不可逆写（mass overwrite、机器区槽覆盖、无人工闸门的 auto 轨）要人工闸门或配额/限流，不是事后回滚。

已核实的现状缺口（做相关功能时先对照，勿当已解决）：外部 MCP 无提案状态回查；change_log 对外部写归因退化为 watchdog；auto 轨（entity_index/relatedness）免鉴权可入机器区且 source_refs 不验存在性；长驻 MCP 进程在主服务器未运行时检索看不到自己的写入。

## Design Spec

Spec 已归档至 vault：`~/md-vault/projects/md-repo/specs/2026-09-27-md-sync-design.md`。架构变更前用 md-vault MCP `search_docs("md-sync design")` 检索、`read_doc("projects/md-repo/specs/2026-09-27-md-sync-design.md")` 读全文。

## 文档归档

归档位置：`~/md-vault/projects/md-repo/`（plans/ specs/ adr/ benchmarks/ review/ issues/ + 顶层杂项）。

**沉淀规则（2026-11-17 用户定）**: 需要反复修复/返工的问题在 `issues/`（本地台账，git 不入）留完整复盘（含被打回的轮次与错误假设），可提交的约束进 AGENTS.md Gotchas；一次性修好的 bug 不单独成文，账本 + commit 即可。

**vault 写入纪律（2026-11-17 用户定）**: spec/计划/新文档默认只落仓库（`docs/superpowers/` 等），保持仓库内新鲜；vault 仅在用户明说「归档」或要求直接写入时才写，不主动同步。

MCP 检索：`search_docs("<关键词>")` 全文检索、`read_doc("<相对路径>")` 读全文、`get_related_docs("<相对路径>")` 查关联。

仓库保留清单：AGENTS.md / GLOSSARY.md / README.md / MISSION.md / NOTES.md / RESOURCES.md + `server/` 下 README 与 prompts/ + `docs/superpowers/` 全目录（plans/ specs/ notes/——Gotchas 的法源索引在此）+ 2 份提案通道 HTML。

## Frontend UI Workflow

Web UI is a Vue 3 CDN SPA (no build step): `server/templates/index.html` is the shell that loads `app.js`, with component modules under `server/templates/js/` (`components/` + `utils/`——其中 `utils/chatEvents.js` 是 chat 事件分发器纯函数，node 可直 import 测，契约见文件头注释) plus `style.css`. Static assets are served via `/static` (mounted from the templates directory in `api.py`). Tailwind CSS Play CDN + Flowbite 2.5.2.

### Rules

1. **Design-first**: For any UI layout change (panel placement, responsive behavior, animation, scroll strategy), present the approach to the user BEFORE implementing. Do not iterate reactively — decide placement, transition mechanism, and content reflow strategy upfront.
2. **Copy norms** (2026-11-04, 监控页移动端评审后定稿): UI 只放用户做判断/行动需要的信息——状态、数量、时间、标题；实现 meta（后端字段名、端点路径、L2/trigger_state 这类设计概念）不进正文。刷新节奏只表达一次（页头「更新于 HH:mm」），卡头不重复。统计口径的解释进 title tooltip 或 L2 详情层，不写常驻小字。
3. **Verify before commit**: UI/CSS changes require user visual confirmation in a browser before committing. "Tests pass" ≠ "UI works". Never commit layout changes without explicit user approval.
4. **Segmented writing**: HTML files >100 lines must be written in segments (structure → styles → scripts), not one-shot. This makes diffs reviewable and errors locatable.
5. **Panel/drawer checklist**: Before implementing any slide-in panel or drawer, explicitly decide:
   - Placement: side (left/right) vs bottom vs overlay
   - Transition: `transform` (GPU, no reflow) vs `width`/`flex-basis` (triggers reflow) — prefer transform
   - Content reflow: does the main content area resize? If yes, how does centering/scrolling behave?
   - Toggle control: where is the button? Must be visible in BOTH open and closed states. Position must not jump between states.
   - Z-index layering: does the toggle get occluded by the panel? Use `position: relative; z-index` if needed.

### Known Pitfalls (learned the hard way)

- `position: absolute` panels don't participate in flex layout — main content width doesn't change, so `mx-auto` centering is visually off. Fix: add `padding-right` to main area when panel opens.
- `min-h-[Xpx]` on content children prevents the container from shrinking when a panel takes space.
- `flex-basis` transitions cause content reflow (text rewrapping) during animation. Use `transform: translateX()` instead for GPU-accelerated, zero-reflow slides.
- Scroll position is not preserved when container height changes during a transition. If this matters, anchor an element and adjust `scrollTop` per frame via `requestAnimationFrame`.
- Tailwind Play CDN generates styles at runtime — class names in JS `classList.add()` work, but dynamically constructed class strings must be complete (no partial matching).
- Hidden containers (`v-show` → `display:none`) have `clientHeight === 0` — any scroll-to-bottom/into-view executed while hidden is a no-op (F5-restore lands before the panel opens). Fix: pending flag + ResizeObserver consuming on the 0→non-zero size transition; if the element can unmount under `v-if`, re-attach the observer via `watch(ref)`.
- `v-show`-resident components mount at page load — `onMounted` may fire BEFORE boot-restore backfills props. Initialization that depends on restored state must be idempotent/retriggerable via `watch`, never a single `onMounted` pass.
- **api.js 封装的 null 语义先查再用（2026-11-22 getDocs() 304→null 陷阱）**：该函数 ETag 三态契约是 304 时返回 null（专供文件树消费者 null=保持旧值）。第二个「每次都必须拿到数据」的消费者复用会把 null 当空数据吃，叠一次性拉取守卫后永久静默失效（无任何报错）。此类消费者用 getDocsRaw()（no-store 全量）。回归钉：test_vault_dirs_uses_raw_docs_fetch。

