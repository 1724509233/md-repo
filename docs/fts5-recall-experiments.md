# FTS5 召回条件实测笔记

> 2026-10-15 · 为分层召回方案（`docs/superpowers/specs/2026-10-15-tiered-recall-design.md`）做的实证调研。
> 所有标注「实测」的结论均在真实环境验证过；标注「文档」的来自 SQLite 官方 FTS5 文档。

## 1. 测试环境

| 项 | 值 |
|----|-----|
| SQLite | 3.53.4（Python `sqlite3` 内置） |
| 真实索引 | `~/md-vault/.sync/index.db`，`segments_fts`（external content，列：body/meta/code），4092 段 |
| 受控实验 | 内存库 `CREATE VIRTUAL TABLE t USING fts5(x)`，手工插入已知内容 |
| 分词 | jieba `cut_for_search`（与线上 `_tokenize_for_index` 一致） |
| 官方文档 | https://www.sqlite.org/fts5.html（2026-10 抓取） |

## 2. FTS5 召回条件工具箱（实测确认）

| 条件 | 语法 | 实测行为 |
|------|------|----------|
| 精确词 | `召回` / `"召回"` | 连续 CJK = **1 个 token**（= jieba 词），精确匹配 |
| 短语 | `"a b"` 或 `a + b` | 多 token **相邻**匹配；`+` 是短语拼接符（文档 BNF：`<phrase> := <phrase> + <phrase>`） |
| 前缀 | `召回*` / `recall*` | CJK = jieba 词前缀（真实库 `召*` → 17 命中）；英文 = 词前缀（`recall*` 命中 `recalls`） |
| NEAR | `NEAR(召回 关联, 10)` | 词距 ≤ N token，默认 N=10；**短语间空格分隔**（见 §3 坑 2） |
| AND / OR | 显式，或空白隐式 AND | ✓ |
| NOT | **二元** `a NOT b` | ✓ 语法与语义均验证（一元 `NOT a` 报错，见 §3 坑 1） |
| 列过滤 | `body:mcp`、`{body meta}:mcp`、`-code:mcp` | ✓ 真实库 `mcp` 全列 110 命中；`-code:mcp` / `body:mcp` / `{body meta}:mcp` 均 99（11 个 code-only 命中被排除） |
| 首 token | `^term` | ✓ 实测：仅匹配列首 token（受控库 2/2 行命中）；**只能出现在短语整体之前**，`a + ^b`、`NEAR(^a b)` 均语法错误（实测） |
| trigram | 需单独建表 `tokenize="trigram"` | 子串匹配 + LIKE/GLOB（文档确认，未建表实测）；**<3 字符的子串不匹配任何行**；`case_sensitive 1` 时只支持 GLOB 不支持 LIKE |
| fts5vocab | `CREATE VIRTUAL TABLE v USING fts5vocab('segments_fts', 'row')` | ✓ 实测（index.db 副本）：直接列出索引全部 token。模式名是 `row` / `col` / `instance`（**不是** `column`）。`row` 列 = (term, doc)；`col` 列 = (term, col, doc, cnt)。调试"索引里到底存了哪些 token"的首选工具 |

补充：CJK 词天然是合法 bareword（bareword 允许全部 >127 码点连写），`召回` 与 `"召回"` 等价。

## 3. 语法坑（全部实测复现）

| # | 错误写法 | 报错 | 正确写法 |
|---|----------|------|----------|
| 1 | `NOT recall`（一元） | `syntax error near "NOT"` | 二元：`recall NOT fts5` |
| 2 | `NEAR(recall, fts5, 5)`（逗号分隔短语） | `syntax error near ","` | 空格分隔：`NEAR(recall fts5, 5)`；逗号只用于最后的 N 参数 |
| 3 | `+recall`（"必选词"） | `syntax error near "+"` | FTS5 **没有**必选词语法；`+` 是短语拼接（`a + b` = 相邻短语） |
| 4 | `"one two thr*"`（引号内前缀） | 不报错但 `*` 被传给 tokenizer，前缀失效 | `*` 必须在引号外：`one two thr*`（`"abc def*"` 中 `*` 也只前缀化最后一个 token `def`） |
| 5 | `(a OR c) b`（括号表达式后跟裸短语） | `syntax error near "b"`（实测） | 隐式 AND **不会**插到括号表达式之后，必须显式：`(a OR c) AND b`（实测 1 命中） |
| 6 | `NEAR(^a b)` / `a + ^b`（`^` 位置） | `syntax error near "^"`（实测） | `^` 只能出现在短语整体之前：`^a`、`^ "one two"` |

> 坑 3 纠正了一个常见误解：FTS3/4 或其他引擎的 `+term` 必选词概念在 FTS5 中不存在。
> 坑 5/6 是动态拼接 FTS 表达式时最容易踩的运行时错误（只在 execute 时暴露），见 §10 校验策略。

## 4. 双重分词机制（核心发现）

```
jieba cut_for_search → 空格拼接文本 → FTS5 unicode61 再切分
```

unicode61 规则（文档 + 实测）：Unicode L*/N* 类字符（含 CJK 表意文字）为 token 字符，**连续段 = 1 个 token**。
因此 **FTS5 token ≈ jieba 词**，英文/数字词原样保留。

受控实验（内容：`'召回 召回率 召唤 使用 fts5 检索 recall recalls'`）：

| 查询 | 结果 | 说明 |
|------|------|------|
| `"召"`（单字精确） | 0 | 索引中不存在单字 token |
| `召*`（单字前缀） | 1 行命中 | 所有"召"开头的词（召回/召回率/召唤） |
| `"召回"` / `召回*` | 1 | jieba 词精确 / 词前缀 |
| `"召 回"`（按字切分的短语假设） | 0 | **证伪**"unicode61 把中文按字切分"的假设 |
| `recall*` | 1 | 英文词前缀（命中 recalls） |

真实库推论：

- **单字中文查询在 FTS5 层就无解**（双重原因：jieba `len(t) > 1` 过滤 + CJK 连续成词）；
  出路是**单字 → `char*` 前缀化**（真实库 `召*` → 17 命中，与 fts5vocab 查到的 `召回` doc=17 一致）
- `"分层召回"` 与 `"分层" AND "召回"` 是不同 token、互不命中；但索引与查询同走 jieba，实际自洽
- 前缀查询对 CJK 是**词级**前缀（jieba 词前缀），不是字符级：`召*` 命中 `召回/召回率/召唤`，
  但**不能**命中"召"开头的其他字（如"招"）——token 边界由 jieba 决定，unicode61 只把 jieba 输出的每个空格段当原子

## 5. cut_for_search 子词扩展（实测）

| 查询 | cut_for_search 输出 |
|------|---------------------|
| `搜索引擎` | `[搜索, 索引, 引擎, 搜索引擎]`（整词 + 全部子词） |
| `中华人民共和国` | `[中华, 华人, 人民, 共和, 共和国, 中华人民共和国]` |
| `知识图谱` | `[知识, 图谱]`（不在词典，无整词 token） |
| `分层召回` | `[分层, 召回]`（不在词典，无整词 token） |

**过约束问题**：现状代码把全部 token AND 起来（`segments_sql.py:79-80`）。
词典词查询（如 `搜索引擎`）展开 4 token 全 AND → 只含"搜索…引擎"（分开出现）的文档漏召回。
真实库对照：`"搜索引擎"` 2 命中 vs `"搜索" AND "引擎"` 2 命中（本库恰好一致，但机制上过约束成立）。

## 6. NEAR 实测（真实库）

| 查询 | 命中 |
|------|------|
| `NEAR(召回 关联, 10)` | 1 |
| `NEAR(召回 关联, 2)` | 1 |
| `"召回" AND "关联"`（现状 strict） | 3 |

NEAR 是单条查询、词距约束，介于 AND（同段共现）与 OR 之间；本库样本小，收益场景待评估。

**N 的语义是 gap 不是窗口**（文档示例 + 实测）：第一个短语结尾与最后一个短语开头**之间**的 token 数 ≤ N。
文档示例 `'A B C D x x x E F x'`：`NEAR(e d, 3)` 命中、`NEAR(e d, 2)` 不命中（D 与 E 之间恰 3 个 token）。
调参直觉：N=10 意味着两短语间可隔 10 个 jieba 词，对中文已相当宽松；建议从 N=5 起步。
NEAR 组内可混用前缀 token（文档原例 `NEAR("one two" thr* + four)`）；`^` 不可用于 NEAR 内部（§3 坑 6）。

### 6.1 布尔组合与优先级（实测）

优先级从紧到松：**隐式 AND > NOT > AND > OR**（文档明确"隐式 AND 比 NOT 绑得更紧"）。

受控实验（行A=`a b`，行B=`a c`）：

| 查询 | 命中 | 解析 |
|------|------|------|
| `a NOT b c` | 2 | = `a NOT (b AND c)`（NOT 先结合右侧的隐式 AND） |
| `(a NOT b) AND c` | 1 | 显式括号改变结合 |

**实现含义**：想表达 `(one NOT two) AND three` 必须显式加括号，否则语义漂移。
列过滤可作用于整个括号表达式（实测）：`-code : (mcp OR fts5)` → 131 命中 vs 全列 146，
比逐 token 排除简洁；嵌套列过滤只能进一步收窄、不能重新放开被排除的列。

## 7. 分词 → 组合条件：四个正交轴

`cut_for_search` 输出是**带冗余的 token 集**（整词 ⊃ 子词）。组合方式拆成四个轴：

| 轴 | 选项 | 说明 |
|----|------|------|
| **1. 选哪些 token** | T1 全部 AND（现状）<br>T2 仅最长词<br>T3 仅子词（丢冗余整词）<br>T4 单字 → `char*` 前缀化 | T1 过约束（§5）；T2 最严（整词相邻）；T3 最松的 AND |
| **2. 组合算子** | C1 AND<br>C2 OR<br>C3 `NEAR(a b, N)`<br>C4 `a NOT b`<br>C5 文档级 AND（逐 token 查询 → path 层交集） | C5 是现状做不到的跨 segment 匹配，最大漏召回源的解法 |
| **3. 匹配范围** | S1 单 segment（现状，FTS5 行级）<br>S2 文档级（path 交集）<br>S3 列过滤/排除（`-code:` 等） | S3 实测可砍掉 ~10% code-only 噪声命中 |
| **4. token 形态** | F1 精确 `"token"`<br>F2 前缀 `token*` | F2 解决英文词干 + CJK 单字（§4） |

## 8. 映射到分层召回

| 层 | 组合 | 解决什么 |
|----|------|----------|
| strict（现状） | T1 × C1 × S1 × F1 | — |
| relaxed | T3 × C1+C5 × S2 × F1 | 关键词分散在同文档不同 segment（最大漏召回源） |
| fuzzy | T4/T3 × C2 × S2 × F2 | 词干、单字中文 |
| （可选中间档） | T1 × C3(NEAR N=5 起步) × S1 | 单段内词距约束，实现成本最低（单条查询）；N 语义见 §6 |

**评分**：`bm25(segments_fts, 1.0, 0.4, 0.1)`（body/meta/code 列权重）各档通用；
文档级档 score = Σ 每 token 最佳 bm25（覆盖度 + 相关度）。

**性能**：
- 文档级 AND = N 次 FTS 查询（N = token 数，通常 2-5），个人库规模（数千段）可忽略
- 前缀查询默认 range scan；要优化可建表时加 `prefix='2 3'` 前缀索引（CJK 单字前缀收益有限，库小无感）

## 9. 待拍板问题（含评审倾向）

1. **T1 过约束**是否在 strict 层就修？
   → 评审倾向：**strict 改 T2（仅最长词）**。索引侧已含整词 token，查询用整词短语最贴近用户意图；
   整词不在词典时（如 `分层召回`）自然退化为子词 AND，不会更差。成本仅改 token 选取逻辑。
2. **NEAR 中间档**做不做？
   → 评审倾向：**做**。单条 SQL、无跨表交集，是 strict（同段相邻）与 relaxed（同文档共现）之间成本最低的过渡档；
   N 从 5 起步做 A/B（N=10 对中文偏松，§6）。
3. **列排除**（`-code:`）默认行为还是参数？
   → 评审倾向：**relaxed/fuzzy 档默认带 `-code :`，strict 保留全列**。~10% code-only 噪声削减是净收益；
   "在代码里搜"是显式低频场景，以参数开放。

## 10. 补充细节（文档核对 + 实测）

- **bm25 rank 默认配置**：`INSERT INTO segments_fts(rank) VALUES('bm25(segments_fts, 1.0, 0.4, 0.1)')`
  可设默认 rank，省去每查询手写权重（内存库实测机制可用；是否改真实库待拍板）。
- **最大 token 长度 32768 字节**：超出静默截断且可能切坏 UTF-8；md-vault 场景实际影响可忽略，值得知晓。
- **C5（文档级 AND）score 聚合**：Σ 每 token 最佳 bm25 之外，建议加 path 内命中 token 数的**覆盖度因子**，
  避免长 token 列表下"每 token 分高但覆盖稀疏"的 path 排前。
- **FTS 表达式校验策略**：隐式 AND 括号边界（§3 坑 5）、`^` 位置（坑 6）等错误只在运行时暴露。
  动态拼接时建议：白名单组合器（只生成已知合法形态）或 execute 前跑一次 `SELECT count(*) ... MATCH ?` 探针。
- **trigram 表若做 CJK 子串兜底**：需一并拍板 `case_sensitive` / `remove_diacritics` 选项
  （影响 LIKE/GLOB 加速可用性，§2）。

## 附录：复现脚本要点

```python
# 受控实验
conn = sqlite3.connect(":memory:")
conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
conn.execute("INSERT INTO t VALUES ('召回 召回率 召唤 使用 fts5 检索 recall recalls')")
conn.execute("SELECT count(*) FROM t WHERE t MATCH ?", (query,))

# 真实库
conn = sqlite3.connect(os.path.expanduser("~/md-vault/.sync/index.db"))
conn.execute("SELECT count(*) FROM segments_fts WHERE segments_fts MATCH ?", (query,))
```

关键对照查询：`"召"` vs `召*`、`NEAR(召回 关联, 10)`、`-code:mcp` vs `mcp`、
`"搜索引擎"` vs `"搜索" AND "引擎"`、`recall NOT fts5`、
`(a OR c) b`（坑 5）、`a NOT b c` vs `(a NOT b) AND c`（优先级）、
`-code : (mcp OR fts5)`（列过滤 × 括号表达式）。

fts5vocab 调试（在 index.db **副本**上建，勿动原库）：

```sql
CREATE VIRTUAL TABLE v USING fts5vocab('segments_fts', 'row');   -- (term, doc)
CREATE VIRTUAL TABLE vc USING fts5vocab('segments_fts', 'col');  -- (term, col, doc, cnt)
SELECT term, doc FROM v WHERE term LIKE '召%' ORDER BY doc DESC;
```
