# Case A v0.3 Gold

这是独立于 Gold v1.7 的 Schema v0.3 标签投影，用于验证新分类、ID 引用和三种推理视图。它不覆盖、改写或重新评分历史 v7.2 基线。

- `gold_info_units.jsonl`：按 v0.3 语义逐项人工映射，保留原始来源字节与 ID 以便对照。
- `gold_relations.jsonl`：仅保留 `about/support/contradict`；旧 `actor_of/target_of` 已物化到 `InfoUnit.actor/target`。
- `label_mapping.json`：记录从 Gold v1.7 到本目录的分类决定。
- `gold_view_cases.jsonl`：分别覆盖 conflict、timeline、hypothesis，`claim` 仅是布局角色。

新版评测必须分别报告原始第一轮候选、正式归一化输出和 Consolidation 输出，不得把本目录结果称为 v7.2 同口径成绩。
