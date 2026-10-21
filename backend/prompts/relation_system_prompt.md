# 基础关系：Schema v0.3 字段契约

输入为 RelationInput：core_issue、issue_set、完整 info_units。
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
本文件当前不由运行路径加载，执行接口接通前不调用模型。
