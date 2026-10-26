---
status: accepted
---

# 引用维护以图谱 edges 表为数据源（替代全库扫描）

reference maintenance（移动/删除后的引用改写）用既有图谱 edges 表做引用方定向查询（`_find_referrers`），而非每次全库 `rglob` 扫描；conn 为 None 或表为空（图谱未构建）时回退全库扫描——唯一文档化的 db→fs 依赖。触发因是性能：10-07 之前，`_update_references` / `_remove_dead_links` 每次 move/delete 都读遍所有文件，只为找少数引用了目标的那几个。此前图谱是 change_log 的尾随订阅者（10-01 定位中与 FTS5 抽取共享同一个 subscriber，属读侧增强），本决策使依赖方向反转：图谱成为写路径的上游依赖，其数据正确性开始有实际后果——edges 漏条目 → 引用漏改；漏判 move 掉进 delete 路径时，`_remove_dead_links` 会删掉本该存活的引用。

图谱至今仍是可重建的派生缓存（`recovery.py` 恢复演练的前提），不是权威真相源；正确性靠三层缓解：`apply_change` 的 extract-先行顺序（改写引用前对目标文件跑 `graph_extract_file`，保证 edges 新鲜）、空表回退全扫、索引重建演练。曾考虑的替代方案是维持全库扫描，被拒绝：数千文件规模下每次 move/delete 读遍全库不可扩展，且回退路径已保留首跑与降级状态的等价正确性。
