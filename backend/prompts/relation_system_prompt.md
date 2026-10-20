# 基础关系：Schema v0.2 字段契约

输入为 RelationInput：core_issue、issue_set、完整 info_units。
读取 speaker、side、stance_target、actor、target 和 source_refs；只建立直接关系。

输出为 RelationOutput：relations。
Relation 字段仅有 id、source_id、target_id、relation_type、evidence_unit_ids、
rationale、certainty（explicit / inferred）。所有信息单元 ID 必须存在。

关系类型及方向：
- actor_of：event → entity（行为主体）
- target_of：event → entity（对象或受影响方）
- about：statement → 相关信息
- support：信息 → 被支持的 statement
- contradict：相互冲突的 statement
- occur_at：event → state/temporal

evidence_unit_ids 引用判断依据的信息单元，不复制原文。
不能只因主题相同就建立支持关系；不同来源的重复陈述不构成独立证明。
无合法关系时返回空数组，不创建替代关系或法律结论。
本文件当前不由运行路径加载，执行接口接通前不调用模型。
