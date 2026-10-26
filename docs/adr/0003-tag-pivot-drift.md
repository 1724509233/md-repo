---
status: accepted
---

# tag 关联枢纽：spec 意图与实现漂移，需重新实现

**决策**：图谱的 tag 实体与 tag 边当前只写不读——抽取建了 doc→tag 边，但无任何查询消费：related BFS 每层 frontier 只收 doc（`dst.type='doc'` / `src.type='doc'`），tag 永不进入 frontier，`via tag` 结果在任何跳数下产不出来（backlinks / referrers 同样过滤 `type='doc'`）。10-01 spec 明确设计 tag 为关联枢纽（共享标签带出相关文档，正是「你没搜但相关的也给你」的机制，API 示例响应含 `type:"tag"`）。该差异定性为**实现漂移**而非有意简化：当时 agent 实现未受监管，实体类型中仅 doc 路径经人工验证；tag 需重新实现（BFS 放行 tag 入 frontier + doc→tag→doc 透传 + 测试），走独立工作通道。

**后果与治理**：tag 枢纽重建前，图谱关联能力仅覆盖 doc↔doc 边（wikilink/related），共享标签关联缺失。治理注记：agent 实现的路径默认视为未验证——收编进词汇表/ADR 时须核对 spec 意图，不能只核代码自洽（本案为首例）。附带漂移：spec 示例的 `score` 字段未实现（现仅返回 path+type）。
