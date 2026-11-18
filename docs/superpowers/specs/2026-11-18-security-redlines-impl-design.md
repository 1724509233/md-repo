# 安全红线落地设计：LAN 硬闸门 / 隐私读边界 / egress 审计

- 日期：2026-11-18
- 状态：待评审
- 范围：红线三件套（AGENTS.md「安全与隐私红线」§5/§6/§7 的兑现）。**不含** oracle P1（快照保留 N 份、auto 轨降级、MCP 增量索引）与**同步面隐私排除**（后者与 API 鉴权同包，另行立项）。

## 0. 背景与决策记录

外部 MCP 通道评审（2026-11-18）后定稿 8 条红线。本 spec 落其中三条机制。经逐题确认的关键决策：

| 决策点 | 结论 |
|---|---|
| 读边界声明处 | 全局 config `privacy.dirs`，仅对**模型面**生效；Web 控制台（用户自己的眼睛）不受限 |
| 过滤粒度 | **目录级、查询时现算**（否决文件级物化标记：派生状态制造陈旧类 bug，而漏点类别与现算完全相同） |
| 移动语义 | 方向不对称：移入隐私=允许；**移出隐私=模型面一律拒绝**（人经 Web 移动=确权） |
| 同步 | 本轮排除（与 API 鉴权同包） |
| LAN 闸门 | 硬闸门：非回环 host 缺 `lan_exposure: true` → 启动失败退出 |
| egress 粒度 | 文档行级（ledger.db 新表）；交互 agent + bgagent；MCP/Web 不记 |
| 隐私目录可见性 | Web 面三处回显（文件树/编辑器徽标/搜索结果锁），advisory 不拦截 |

## 1. LAN 硬闸门

**配置**：`server:` 节新增 `lan_exposure: bool`（缺省 false）。

**判定**：host ∈ 回环集合 {`localhost`, `127.0.0.1`, `::1`} ∪ `127.0.0.0/8` 网段 → 放行。其余（含 `0.0.0.0`）在 `lan_exposure != true` 时 `load_server_config` 之后、uvicorn 启动之前报错退出，错误文案必须写明后果（「局域网内任何人可读写 vault、批准提案」）与开法（显式加一行）。

**实现**：`is_loopback(host)` helper 落在 `server/config/`（与 egress 共用，见 §3——一处定义）。`config.example.yaml` 默认值改 `127.0.0.1`，注释保留 `0.0.0.0` 示例并注明需要 `lan_exposure: true`。

**闸门落点（钉死）**：判定在 `server/main.py` 启动路径（`load_server_config` 之后、`uvicorn.run` 之前），**不进 `load_server_config` 共用 loader**——MCP stdio 走同一 loader（mcp_server.py:279），放进 loader 会让存量 `0.0.0.0` 配置的 MCP 启动即死，违背「破坏性只此一次」；MCP stdio 不监听任何端口，本闸门与它无关。错误文案本身进回归钉（含后果与开法字样，§5-9）。

**破坏性**：既有 config.yaml 的 `0.0.0.0` 升级后启动失败一次、加一行即解——这是红线 6「默认保守、放开显式」的本意。

## 2. 隐私读边界

### 2.1 配置形态与校验

```yaml
privacy:
  dirs:            # vault 相对首段目录名
    - 日记
    - finance
```

- 加载校验：拒绝绝对路径、`..`、首段 ∈ {`.sync`, `.trash`, `suggestions`}（防把系统目录划入造成行为歧义）、空串、重复（去重）；条目不含 `/`（只支持**首段目录**级，见 §2.8 残余）。
- 缺省 `[]` = 机制惰性：全部行为与今天逐字节等价（安全默认范式）。
- 生效进程：主服务器（WebContext）与 MCP stdio（`main()`）经 §2.2 `PrivacyConfig`（mtime 活视图，**非启动时冻结值**）持同一语义；agent runtime 经 `ChannelPolicy` 新增字段 `privacy: PrivacyConfig`（共享引用，配置变更穿透已构造的 policy 对象）。

### 2.2 公共设施 `server/privacy.py`

- `is_privacy(dirs, path) -> bool`：首段命中；反斜杠规范化与 `_first_segment` 同语义。
- `privacy_clause(dirs, col) -> tuple[str, list]`：SQL 谓词构造器，生成 `NOT (col LIKE ? ESCAPE '\' OR ...)`，`%_` 转义；空名单返回 `("", [])` 调用方短路。
- **大小写语义（两侧统一为 ASCII 不敏感）**：Python 侧目录名与首段各 `casefold()` 后比对；SQL 侧依赖 SQLite `LIKE` 的 ASCII 大小写不敏感默认（非 ASCII/CJK 无大小写概念，不受影响）。两侧行为一致性由 §5-7 用例钉死（`DIARY/x`、`Diary/x` vs 名单 `diary`）。
- **统一措辞常量** `INVISIBLE_MSG`：所有模型面拒绝文案共用同一措辞（拒绝面是「隐私 vs 不存在」的判别 oracle，但目录名本就来自用户自己的 config——措辞统一即钉死该 oracle 的行为面）。
- **`PrivacyConfig` 加载器（mtime 缓存）**：读 config.yaml 的 `privacy.dirs`，按文件 mtime 失效重读——主服务器与 MCP stdio 共用。配置变更无需重启即对「下一句查询」生效（§2.7 的承诺以此成立），消除长驻 MCP 的陈旧窗口。
- 目录级语义只在这一个模块定义——策略、gate、SQL、UI 全部调它。

### 2.3 模型面收口清单（漏点类别唯一：新扫描面忘带过滤 → §5 回归钉封死现有面）

| 面 | 位置 | 机制 |
|---|---|---|
| read_doc（agent+MCP） | `validate_tool_call` 路径循环 | `not is_ask and is_privacy(p)` → 拒绝；措辞 "invisible to agents"。**ask 工具路径不拦**——写进隐私区允许 |
| search_docs（agent+MCP） | `ai/search.rag_search` 加 `privacy_dirs=None` 参 | 模型面传名单注入 `privacy_clause`；Web 搜索路由传 None |
| get_related_docs（agent+MCP） | `graph_get_related_with_machine` 加同名参 | user 层 + machine 层结果双侧过滤 |
| MCP get_related_docs | `mcp_server.py` | 补挂 `validate_tool_call`（连带封掉现核实的裸路径消毒缺口） |
| list_files（agent） | 磁盘扫描循环 | `is_privacy` 过滤 |
| recent_changes（agent） | change_log SQL | `path` + `from_path` 双列 `privacy_clause` |
| move 出界（全通道） | 新 `PrivacyGate` 加入 **VAULT_GATES** | ask 工具 `from_path`/`moves[].from_path` 命中而对应 `to_path` 不命中 → 拒绝；空名单天然惰性 |
| relatedness 合并 | executor `_merge_relatedness` | 任一端点 `is_privacy` → `_MergeFailed`（防提案通道把隐私路径提议成可见边） |
| entity_index 合并 | executor `_merge_entity_index` | `source_refs` 逐行渲染 `- source: <ref>` 进 entities.md（executor.py:156-157）——合并时 `is_privacy` 命中的 ref **整行剔除**（全剔除则 `_MergeFailed`）；auto 轨无人工闸门，此面与 relatedness 同表同险 |
| RAG 降级回退 | `ai/search.py::_legacy_rag_search` | FTS 损坏/缺表时整库 rglob 直返——回退入口同样过 `is_privacy`（红线 5 索引面在降级态同样成立；§5-1 降级态钉） |

### 2.4 方向不对称的移动语义

- 模型通道：移入隐私允许（移动不读内容）；移出拒绝（等价于替用户做披露决定）。
- Web 控制台 move/rename：不受限（人拖文件=确权）。
- 隐私文件的写/编辑（write_doc 覆写、edit_doc 追加）：允许——边界只限读。

### 2.5 回显（Web 面，红线 7 的镜像：边界本身可见，防静默配置失效）

1. 文件树：隐私目录行小锁 glyph + muted 色调，子文件继承弱色调（不逐行挂锁）。
2. 编辑器头：当前文档在隐私目录 → 「锁 模型不可见」徽标。
3. Web 搜索结果行：隐私命中项挂锁——「你看得见、模型看不见」差集可视化。

三处均为中性视觉（非危险红），仅回显不改写名单（单一真相源=config.yaml）。细节由 @designer 实施时定稿；按前端规则浏览器目视确认后才 commit。

### 2.6 保存/写入引用警告（advisory，不拦截）

- Web 保存路由：content 中出现对隐私目录的**显式引用**（`[[目录/...]]` wikilink、裸路径串）→ 响应带 warning 数组，控制台提示「本次保存引用了隐私目录 N 处…（内容已保存；模型可见这些引用）」。纯前缀字符串匹配，不读隐私内容。
- agent write/edit 结果串尾部追加同款 advisory（模型写出的内容出现隐私路径 = 自我纠正信号）。
- **摘录相似度检测明确不做**（需要读全部隐私内容做模糊匹配，不可靠且养内容指纹面）；未来项=本地模型后台哨兵（见 §7）。

### 2.7 零状态收益（相对被否决的标记列方案）

无标记列、无迁移、无配置清扫：目录划进/划出隐私名单，**下一句查询立即生效**（此承诺以 §2.2 的 mtime 缓存加载器为机制支撑——「立即」指无物化状态需重建，进程侧靠 mtime 失效，非指绕过缓存的毫秒级）。索引层照常收录（`files`/segments/graph 不变）。

### 2.8 残余边界（文档化，不机制化）

1. 非隐私文件正文里的 wikilink/摘录是用户自己写的内容，机制不审（§2.6 仅提示显式引用）。
2. 提案记录 `source_refs` 可能出现隐私**路径名**：agent 无法读隐私文件但**能命名其路径**（如经①的 wikilink 得知）——`entity_index` 渲染面已收口（§2.3），relatedness 端点已拦；剩余面（提案记录本体、trail、通知深链）为路径级元数据，日志/记录不回溯清洗。
3. 同步链路本轮不动（设备≈人，与 API 鉴权同包另行决策）。
4. `privacy.dirs` 只支持首段目录；嵌套目录粒度（`projects/secret/`）为后续扩展（`is_privacy` 接受多段前缀即可，接口已留）。

## 3. egress 审计

### 3.1 表结构（ledger.db——审计是权威数据；`init_ledger_db` 幂等建表即迁移）

```sql
CREATE TABLE IF NOT EXISTS egress_log (
  id            INTEGER PRIMARY KEY,
  ts            REAL NOT NULL,
  channel       TEXT NOT NULL,      -- 'agent' | 'bgagent'
  actor         TEXT NOT NULL,      -- 'agent' | bgagent alias（proposals.actor 同语义：通道注入，不可伪造）
  run_id        TEXT NOT NULL DEFAULT '',   -- bgagent 批次；交互恒 ''
  endpoint_host TEXT NOT NULL,      -- base_url 的 host[:port]；永不含 key/path/query
  model         TEXT NOT NULL DEFAULT '',
  path          TEXT NOT NULL,      -- vault 相对路径，一篇一行
  via           TEXT NOT NULL       -- 'read' | 'search' | 'related' | 'recent'（首触方式）
);
CREATE INDEX IF NOT EXISTS idx_egress_path ON egress_log(path);
CREATE INDEX IF NOT EXISTS idx_egress_ts   ON egress_log(ts);
CREATE INDEX IF NOT EXISTS idx_egress_run  ON egress_log(run_id);
```

记账单元：一次 run（bgagent=一个批次；交互=一次 prompt 的 agent loop）内每个**实际进入过模型上下文**的文档恰好一行（loop 内去重集，重复读保留首触 via）。

### 3.2 触达采集（不碰 API 报文）

- 记：`read_doc`（args.path 全文进上下文）、`search_docs`/`get_related_docs`/`recent_changes`（结果带路径+片段）。
- 不记：`list_files`（仅路径名，隐私已被上游过滤）、`write_doc`/`edit_doc`（送模型的是模型自产字节+回执，非文档既有内容）。
- 管道：四读工具 `ToolContext.last_meta` 各加 `paths: [...]`，runtime tool_result 后处理并入 per-loop 去重集；loop/run 终一次性 flush；fail-open（仿 recall：审计不反噬主流程，异常必落日志）。bgagent flush 挂钩在 RunReport 生成同站（`_write_report_and_prune` 时机），同 code path 两 sink。

### 3.3 端点判定

`parse_endpoint(base_url) -> host[:port]`；非回环（`is_loopback`，§1 共用）才记账；`base_url` 缺省 → `(provider default)` 视作云。本地 Ollama 下本表恒空。

### 3.4 双保险不变量

`egress_log.path ∩ privacy = ∅`：上游过滤保证隐私文档不可能出现在工具结果；flush 时再跑 `is_privacy` 跳过（防未来收口面漏网时连审计都泄）。

### 3.5 读取面与提示

- `GET /api/egress?path=&host=&run_id=&limit=50`（ts 倒序）——「这篇文档到过哪些云端端点」直达；`run_id` 参数与控制台 run 通知 join。
- 监控页一行「当前出域端点：N 个 · 更新于 HH:mm」+ 详情表（时间/通道/actor/model/文档）。host 可展示（判断需要），key 与完整 URL 段永不出现。文案走既有规范。
- 启动日志：全部生效模型端点分类播报（云/本地各一行）——前提破裂可见。

### 3.6 保留与关系约束

- 90 天滚动，executor 周期顺带清（与 snapshot GC 同站，best-effort）。
- **与 RunReport/notifications 的关系（实现红线）**：两者键重叠（run_id/actor/ts）、语义相反（产出账本 vs 摄入账本），**不合并**。任何会被展示层策略折叠/剪枝/日上限合并的载体（notifications、report 文件）不得兼任审计账本；join 只发生在 UI 层。

## 4. 横切约束

1. **空名单/本地端点 = 逐字节等价**：`privacy.dirs: []` 且端点全回环时，全部行为与改动前一致（总等价测试钉）。
2. `is_loopback` 与 `is_privacy`/`privacy_clause` 各只有一处定义，策略/gate/SQL/UI 全调公共函数。
3. MCP stdio 与主服务器读同一 config.yaml；两进程无状态同步需求。
4. Windows：路径比较走既有反斜杠规范化惯例；LIKE 前缀用 `/` 正形式（与 files 表存储形式一致）。

## 5. 测试策略（回归钉清单）

1. 收口面反向钉（每面一条）：名单非空 → read_doc 拒 / search 无命中 / **RAG 降级回退（_legacy_rag_search）无命中** / related 两层无 / list_files 无 / recent_changes 无 / move 出界拒 / relatedness 合并 failed / **entity_index 合并隐私 source 行剔除（全隐私 → failed）**。
2. 等价总钉：空名单 → 全链路行为与基线一致。
3. Web 面守恒钉：名单非空时文件树/`/api/docs`/Web 搜索/保存/`/api/sync` 响应零 privacy 过滤痕迹（防误伤）。
4. 方向不对称钉：agent 移入允许、移出拒；Web move 不限；隐私文件写编辑允许。
5. LAN 闸门钉：`0.0.0.0` 无 flag 启动报错；回环写法矩阵（localhost/127.0.0.1/127.2.3.4/::1 + flag 组合）。
6. egress 钉：回环端点零行；云端端点 run 后按实际读过的文档逐行；未读文档不出行；run 内去重；隐私路径 flush 时跳（双保险）；fail-open（表写失败不炸 run）。
7. `is_privacy` 边界：反斜杠、深层同名目录（`notes/suggestions/` 教训同款）、`%_` 转义注入、嵌套前缀不误伤（`diaryx/` 不命中 `diary/`）；**大小写一致性双钉**：`is_privacy`（`DIARY/x`、`Diary/x` vs 名单 `diary`）与 `privacy_clause` 生成的 SQL 实际查询结果同判。
8. 引用警告钉：wikilink/裸路径命中；写进隐私区不警告（那是合法动作）；advisory 不改变保存结果。
9. LAN 闸门错误文案钉：报错字符串含「后果」与「lan_exposure」开法字样；且 MCP stdio 以 `0.0.0.0` 配置启动**不受闸门影响**（loader 无判定）。
10. config 校验钉：绝对路径 / `..` / `.sync`/`.trash`/`suggestions` 首段 / 空串 / 含 `/` 条目全部拒绝，重复去重。
11. 端点播报钉：启动日志含云/本地分类行；`/api/egress` 路由（path/host/run_id/limit 过滤 + 倒序 + 默认 50）；90 天滚动清理在 executor 周期生效；`PrivacyConfig` mtime 变更 → 两进程语义下的新值生效钉。
12. 前端三件套 + 警告 UI：浏览器目视确认后 commit（既有规则）。

## 6. 交付切块（供 plan 参考，非承诺）

A. config（lan_exposure/privacy.dirs 解析校验）+ LAN 闸门 + is_loopback → B. server/privacy.py（含 PrivacyConfig）+ validate/gate/搜索/关系/list/recent/executor（两合并）/fallback 收口 + **Web 标注回传**（tree/doc 负载加 `privacy: bool`——Web 面唯一的隐私触点，纯展示标注非过滤）+ 钉 → C. egress 表 + runtime 采集 + /api/egress + 播报 → D. 前端三件套 + 警告 UI。A 独立可先行；B/C 依赖 A 且可并行；**D 依赖 B**（锁/徽标数据源 = B 的标注负载）。

## 7. 非目标 / 后续项

- 同步面隐私排除（与 API 鉴权同包）；嵌套目录粒度；摘录泄漏的本地模型哨兵；文档级/frontmatter 标记；`/api/egress` 导出；隐私名单的 Web 编辑 UI。
- **接受的事实性残余（记录在案）**：经反向代理访问时 `lan_exposure` 闸门管不到代理本身的暴露（前提转移给部署者）；agent loop 中途崩溃丢失本次未 flush 的 egress 行（fail-open 审计的既定代价，与 recall 台账同款）。
