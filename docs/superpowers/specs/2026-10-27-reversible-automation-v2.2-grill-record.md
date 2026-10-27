# 可逆自动化 v2.1 续审记录（grill 会话 · v2.2 修订输入）
**日期：2026-10-27 · 状态：已确认（13 项拍板，frontier 清空）**
**文档定位**：对 `2026-10-27-reversible-automation-design-v2.1.md` 的续审产物 = 两路代码审计对账（提案账本线 / 写账本快照线，基于 `0e43870`）+ 13 项用户拍板 + v2a 债单。本文是 **v2.2 修订的输入件**：spec 落点索引（§三）+ 债单（§四）+ 审计现状（§一）。不直接改 v2.1 送审稿；§三的修订待过目后决定并入正文或单立 v2.2。
**前置事实（本轮核查）**：`D:/md-vault` 非 git 仓库；`suggestions/` 存量 4 投影文件（D16 不导入口径对上）；人面 = Web UI 控制台，Obsidian 非设计目标（用户明确）。

---

## 一、现状审计对账（v2a ①–⑨ × 代码，commit 0e43870）

| 期项 | 状态 | 证据 / 缺口 |
|---|---|---|
| ① 快照机制 | **已实现** | `server/snapshots.py`（379 行）：`.sync/snapshots/` blob 库 + `snapshots` 表 + `POST /api/memory/rollback`（人触发，先全验后全写）+ `rollback-calls` 列表 + 撞车守卫（整批拒）+ D18 保留策略（单活槽 / 删除类 30 天 / GC 不删活 / in-flight 永不回收）；捕捉调用点 = agent 四工具 + web 删除（单文件 + 文件夹批单 call_id）+ executor 三 handler + MCP。**缺口**：捕捉失败计数未进控制台（D19/#C2，`memory_status` 不报，仅日志） |
| ② errata 动词 | **部分** | `executor._merge_errata` → `index/errata/<stem>.md` 物化 ✓；correction 退役（`RETIRED_TYPES` parked failed + 可操作 reason）✓；固化者 verbs 代码机制 ✓（propose `ctx.verbs` 检查 + runner 盖章）。**缺口**：D23 覆盖保护**拒收**未实现（现覆盖 + pre-image，无已物化卡 pre-check；不变量 7 拒收分支无测试） |
| ③ relatedness | **已实现（数据先行）** | `relatedness` 表 + `server/vault/db/relatedness.py`（upsert / ignore 标记）；回读注入含 ignored 遥测 ✓；排序接线 v2b（与 D30 一致） |
| ④ 提案记录进库 | **部分** | `proposals` + `proposal_transitions` 表（schema.py:235-273）✓；propose 工具（零落盘报错 / runner 盖章 actor+run_id+verbs / 越权拒 / 退役动词重定向）✓；SuggestionsGate 硬关闭 + v1 钩子 `validate_suggestions_write` 退役（测试钉住）✓；控制台读库 + 无编辑端点 ✓；零文件落 + 零 change_log 行测试 ✓。**缺口**：条件 UPDATE（D29，现为读侧检查 + `WHERE id=?`）；expired→revive（#B6，代码中 expired 终态） |
| ⑤ 协同终审四工具 | **未启动** | `list_proposals` / `get_proposal` / `adopt_proposal` / `reject_proposal` 零实现（控制台 adopt/reject API 已覆盖人面） |
| ⑥ 三条守卫 + 身份带外 | **部分** | 可信盖章（runner.py:439-442 注入 write_source/run_id/verbs）✓；直写通道身份带外 ✓；回读注入 ✓（未决计数 / 失败 top5+reject_reason / relatedness+ignored）。**缺口**：法律层 `verbs:` fail-fast（config 未声明 = unrestricted propose，越权守卫对未声明角色静默） |
| ⑦ entity schema 字段化 | **已实现** | propose per-verb payload schema（字段化，禁散文）；机器区 FTS 池外（`EXCLUDED_INDEX_DIRS`，不变量 6 注记一致） |
| ⑧ 通知纪律 | **未启动** | `notify.py` 本 diff 仅一行注释；D33 三条量级守卫（日上限 / 同类合并 / 推送预算进法律层）零实现 |
| ⑨ 控制台形态 | **部分** | 回滚入口 + rollback-calls + 读库详情（justification 替代文件读）✓。**缺口**：degraded 计数（同①） |
| （超集） | MCP 捕捉提前落地 | P2 范围（R1）已实现于 v2a 代码：`mcp_server.py` capture + finalize，跨进程单语句 UPDATE + SQLite 单写者。正向偏差，不改分期 |

**结构性偏差（非分期问题）**：④ 条件 UPDATE、④ revive、② D23 拒收、死边 `failed→pending`（TRANSITIONS 有定义 + 测试断言合法，但全 server 无生产调用点，且与「failed 旧记录只读存档」矛盾）。

---

## 二、13 项决定（Q1–Q13，均用户拍板）

| # | 拍板 | 要点 |
|---|---|---|
| Q1 | vault VCS = **接受降格回写** | vault 非 git；「版本控制」降格为账本 hash 链（仅元数据，无内容）；不引入 git（md-sync 是唯一一致性机制，vault 内 git = 第二真相源）；快照库 = 全系统唯一内容凭据，地位明文化 |
| Q2 | glossary = **5 条修正 + 新词入典** | 见 §六 |
| Q3 | v2a errata 单槽 = **接受 + 文案要求** | 同 stem 至多一张已物化卡；第二张拒收 + surface；surface 文案必须携带人出口（「旧卡 `<path>` 已确权：删除旧卡后可重新物化；v2b 唯一文件名上线后自动解决」） |
| Q4 | 条件 UPDATE = **兑现 D29** | `UPDATE ... WHERE id=? AND status=?` + 影响行数 0 → 非法前置态错误；双通道并发 race 测试钉不变量 11 |
| Q5 | expired→revive = **实现，低优先级** | 人 only 路由（条件 UPDATE `expired→awaiting_review`，actor=human，TTL 重置）+ 静态断言 agent 工具面无 revive 入口（不变量 12） |
| Q6 | 状态机形状 = **删死边 + 图注** | 删 `failed→pending`（死边 + 违反只读存档语义）；spec 图补注「图 = 人决策面视图；`pending`/`processing` = 执行器机械入场态，不在人决策面」 |
| Q7 | bgagent `tools:` = **必须显式声明** | `None`（= 全量工具，fail-open）→ 启动 fail-fast 校验报错，并入 config.py 既有校验族（alias 唯一 / cron 合法 / prompt 存在 / target_dir 不重叠） |
| Q8 | D23 errata 拒收 = **实现，低优先级** | 落卡前 pre-check（槽内已有已物化卡 → 拒收 + surface，文案 = Q3）+ 不变量 7 拒收分支测试 |
| Q9 | 法律层双 fail-fast + config 迁移 | ① `tools:` 显式（Q7）；② `propose ∈ tools` 而未声明 `verbs:` → 启动报错（无 propose 的角色不受限，如 sentinel）；config 迁移：consolidator = 读工具 + propose、`verbs: [entity_index, relatedness, summary, errata]`（D31）、target_dir → 机器区；sentinel = 仅读、删 v1 残留 `write_doc`、target_dir → 机器区 |
| Q10 | **报告通道**（新宪法级通道，ADR-0004） | 介质 = 机器区文件（`index/`）；**无落地动词角色（哨兵 / 叙事者）= 读工具 + `write_doc`（写边界 = 机器区）**，不走 propose；**分界线：提案角色（固化者）= propose 唯一写入口，无直写**（防终审闸门被绕）；人面 = 控制台报告 API（列表 + 详情 + 深链）+ 推送（D33），主 agent 用既有 `read_doc`（index/ 可读、不进搜索图谱，不新增读工具）；bgagent 产出漂移叠加 = §十一 范围外（未来优化方向）；**人面声明：Web UI 控制台 = 唯一设计人面，Obsidian 不在设计目标内** |
| Q11 | `apply_change` 纪律 = **chokepoint 绝对化** | file_ops 增 `apply_folder_change`（同模块同守卫），web 文件夹三路径（create_folder / folder-move / folder-delete，现裸 `log_change`）改调；「一切 change_log 行经 file_ops 变更面」无例外 |
| Q12 | v2a 范围 = **不动，债单生效** | §四 10 项即 v2a 实施清单 |
| Q13 | 自编辑捕捉 = **v2a 维持排除 + 前提修正** | spec 前提句删「Obsidian 版本历史兜底」（双重落空：非 git + 不用 Obsidian）→ 换诚实表述（会话内 undo 为唯一兜底，2026-10-27 用户知晓记录）；选项 A（Web UI 自编辑捕捉 = 机械扩展 api.py 既有三件套，单活语义，一步 undo）记为**已认可的未来优化**（§十一），D20 触发器原文保留（「用户提出自编辑 undo」）；**残留暴露明示**：Mac 本地直接改 + sync push 到服务端 = 未捕捉（sync 协议路径非 Web UI 编辑面；如需覆盖 = P2 在 sync push 落盘路径接同一捕捉） |

---

## 三、v2.2 spec 修订落点（× v2.1 章节，待过目后并入正文或单立 v2.2）

| v2.1 位置 | 修订内容 | 来源 |
|---|---|---|
| §一.2 根立场 | 不动；「每次不可逆操作留下可恢复的凭据」与 D20 排除的张力由 Q13 裁决记录（暂缓，触发器保留） | Q13 |
| §二 表 · 机器区写者行 | 「唯一执行器 + 提案机制」→「唯一执行器 + 提案机制 + **报告通道**（非提案角色 write_doc，机器区 scope）」 | Q10 |
| §2.2 | 「errata 卡文件浏览器可见 = 惰性呈现」降格为陈述（人面本就收敛控制台）；新增前提行：「人面 = Web UI 控制台；Obsidian 不在设计目标内」 | Q10 |
| §三.1 捕捉范围（D20 段） | v2a 口径不动（人类通道 = 删除仅）；前提句修正（删 Obsidian 兜底 → 诚实表述 + 知晓记录）；残留暴露（sync push 不捕捉）明示 | Q13 |
| §三.1 保留策略段 | 「版本控制」回写：vault = 非 git；版本控制 = 账本 hash 链（仅元数据，无内容）；快照库唯一凭据地位明文化 | Q1 |
| §五.1 状态机图 | 补注：图 = 人决策面视图；`pending`/`processing` = 执行器机械入场态，不在人决策面（代码 8 态 vs 图 6 态对齐） | Q6 |
| §五.2 / §三.3 | 协同终审四工具（v2a⑤）实施注记：与控制台 API 同 `decide_record()` 单路径（条件 UPDATE，Q4） | Q4/⑤ |
| §七.2 工具面表 | 增行：**报告通道** —— 无落地动词角色 = 读工具 + `write_doc`（写边界 = 机器区 `index/`），无 propose；分界线句：提案角色无直写（直写专属非提案角色，否则终审闸门可被绕过） | Q10 |
| §八 分期表 | v2a 债单 = 本文 §四（10 项）；「漂移叠加 / 自编辑捕捉」不在任何期，归 §十一 | Q10/Q13 |
| §十一 范围外 | +2 条：① bgagent 产出漂移叠加（重复 run 产出重叠 / 叠加去重）= 未来优化方向；② Web UI 自编辑捕捉 = 已认可的未来优化（触发器 = D20 原文「用户提出自编辑 undo」）；残留暴露（sync push）同条明示 | Q10/Q13 |
| §九 修正案 1 | 机器区写者面同步增报告通道（措辞与 §二 表一致） | Q10 |
| GLOSSARY.md | 5 条修正 + 13 个新词（含报告通道） | Q2 |
| ADR | `0004-report-channel.md`（报告通道：非提案角色的机器区直写） | Q10 |

---

## 四、v2a 债单（10 项，Q12 拍板生效；顺序 = 优先级的自然序，实施细节另排）

| # | 条目 | 来源 | 优先级 |
|---|---|---|---|
| 1 | 条件 UPDATE（`proposals.advance` 加 `AND status=?` + 影响行数 0 → 错误）+ 双通道并发 race 测试（不变量 11） | Q4 / D29 | 高 |
| 2 | D23 errata 拒收：`_merge_errata` pre-check 已物化卡 → 拒收 + surface 文案（Q3 口径）+ 不变量 7 拒收分支测试 | Q8/Q3 / #C1 | **低** |
| 3 | expired → revive 人 only 路由（条件 UPDATE，actor=human，TTL 重置）+ agent 工具面无 revive 入口静态断言（不变量 12） | Q5 / #B6 | **低** |
| 4 | 协同终审四工具（`list_proposals` / `get_proposal` / `adopt_proposal` / `reject_proposal`，与控制台 API 同 `decide_record()` 单路径 + 红线三条 + 对话回执 `actor: human, via: 主agent`） | v2a⑤ | 中 |
| 5 | 捕捉失败计数进控制台待确认面板（`memory_status` 上报 degraded 计数；D19/#C2「v2a 运行期内降级不可静默」） | v2a①⑨ | 中 |
| 6 | 通知量级守卫（日上限默认 20 / 同类合并 / 推送预算进法律层 config / 打开率遥测；D33） | v2a⑧ | 中 |
| 7 | 法律层双 fail-fast：bgagent `tools:` 必须显式；`propose ∈ tools` 须声明 `verbs:`（并入 config.py 启动校验族）+ config.yaml 三角色迁移（consolidator verbs=[entity_index,relatedness,summary,errata] + target_dir→机器区；sentinel 删 write_doc + target_dir→机器区；narrator target_dir→机器区） | Q7/Q9 / D3 | 高 |
| 8 | 报告通道落地：spec 修订（§七.2 行 + §二 写者 + 分界线 + §九.1）+ 控制台报告 API（列表 + 详情 + 深链）+ 哨兵 / 叙事者 `target_dir → index` + 漂移叠加记 §十一 | Q10 | 中 |
| 9 | `apply_folder_change`（file_ops 同模块同守卫）+ web 文件夹三路径改调 + AGENTS.md 纪律段同步（「一切 change_log 行经 file_ops 变更面」）+ 测试 | Q11 | 中 |
| 10 | 状态机死边 `failed→pending` 删除（TRANSITIONS + 对应测试改钉「failed 无出边，补救 = agent 重 propose 新记录」）+ spec 图补入场前缀注 | Q6 | 低 |

---

## 五、未来优化方向（范围外，非债务）

1. **bgagent 产出漂移叠加**：重复 run 的产出重叠 / 叠加去重（Q10 用户原话：「未来的优化方向」）。
2. **Web UI 自编辑捕捉**：`save_doc` / `PUT /api/docs` 路径接既有三件套（capture → finalize → gc_after_capture），单活语义 = 一步 undo（回到上一次 Web 保存前），actor=web，同一回滚 UI。触发器 = D20 原文「用户提出自编辑 undo」（2026-10-27 用户：暂无需求，方案认可）。
3. **sync push 路径捕捉**：Mac 本地直接改 + sync push 的残留暴露（Q13 明示项），如需覆盖 = P2 在 sync push 落盘路径接同一捕捉。

---

## 六、GLOSSARY 同步清单（Q2 批准，2026-10-27 已落 GLOSSARY.md）

**修正 4 条**：sentinel（archive 退役 → 只读报告 + 报告通道）、write registry（`batch_id` / `consumer_skip`）、write gate SuggestionsGate（v1 钩子退役 → 结构性硬关闭）、related edge（与 relatedness 分立）。
**新词 13 个**：propose 工具、提案记录、decide_record、越权拒绝、身份带外、人执笔写路径、报告通道、回读注入、relatedness、errata、记忆化、单活快照、degraded 模式。
（write_source 词条核对后仍准确，未改：propose 零文件落、不经 write registry，「4 个 ask 工具」口径不变。）
