# L5 记忆控制台 UI 测试用例（债单 #5/#8 UI 半）

> 验证对象：commit `6e812b3`（报告区 + 报告详情第三面 + L0 快照降级 chip + 通知深链报告路由）及其回归面。
> 执行方式：手动（浏览器 + curl + sqlite3），2026-10-28 编写。
> 计划出处：`2026-10-27-v2a-debt-list-10.md` Task 13/14；L5 commit 标注 visual-confirmed，本用例补视觉 + 功能双重验证。

## 0. 环境速查（已核实 2026-10-28）

| 项 | 值 |
|---|---|
| 服务 | `http://127.0.0.1:9888`（在跑，PID 96242；已确认运行新代码——`/api/memory/status` 返回 `snapshot` 键） |
| **服务端 vault** | **`~/md-vault`**（⚠️ 不是仓库的 `./vault`，后者是客户端 vault；所有文件/DB 操作都打 `~/md-vault`） |
| 控制台入口 | 左侧 rail 的【通知】按钮（`onMemoryRailClick`）；再点一次 = 关闭；**重新进入恒回队列面**（detail/report 双清） |
| 轮询 | 30s（`pollMemory`）。等变化 = 等 ≤30s，或 **F5 刷新页面立即重拉**（onMounted 即 poll） |
| DB | `~/md-vault/.sync/index.db`，**WAL 模式 → sqlite3 CLI 直接 INSERT 安全** |
| 本地 config.yaml | `background_agents:` 整段被注释 → sentinel/narrator/executor 不会自动跑 → **全部数据准备走手动（R1–R4）** |
| 起始数据（已核实） | 0 提案、0 通知、`index/reports/` 不存在 → 天然从空态用例开始 |
| markdown 助手 | `utils/markdown.js` = marked + DOMPurify + hljs + mermaid（报告正文支持代码高亮与 mermaid 图） |

## 0.5 执行通道：openchamber_web（已验证可用，2026-10-28）

能力：`browser.open`（独立 tab）/ `browser.snapshot`（文本 + 交互元素 + console 报错 + 全部 tab 列表）/ `browser.click`（selector）/ `browser.capture`（截图存 `.openchamber/screenshots/`）/ `browser.resize`。无任意 JS eval（JS 层断言仍走 agent-browser 或 DevTools）。

**⚠️ 必做**：打开后先 `browser.resize viewport="desktop"`（1440×900，已验证）。面板默认 fill 视口可能 < 1024 CSS px → 应用落入移动端布局（控制台变左侧 overlay），视觉验证会失真。
**⚠️**：窗口从 < 1024 拉到 ≥ 1024 不会自动重开控制台；反过来缩窄会自动关闭并清 detail/report 状态（E3 用例即此行为）。

已顺手验证：A1 空态（报告分区头 + 计数 0 + 「只读 · 建议不执行」+ 虚线「暂无报告」，与「无待审提案」并列）、C1（L0 行无 degraded chip 且不占位）、E4（主区无回流）——截图 `l5-desktop-1440-*.jpg`。

## 1. 数据准备配方

### R1 报告文件（落盘即被 30s 轮询拾取，无需重启）

```bash
mkdir -p ~/md-vault/index/reports/sentinel ~/md-vault/index/reports/narrator

# 主力报告：覆盖全部 markdown 形态（标题/列表/表格/引用/代码/mermaid）
cat > ~/md-vault/index/reports/sentinel/2026-10-28.md <<'EOF'
# Sentinel 只读报告 · 10-28

> 分析认为：以下仅为建议，不执行搬动。

- 召回遥测：failed_7d = 21，docs_tracked = 98
- 衰减分桶：7d 活跃 12 · 30d 45 · 候选归档 7

| 文档 | 权重 | 最近命中 |
|---|---|---|
| watchdog-design.md | 32.3 | 2d |

```python
def sample():
    return "code highlight"
```

```mermaid
graph LR; a[召回] --> b[分桶]; b --> c[归档候选];
```
EOF

# 对照报告（narrator 通道，验证多 alias 并列）
printf '# Narrator 周报 · 10-28\n\n本周概览正文。\n' > ~/md-vault/index/reports/narrator/2026-10-28.md
# 无 # 标题的文件（title 回退 alias 用）
printf '无标题正文一行\n' > ~/md-vault/index/reports/sentinel/2026-10-27.md
# 空文件（B5 空正文用）
: > ~/md-vault/index/reports/narrator/2026-10-26.md
```

### R2 种子通知（深链 D 组；bell 下拉 = `/api/notifications`，`read=0` 才显示）

```bash
sqlite3 ~/md-vault/.sync/index.db <<'SQL'
-- D1：只有 report_paths → 报告详情面
INSERT INTO notifications (ts, alias, name, status, report_path, summary, read, ops_total, files_changed, kind, proposal_paths, report_paths)
VALUES (CAST(strftime('%s','now') AS REAL), 'sentinel', '哨兵', 'ok', '', '报告已生成', 0, 1, 1, 'batch', '', '["index/reports/sentinel/2026-10-28.md"]');
-- D4：只有旧字段 report_path（单文件深链，回归）
INSERT INTO notifications (ts, alias, name, status, report_path, summary, read, ops_total, files_changed, kind, proposal_paths, report_paths)
VALUES (CAST(strftime('%s','now') AS REAL), 'narrator', '叙事', 'ok', 'welcome.md', '旧字段深链', 0, 1, 1, 'batch', '', '');
SQL
```

D2/D3 需要真提案 relpath：先跑 R3，记下输出 `P_ID`，再补插：

```bash
sqlite3 ~/md-vault/.sync/index.db <<SQL
-- D2：只有 proposal_paths → 提案详情面（回归）
INSERT INTO notifications (ts, alias, name, status, report_path, summary, read, ops_total, files_changed, kind, proposal_paths, report_paths)
VALUES (CAST(strftime('%s','now') AS REAL), 'consolidator', '固化', 'ok', '', '提案待终审', 0, 1, 1, 'batch', '["suggestions/2026-10-28_summary_p${P_ID}.md"]', '');
-- D3：两者都有 → 必须开提案详情面（优先级回归）
INSERT INTO notifications (ts, alias, name, status, report_path, summary, read, ops_total, files_changed, kind, proposal_paths, report_paths)
VALUES (CAST(strftime('%s','now') AS REAL), 'consolidator', '固化2', 'ok', '', '双路径', 0, 1, 1, 'batch', '["suggestions/2026-10-28_summary_p${P_ID}.md"]', '["index/reports/sentinel/2026-10-28.md"]');
SQL
```

### R3 种子 awaiting_review 提案（L0 计数 + feed 卡 + 深链 + E1 回归）

```bash
sqlite3 ~/md-vault/.sync/index.db <<'SQL'
INSERT INTO proposals (type, actor, run_id, status, payload, source_refs, justification, reject_reason, created_at, updated_at, merged_at, relpath)
VALUES ('summary', 'consolidator', 'r-test-1', 'awaiting_review',
        '{"form":"cache","body":"总结正文内容"}', '["welcome.md"]',
        '请采纳此总结', '', CAST(strftime('%s','now') AS REAL), CAST(strftime('%s','now') AS REAL), '', 'pending');
INSERT INTO proposal_transitions (proposal_id, from_status, to_status, actor, ts, reason)
VALUES (last_insert_rowid(), NULL, 'created', 'consolidator', CAST(strftime('%s','now') AS REAL), '');
INSERT INTO proposal_transitions (proposal_id, from_status, to_status, actor, ts, reason)
VALUES (last_insert_rowid(), 'created', 'processing', 'executor', CAST(strftime('%s','now') AS REAL), '');
INSERT INTO proposal_transitions (proposal_id, from_status, to_status, actor, ts, reason)
VALUES (last_insert_rowid(), 'processing', 'awaiting_review', 'executor', CAST(strftime('%s','now') AS REAL), '');
-- relpath 约定 = suggestions/<date>_<type>_p<id>.md（create() 同口径；UPDATE 不影响 last_insert_rowid 前的取值）
UPDATE proposals SET relpath = 'suggestions/2026-10-28_summary_p' || id WHERE relpath = 'pending';
SELECT 'P_ID = ' || id FROM proposals WHERE relpath LIKE 'suggestions/2026-10-28_summary_p%';
SQL
```

### R4 degraded 计数触发（C 组二选一；计数 = 进程内，**重启归零**）

- **路线 A（当前 config 下唯一可用，走 LLM 聊天路径）**：
  ```bash
  printf 'degrade-me\n' > ~/md-vault/notes/deg-test.md   # 若 notes/ 不存在先 mkdir -p
  chmod 000 ~/md-vault/notes/deg-test.md
  ```
  然后在 AI 聊天里让主 agent 改写 `notes/deg-test.md`（`edit_doc` update → pre-image 读失败 → degraded +1，**写入照常成功**，降级 = 无快照继续写）。
- **路线 B（需先在 config.yaml 放开 executor 并重启）**：materialize 一个 summary 槽 → `chmod 000` 该槽文件 → 控制台一键 adopt 同槽的第二个 summary 提案 → executor update capture 失败。
- **复位**：先 `chmod 644` 测试文件再删；计数归零 = 重启服务（DB 不受影响）。

## 2. 测试用例

优先级：★★★ = 核心（不过 = L5 不算完成）；★★ = 重要；★ = 回归/边界。

### A 报告区（队列面）

| # | 优先级 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|
| A1 空态 | ★★★ | 起始状态（`index/reports/` 不存在） | F5 → 开控制台 | 「报告」分区头可见（计数 0 + 右侧「只读 · 建议不执行」注记），区体 = 淡显一行「暂无报告」；与上方「无待审提案」空态块并列、互不干扰 |
| A2 列表渲染 | ★★★ | R1（4 个文件） | F5 → 开控制台 | 4 张卡片行，与待审卡同壳：alias chip（sentinel/narrator）+ 标题（取首个 `# ` 行）+ 日期相对标签（今天/昨天/MM-DD）；分区计数 = 4；无 `#` 的文件标题**回退显示 alias**；卡片 hover title = 完整 relpath；mtime 降序（10-28 在前） |
| A3 首载不闪 | ★★ | R1 落盘后**不开**面板 → 30s 后再开 | 开面板观察 | 首载只建基线：新卡**不**带脉冲（fresh 无）；DevTools 检查卡片无 `fresh` class |
| A4 新报告脉冲 | ★★★ | 面板开着（已建基线） | 新落一个文件 `narrator/2026-10-25.md` → 等 ≤30s 轮询 | 新卡出现且带脉冲（fresh），**常驻该卡**直到文件被删；另：覆写既有报告（改写内容 → mtime 增长）→ 该卡重新入脉冲 |
| A5 报告删除 | ★★ | A4 后 | `rm` 该报告文件 → 等 ≤30s | 卡片消失，脉冲集清理（其它卡脉冲状态不受影响） |
| A6 首载前不渲染 | ★ | 新页面（未轮询到） | 开 DevTools 暂停网络 → F5 → 开控制台 | 「报告」分区**整体不渲染**（未知态 ≠ 空态）；恢复网络后出现 |

### B 报告详情面（第三面）

| # | 优先级 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|
| B1 深链直达 | ★★★ | R1 + R2（D1 通知） | 顶栏 bell → 点「哨兵/报告已生成」 | 下拉溶解 → 控制台**直接开在报告详情面**（非队列面）；标题一次黄铜下划线 sweep（≈900ms）；滚动位置回顶 |
| B2 全文渲染 | ★★★ | B1 | 查看正文 | h1/h2/列表/表格/引用（brass 左边线）/代码块（hljs 高亮）/mermaid 图全部渲染，版式 = `.rpt-md` 排版；代码块有「复制」按钮 → 点击 → 按钮变「✓」1.5s 复原，剪贴板内容 = 代码原文 |
| B3 头部信息 | ★★ | B1 | 查看头部 | alias chip + 「只读」注记 + 标题 + 完整文件路径行（`index/reports/…`）；**无** adopt/reject 动作行（报告无状态机） |
| B4 文件缺失 | ★★ | 深链进入后 | `rm` 该报告文件 → 返回队列 → 重新点同一通知 | 正文 = 红色服务端错误文案（404「report not found」直显），不白屏不崩 |
| B5 空正文 | ★ | R1 空文件 | 队列面点 10-26 卡片进入 | 正文区淡显「（无正文）」 |
| B6 面切换/回退 | ★★★ | B1 | 点返回箭头 → 点 X 关闭 → 重新开面板 | 返回 → 队列面（报告卡仍在）；X → 面板收起（width 0）且 state 全清；重入 = 队列面（不卡在报告面） |
| B7 深链后 rail 重入 | ★★ | B1 后 | 不返回，直接点 rail【通知】 | 回队列面（`onMemoryRailClick` 双清生效） |

### C L0 快照降级 chip

| # | 优先级 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|
| C1 零位不占位 | ★★★ | degraded = 0（初始/重启后） | 看 L0 状态行 | 「N 待审 · N 待落地 · N 失败 · 轮询 30s」，**无** chip，行内无空位 |
| C2 计数出现 | ★★★ | R4 路线 A 执行后（degraded ≥ 1） | F5 → 开控制台 | L0 行失败数后出现红边 chip「1 快照降级」（红点 + `--st-error` 家族色） |
| C3 增长脉冲一次 | ★★★ | 面板开着且 prevDeg 已建立 | 再触发一次 degraded +1（路线 A 再来一次编辑）→ 等轮询 | chip 数字 +1，envPulse 脉冲**约 1.2s（2 次）后停止**，不常驻闪烁；计数不变轮询 → 无脉冲 |
| C4 首载基线不闪 | ★★ | 服务端 degraded 已 = 2 | F5（新页面）→ 开面板 | chip 显示「2 快照降级」但**无**脉冲（首次观察 = 基线） |
| C5 tooltip | ★ | C2 | hover chip | 「运行期内快照捕捉降级计数（进程内，重启归零）」 |
| C6 动效降级 | ★ | C3 环境 | 系统「减弱动态效果」开 → 触发增长 | 脉冲/深链 sweep 均禁用（prefers-reduced-motion），chip 静态显示 |

### D 通知深链与优先级（bell 下拉点击）

| # | 优先级 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|
| D1 report_paths only | ★★★ | R2 第一条 | 点「哨兵/报告已生成」 | 开报告详情面（= B1） |
| D2 proposal_paths only | ★★ | R2 D2 条 | 点「固化/提案待终审」 | 开**提案**详情面（五段材料），回归不破坏 |
| D3 双路径 | ★★★ | R2 D3 条 | 点「固化2/双路径」 | 开**提案**详情面；report_paths 被忽略（优先级 = proposal > report） |
| D4 旧字段 | ★ | R2 D4 条 | 点「叙事/旧字段深链」 | `openDoc('welcome.md')` 打开文档（编辑器），不触碰控制台 state |
| D5 普通通知 | ★ | 无路径通知 | 点一条无路径通知 | 下拉关闭，无深链动作 |

### E 回归/通用

| # | 优先级 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|
| E1 提案详情回归 | ★★★ | R3 | 队列面点提案卡 → 五段材料 → 填理由 reject | 详情面（`v-else-if="!report"` 链改动后）功能完好：正文/来源/相似记忆/动作行；reject 成功后 pollMemory 立即刷新，L0 计数变化，卡转终态淡显 |
| E2 rail 徽标 | ★ | R3 + 无 failed | hover rail 徽标 | 「待审 1 · 失败 0」；reject 后徽标回落（0 时不渲染） |
| E3 移动端 | ★★ | 桌面态面板开着（任意面） | 拖窗口宽 < 1024 | 控制台自动关闭，detail/report 双清；< 861 时详情「来源」跳转先收起 overlay 再开文档 |
| E4 主区无回流 | ★★ | 桌面 | 反复开关面板 | 300px 面板开合，主内容无文本重排/跳动（transform/width 方案未动） |
| E5 服务容错 | ★ | — | `kill 96242`（面板开着）→ 等 1–2 个轮询周期 | 面板保持旧数据，无 Vue 崩溃/白屏（DevTools 只见 network err）；重启服务后恢复轮询 |
| E6 轮询节奏 | ★ | — | DevTools Network 过滤 `memory/status` | 间隔 ≈30s；`actioned`（adopt/reject 后）立即额外拉一次 |

### F API 契约（功能层，curl 即可）

| # | 优先级 | 命令 | 预期 |
|---|---|---|---|
| F1 列表形状 | ★★★ | `curl -s localhost:9888/api/memory/reports` | `{"reports":[{relpath, alias, date, title, size, mtime}]}`，mtime 降序；目录不存在时 = `{"reports":[]}`（200 不 500） |
| F2 详情守卫 | ★★★ | `curl -s -o /dev/null -w '%{http_code}' localhost:9888/api/memory/reports/index/entities.md` 及 `..%2F` 越界 | 越通道 = **400**；不存在文件 = **404**；正常 relpath = 200 且 content 与文件全文一致 |
| F3 status 键 | ★★★ | `curl -s localhost:9888/api/memory/status | jq .snapshot` | `{"degraded": <int>}`；R4 触发后 > 0 |

## 3. 清理

```bash
rm -rf ~/md-vault/index/reports        # R1 全部（若想保留某份报告则只删其余）
sqlite3 ~/md-vault/.sync/index.db <<'SQL'
DELETE FROM notifications WHERE alias IN ('sentinel','narrator','consolidator')
  AND (report_paths != '' OR proposal_paths != '' OR summary IN ('报告已生成','旧字段深链','提案待终审','双路径'));
DELETE FROM proposals WHERE run_id = 'r-test-1';
DELETE FROM proposal_transitions WHERE proposal_id NOT IN (SELECT id FROM proposals);
SQL
chmod 644 ~/md-vault/notes/deg-test.md 2>/dev/null && rm -f ~/md-vault/notes/deg-test.md
# degraded 计数归零 = 重启服务（可选；重启会清零审计外的进程态）
```

## 4. 通过标准

- 所有 ★★★ 通过 = L5 完成（视觉 + 功能双重验证达成）；
- ★★ 允许个别通过 + 记录已知限制；★ 回归项失败必须记录并回查 `6e812b3` 是否越域改动既有件（style.css 应纯追加）。
- 视觉确认结论 + 截图（agent-browser `browser.capture` 或自截）附在验证记录里，再行 commit/销账。

## 5. 执行结果（2026-10-28，openchamber_web 通道 + curl）

执行 tab 保留在浏览器（桌面 1440×900）。截图存 `.openchamber/screenshots/l5-*.jpg`。

| 组 | 结果 |
|---|---|
| A 报告分区 | A1✓ A2✓ A3✓ A4✓ A5✓；**A6 待手动**（DevTools 网络暂停竞态） |
| B 详情面 | B1✓ B2✓ B3✓ B4✓ B5✓ B6✓ B7✓（B2 复制按钮的 1.5s「✓」反馈待真实浏览器复确认，见发现 2） |
| C 降级 chip | C1✓ C2✓ C3✓（计数 1→2 原地更新）C4✓；**C3/C4 的脉冲观感、C5 tooltip、C6 reduced-motion 待人工目测** |
| D 深链 | D1✓ D2✓ D3✓ D4✓ D5✓ |
| E 回归 | E1✓ E2✓ **E3✗（发现 1）** E4✓；**E5、E6 待手动**（需 kill/重启服务 + DevTools） |
| F 契约 | F1✓ F2✓ F3✓（400 invalid path / not a report path；404 report not found；status 键齐 + degraded>0） |

### 发现

1. **E3 失败 = 真 bug（唯一失败项）**：`app.js:466` `watch(() => window.innerWidth, cb)` —— `window.innerWidth` 非响应式，getter 无依赖，回调**永不触发**。实测 1440→390：控制台不关闭、detail 不清（以左侧 overlay 形态带着详情面存活，回桌面仍开着）。修复：换 `window.addEventListener('resize', ...)` 或响应式 ref 驱动 watch。
2. **B2 复制反馈（环境性）**：面板 iframe 上下文 `navigator.clipboard.writeText` 的 promise 挂起（无权限弹窗）→ `done()` 不跑 → 按钮文字停在「复制」。点击本身已注册（ok），EditorPane 预览为同款模式，真实浏览器正常即排除 L5 回归。
3. **种子纪律**：proposal `relpath` 必须 `.md` 结尾（`api.py:162` guard 强制），通知 `proposal_paths` 与 `proposals.relpath` 须逐字一致，否则深链 400/404。
4. **既有行为记录**：深链通知项点击后从本地下拉移除，poll 不恢复（下拉会逐条变少）；degraded 为进程内计数，仅重启归零（本轮结束值 = 2，重启前 L0 会显示「2 快照降级」chip，无害）。

### 人工目测清单（用户视觉确认时顺手看）

- A6：DevTools 暂停网络 → F5 → 开控制台 → 报告分区整体不渲染。
- C3/C4：chip 计数变化瞬间的 envPulse（约 1.2s、2 次后停）与首载不闪。
- C5：hover chip → tooltip「运行期内快照捕捉降级计数（进程内，重启归零）」。
- E6：Network 过滤 `memory/status`，间隔 ≈30s，reject 后立即额外拉一次。
- B2：真实浏览器点报告内代码块「复制」→「✓」。
- E5：`kill <server pid>`（面板开着）→ 1-2 轮询后面板保持旧数据不崩溃 → 重启恢复。

### 清理

已按 §3 执行完毕：`index/reports/` 删除、proposals/proposal_transitions/notifications 全部清空（API 复核 0/0/0）、deg-test 文件由触发 API 删除（无 chmod 000 残留）。degraded 计数（=2）随服务重启归零，未代用户重启。
