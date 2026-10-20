# 信息抽取：Schema v0.2 字段契约

输入为 ExtractionInput：case_title，以及 files[{file_id, filename, text}]。
text 来自已缓存的完整 UTF-8 TXT。只提取与争点有关的最小关键语义集合，
同一方对同一命题的重复叙述合并节点、保留全部必要引用；不按句数生成节点。

输出为 ExtractionOutput：core_issue、issue_set、info_units。
core_issue 为 null 或属于 issue_set；stance_target 为 null 或属于 issue_set。

InfoUnit 字段仅有：id、type、subtype、title、summary、content、source_refs、
speaker、side、actor、target、stance_target、created_at。

分类：
- entity：person / organization / object / location
- event：action / occurrence / transfer / communication
- state：temporal / quantitative / descriptive
- statement：fact_assertion / assessment / provision

source_refs 每项为 {file_id, source_kind, quote, start, end}，不能为空。
source_kind：document / communication / testimony / record / other。
每项 quote 必须逐字引用输入原文；start/end 为缓存 TXT 的 Unicode 字符区间。
后端负责计算、核验位置和来源元数据；无法唯一定位的引用不得进入案件。
created_at 由后端生成。模型边界与定位步骤将在抽取执行接口接通时实现。

speaker 可用于任何分类；side 是案件内角色字符串或 null，无全局角色枚举。
保留原发言人和不确定性；材料记载不等于事实已经证明，provision 不代表条款有效。
不得输出契约外字段或使用类型别名。本文件当前不由运行路径加载。
