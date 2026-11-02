# 文档归档迁移：docs/ → vault 镜像（检索改走 md-vault MCP）

> Goal: 把项目文档大部分迁至 `~/md-vault/projects/md-repo/` 归档，减少仓库内文档的上下文消耗；检索/召回改走 md-vault MCP（search_docs / read_doc / get_related_docs）。仓库只留活跃代码文档。

## 用户裁决（2026-11-02）

1. **范围**：全量（docs/ 整树 + issues/ + learning-records/ + .superpowers/brainstorm），**但 2026-11-02 五份 bugfix plan 留仓库**。
2. **frontmatter**：归档 md 批量补 `title/tags/date`。
3. **旧副本**：vault 内既有副本按**正文哈希**去重（一致→删；同名异版→legacy/；无对应物→用户笔记，不动）。
4. **./vault**：仓库内 client 段目录（已 gitignore）的 34 个历史跟踪文件 `git rm --cached` 顺带清理，本地文件保留。
5. **HTML 不进 vault**（vault 只处理 md）：`docs/` 下 2 份提案通道 HTML 设计稿**留仓库**，RESOURCES.md 的 HTML 链接不改。

## 现状关键事实（2026-11-02 侦察）

- 迁移池：docs/ 78 md + 2 HTML（HTML 除外）+ issues/ 2 + learning-records 1 + .superpowers/brainstorm 1 ≈ 23,900 行 / ~1.45MB。
  - plans 23（11-02 五份除外 → 迁 18）、specs 34、adr 4、benchmarks 2md+1py、review 1、docs 顶层杂项 3。
- vault = `~/md-vault`（仓库外）。`projects/md-repo/` 既有子目录：`spec/`（34 份旧 spec 副本，**部分文件名带 -design 等改名**、5 份已有 frontmatter）、`docs/`（bgagent-config/fts5 两仓库副本 + mcp-scope/memory-recall 两用户笔记 + vault/README）、`benchmarks/`（dir-scan 1 份，疑似仓库副本）、`ui/`（ai-panel 回顾 1 份，仓库副本）、`prompt/`（7 份 prompts 副本——**源留仓库，不动**）。
- 代码引用文档路径全部是注释/docstring，零运行时依赖。需改的引用点：AGENTS.md:50（Design Spec 节）、RESOURCES.md（3 条 md spec 链接）、4 个 py docstring（snapshot_watcher.py:7 / agent/policy.py:4 / vault/orchestration/file_ops.py:65 / vault/db/fts_query.py:5）、server/vault/db/README.md:47、留仓 5 份 11-02 plan 内的 ~8 处 `docs/` 引用。两个 tests docstring 引 11-02 plan（留仓）→ 不改。
- 10-27 spec 对 HTML 只是文字提及，无相对链接 → 移动后无断链。
- 基线：pytest 779 passed + 4 failed（既有漂移，失败集合不得变）。

## 目标结构

```
~/md-vault/projects/md-repo/
├── plans/            # 18（23 - 11-02 五份）
├── specs/            # 34
├── adr/              # 4
├── benchmarks/       # 仓库 2md+1py（与既有 1 份合并，旧副本按 Task 3 去重）
├── review/           # 1
├── issues/           # 2（新目录）
├── learning-records/ # 1（新目录）
├── brainstorm/       # 1（.superpowers/brainstorm 迁入）
└── bgagent-config.md / fts5-recall-experiments.md / ai-panel-ui-fix-retrospective.md（docs 顶层杂项）
```

仓库 `docs/` 迁移后只剩：`superpowers/plans/2026-11-02-*.md`（5）+ 2 份 HTML。

内部相对链接（plan↔spec↔benchmark，plan 开头 `**Spec:** docs/superpowers/specs/...` 一类）：整树按相对结构搬 → 存活，不重写。

## frontmatter 规范（Task 2）

```yaml
---
title: <首个 H1 文本（去 #）>
tags: [md-repo, <kind>]        # kind ∈ plan|spec|adr|benchmark|issue|note
date: YYYY-MM-DD               # 仅文件名含日期时；ADR 已有 FM（status:）→ 合块补键，保留 status
---
```

幂等：已有 FM 的文件跳过。脚本放临时目录（T/opencode），不入仓库。留仓 5 份 11-02 plan 不加（与仓库 plan 惯例一致）。

## Tasks

- [ ] **Task 0 快照**：`git ls-files` 生成迁移清单 + 每文件 sha256 manifest（临时目录）；记录 vault 旧目录文件清单+正文哈希；确认 `git ls-files vault/ | wc -l` = 34。
- [ ] **Task 1 镜像复制**：mkdir 目标目录；按映射复制（排除 11-02 五份 plan 与 2 份 HTML）；逐文件 sha256 比对 manifest，全等才继续。
- [ ] **Task 2 frontmatter 批处理**：按规范补写；报告 N 改 / M 跳过（ADR 合块单独列）；抽 3 份人工核对。
- [ ] **Task 3 哈希去重**（对 vault 旧 `spec/` `docs/` `benchmarks/` `ui/`；正文哈希 = 剥离 FM 块 + 去行尾空白后 sha256）：
  1. 正文哈希 ∈ 新权威文件集合 → 删旧副本；
  2. 哈希不等但文件名 stem ∈ 权威文件 stem 集合（改名/异版，如 `2026-10-25-reversible-automation-design.md` vs 权威 `2026-10-25-reversible-automation.md`）→ 移 `projects/md-repo/legacy/`；
  3. 无 stem 对应（mcp-scope.md / memory-recall.md / 2026-10-09-frontend-animation-debugging.md 等用户笔记）→ 不动。
  产出报告：删 X / legacy Y / 留 Z（逐条列名）。空目录 rmdir。
- [ ] **Task 4 仓库侧**：
  - `git rm` 迁移清单（逐条核对不含 11-02 plan、HTML、server README/prompts）；
  - AGENTS.md：`## Design Spec` 节改为 vault 指针 + 「架构变更前用 md-vault MCP `search_docs("md-sync design")` 检索、`read_doc("projects/md-repo/specs/2026-09-27-md-sync-design.md")` 读全文」；新增 `## 文档归档` 节（归档位置、MCP 检索方式、仓库保留清单）；
  - RESOURCES.md：3 条 md spec 链接 → vault 路径（2 条 HTML 链接不动）；
  - 4 个 py docstring + db/README.md:47 → vault 路径；
  - 留仓 5 份 11-02 plan 内 `docs/...` 引用 → vault 路径（agent-tools-config:10-22 spec；folder-delete-dotfiles:10-16+09-27；proposals-validation-audit:10-27 v2.1+10-22；search-jump-highlight:10-16；tag-hub-relatedness:ADR 0003+10-01 spec）。
- [ ] **Task 5 ./vault 解跟踪**：`git rm -r --cached vault/`（34 文件，本地保留）。
- [ ] **Task 6 提交**（fixer 不提交，manager 验证后提交）：
  - C1 `docs: archive docs to vault mirror (projects/md-repo) + frontmatter + reference updates`（Task 1-4 的仓库侧）
  - C2 `chore: untrack client ./vault leftovers`
  - C3 `docs: archive docs-vault-archive plan (self)`（本 plan 入 vault plans/ + 补 FM + git rm）
- [ ] **Task 7 验证**：
  - pytest 全量：779 passed + 4 failed，失败集合不变；
  - 索引：确认/临时起 server（真实 config，勿与用户已跑实例冲突——先 lsof 查端口），等 ensure_indexed 收进新文件；
  - MCP 冒烟（md-vault 工具）：`search_docs` 抽 3 个已知词（ledger 落行 / snapshot watcher / bgagent 报告）命中对应归档文档；`read_doc("projects/md-repo/specs/2026-09-27-md-sync-design.md")` 全文可读（与 AGENTS.md 新指针一致）；`get_related_docs` 抽 1 份带 tag 文档 → 图生效（返回其他 md-repo 文档）；
  - 报告：迁移 N 文件 / FM M / 去重删 X / legacy Y / 留 Z + 三个 commit hash + MCP 证据。

## 边界 / 风险

- vault 在 git 外：归档文档失去 git diff 能力（历史留 git log）；变更追踪靠 vault ledger（外部写入归 source=watchdog，预期内）。
- 不碰 `.sync/`、任何 db、Obsidian 配置；vault `prompt/`（prompts 副本，源留仓库）不动；`.DS_Store` 不管。
- 正文哈希比对容忍 frontmatter 差异与行尾空白；改名副本靠 stem 匹配兜底。
- 若去重发现旧副本正文与任何权威文件都不符且 stem 不匹配 → 不删不移，列入报告由用户裁决。
