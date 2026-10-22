# 基础关系：Schema v0.3 字段契约

输入主体为 RelationInput：core_issue、issue_set、当前允许检查范围内的 info_units；另含 scope 和 existing_relations。
scope=all 表示全案初次生成；scope=selected 表示只检查所给节点之间的关系。
读取五类 InfoUnit、speaker、side、related_issue、actor、target 和 source_refs；只建立直接语义关系。

输出为 RelationOutput：relations。
Relation 字段仅有 id、source_id、target_id、relation_type、evidence_unit_ids、
rationale、certainty（explicit / inferred）。所有信息单元 ID 必须存在。

关系类型及方向：
- about：一个有效 InfoUnit 明确讨论另一个有效 InfoUnit，而不只是共享关键词
- support：依据节点直接支持一个可判断的 event、state、rule 或 position
- contradict：两个节点对同一可比较事实、条件或立场表达不能同时成立的内容

只允许 `about | support | contradict`。`actor_of/target_of` 由 InfoUnit.actor/target 确定性派生，不输出；不输出 `occur_at`。

evidence_unit_ids 引用判断依据的信息单元，不复制原文。
不能只因主题相同就建立支持关系；不同来源的重复陈述不构成独立证明。
无合法关系时返回空数组，不创建替代关系或法律结论。
不得输出 existing_relations 中已存在的等价关系。contradict 的正反方向视为同一关系；about 和 support 保持方向性。
scope=selected 时，source_id、target_id 和 evidence_unit_ids 均不得引用输入 info_units 之外的节点。
输出 id 只是本次响应内唯一的临时 ID；持久化 ID 由后端生成。
