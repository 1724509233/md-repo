# Role: executor（唯一执行器）

**本角色不是 LLM agent。** 执行器是框架内置的确定性合并循环（`server/bgagent/executor.py`）：
每 30 分钟扫描一次 `suggestions/`，把 `status: adopted` 的提案落地为主记忆区文件变更，
并把超期的 `awaiting_review` 提案降级为 `expired`。无活则 no-op，连 LLM 调用都不发生。

此文件仅为满足角色注册表的 prompt 格式校验（fail-fast 要求 prompt 文件存在）。
注册表条目必须标记 `is_executor: true`，框架据此**不启动** LLM cron 任务。

设计出处：记忆运行时宪法（memory-runtime.md）§四「躯干：提案状态机 + 唯一执行器」。
