# apply_change 锁持有基准：`_db_lock` 临界区内的磁盘 I/O

**Date:** 2026-10-30
**Question:** 架构评审 backlog L3（`docs/superpowers/plans/2026-10-29-arch-review-leftovers.md`）：
`apply_change`（`server/vault/orchestration/file_ops.py:67`，`with _db_lock:` 包整个函数体）
在典型 vault 规模下持锁多久，其中多少是临界区内的磁盘 I/O？先量化再动——性能卡，非正确性卡。
**Decision:** 记录并关闭（用户裁决 A，2026-10-30）。真实规模（302 文件）≤ 4 ms；
10k 文件中位 14–17 ms、max ~24 ms（暖缓存）。**重开条件：vault > 5k 文件或观测到写者排队。**
**Repro:** `python 2026-10-30-apply-change-lock-hold-benchmark.py`（同目录，~5 min，含 10k 构建）。

---

## Platform

- macOS（darwin，Apple Silicon），APFS
- Python 3.12.13（repo `.venv`）
- 单开发机，无并发负载——写者串行化成本 = 锁持有时间本身，故直接测持锁时长

## 锁内有什么（`apply_change`，`file_ops.py:67`）

| 分支 | 锁内磁盘 I/O |
| --- | --- |
| created/modified | `index_segments`（读文件 + jieba 分词 + INSERT）、`graph_extract_file`（读文件 + 建边）；watchdog 路径（无 `new_hash`）另加 `read_bytes` + sha256 |
| deleted | `remove_dead_links`：1–2 次 rglob 全库扫描（短名唯一性）+ `_find_referrers` 2 次 rglob + referrer 读/重写 |
| moved | 上述 + `update_references_batch`：唯一性 rglob + `_find_referrers` 2 次 rglob + referrer 读/重写（short-name 重写仅 stem 变化时触发） |
| 公共 | `graph_extract_file` 内 `_resolve_wikilink`：每个文件名首次解析 = 1 次 rglob 全库扫描（模块级缓存） |

## Methodology

- **合成 vault**：n 文件 × 9 子目录，每文件 ~1–2 KB、4 个 short-name wikilink + 2 个 tag；
  n ∈ {300（≈ 真实规模 302）, 1000, 3000, 10000}
- **稳态**：`rebuild_segments_all` + `graph_rebuild_all`，wikilink 文件名缓存预热（同运行中的 server）
- **埋点**：`RecordingLock` 包装 `schema._db_lock` 记录 acquire→release 时长，
  patch `file_ops` 模块在 import 时绑定的名字（`file_ops.py:19`）
- **场景**：4 action × 20 次 = created（带/不带 `new_hash`，分别对应 reconcile / watchdog 路径）、
  deleted、moved（dst 换新 stem → 覆盖完整 referrer 重写路径）
- **组件分解**（3k/10k）：rglob、read+sha256、index_segments、graph_extract（暖/冷缓存）、update_references_batch

## Results（锁持有时间，ms；4 action × 20 次）

| 文件数 | action | med | p95 | max |
| ---: | --- | ---: | ---: | ---: |
| 300（真实规模） | created（带 hash） | 0.42 | 0.55 | 0.90 |
| | created（无 hash，watchdog） | 0.43 | 0.49 | 0.96 |
| | deleted | 1.21 | 1.36 | 1.89 |
| | moved（含 referrer 重写） | 3.12 | 3.51 | 5.88 |
| 1,000 | created（带 hash） | 0.38 | 0.45 | 1.78 |
| | created（无 hash） | 0.41 | 0.49 | 0.90 |
| | deleted | 2.12 | 2.36 | 3.17 |
| | moved | 4.12 | 4.93 | 12.19 |
| 3,000 | created（带 hash） | 0.44 | 0.49 | 5.56 |
| | created（无 hash） | 0.46 | 0.55 | 1.07 |
| | deleted | 4.76 | 5.12 | 7.14 |
| | moved | 7.03 | 8.25 | 12.53 |
| 10,000 | created（带 hash） | 0.44 | 0.50 | 4.04 |
| | created（无 hash） | 0.44 | 0.52 | 1.13 |
| | deleted | 13.97 | 14.63 | 17.84 |
| | moved | 16.16 | 17.32 | 23.57 |

## 组件分解（ms，10 次中位）

| 组件 | 10k | 3k | 出现在 |
| --- | ---: | ---: | --- |
| rglob 单个文件名 | 6.96 | 2.33 | 每 move 3 次 / 每 delete 1–2 次 / wikilink 首次解析每文件名 1 次 |
| read + sha256（1 文件） | 0.00 | 0.00 | watchdog created 路径 |
| index_segments（读文件+分词+INSERT） | 1.46 | 1.54 | created/moved |
| graph_extract（暖缓存） | 0.55 | 0.83 | created/moved |
| graph_extract（冷缓存，每文件名首次） | 28.43 | 10.12 | 每次 server 进程每文件名一次性 |
| update_references_batch（1 move，~4 referrer） | 14.03 | 4.74 | moved（stem 变化时） |

## 规律

1. **created 跨规模恒定 ~0.4 ms**——单文件 I/O，无全库扫描；这是最高频 action（watchdog 每事件一次）。
   watchdog 路径 vs reconcile 路径差 < 0.1 ms（5 KB 文件 read+sha256 ≈ 0 ms）。
2. **deleted/moved 随文件数线性涨**，驱动项 = 每次操作 2–4 个 rglob 全库扫描 + referrer 读写
   （rglob(1 文件名)：2.33 ms@3k → 6.96 ms@10k）。
3. **冷缓存一次性成本**：server 启动后每个文件名首次被 `graph_extract_file` 解析 = 4 个 rglob
   ≈ 10 ms@3k / 28 ms@10k，之后走缓存。
4. **页缓存冷启动尖峰**：长构建后首跑观测到 max 261–440 ms（deleted/moved 各一），暖缓存后未再复现；
   属一次性，不改变中位数结论。

## 结论与裁决

- 真实规模（302 文件 / 2.6 MB / 最大 270 KB）：锁持有中位 0.4–3.1 ms，max < 6 ms。
  修复收益 ≈ 0，改动风险（五域复合操作拆分 + TOCTOU 权衡）≫ 收益 → **不动代码**。
- 10k 规模：中位 14–17 ms、max ~24 ms（暖），对低频写者路径可接受；真正的暴露面是
  >5k 文件 + 写者并发的组合。
- **卡片关闭（裁决 A）。重开条件：vault > 5k 文件，或观测到写者排队**（watchdog 消费者与
  agent/web 写者的锁等待）。重开策略：先做 **B1**（锁内 rglob → `files` 表查询，3 处：
  `update_references_batch` 唯一性检查、`_find_referrers` 短名、`remove_dead_links` 短名，
  预计 10k 规模省 ~15–20 ms 锁内时间，语义不变），再评估全文形态（读-先-锁 / refs 写移出锁外）。
