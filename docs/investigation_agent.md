# Investigation Agent：自主调查与本轮工作记忆

入口为 `run_investigation(case, goal, selected_info_unit_ids=None, work_context=None, issue_key=None)` 和 `POST /api/cases/{case_id}/agent/run`。运行绑定当前案件的深拷贝；Proposal 即时校验暂存，正常 Finish 后整体校验并原子写入当前议题的内部推理对象。用户事后编辑或撤销，详见 `docs/issue_reasoning.md`。

## 执行和交付

只有一个工具调用循环。首轮提供三个读取工具、Hypothesis/Gap 创建和 Finish；工具可用性依据服务器绑定的正式 Issue 对象判断，不依赖投影是否显示该对象。仅在已有正式 Hypothesis 时提供状态建议、当前 Issue 实际存在 Gap 时提供 Gap Update 和 search_evidence_gaps、有正式或本轮待确认 Hypothesis 时提供 Evidence Link，原生 `tool_choice=auto`。没有规划阶段、Question/InvestigationStep 模型、计划更新工具或关联参数。读取范围、工具顺序、Proposal 数量和结束时机由 Goal 与读取结果决定。

通过 finish 交付 `answer`、`answer_basis`、可选内容的 `findings`、`open_questions` 和 `completion_status`。空 Findings/Proposal 合法。answer_basis 为 general（能力/通用回答）、case_metadata（可靠案件统计）或 case_material（案件判断）；它描述答复依据，不创建另一条执行流程。涉及案件依据的成果必须选择 case_material。目录不授予材料或引用权限；尚无材料/有效读取结果时，案件判断返回可恢复错误。实际 empty、读取 error、空案件或预算限制可以支持明确的证据不足说明，不能证明事实。后端不以关键词猜测 Goal，也不能独立证明自然语言任务已经充分覆盖。

Findings、Proposal 引用须属于当前案件并实际取得；SourceRef 仅通过实际调用 get_evidence 后取得，并由缓存 TXT 核验。证据不足须列明未解条件；completed 不可同时宣称 Goal 仍有未解条件。答复保留原文、Markdown，长度上限与工具 Schema 一致为1000字符；非法答复不截断、不发布，成功 Proposal 不因之后模型失败而丢失。

读取与所有 Proposal 共享8次非 Finish 调用预算，失败、重复和缓存调用均计数；每轮最多6个有效 Proposal。finish 独立最多3次，总预算11次，最多14次模型调用（含不遵循工具协议的纠正余量）；重复无进展最多3次。预算与完整活动只由后端记录，耗尽的工具从下次 Schema 移除。连续非法调用/重复读取、模型失败和审计写入失败均有显式结束或异常，不自动重试付费请求。普通文本不能冒充已验证交付：产生 `finish_required` 后，后端持续关闭读取和 Proposal 工具，仅开放 Finish，越界调用在执行前拒绝；Finish 参数错误不会重新开放调查。

读取进展按工具合并后、下一轮模型实际可见的材料及推理对象 ID 与本次请求比较：新增 InfoUnit、Relation、SourceRef 或当前推理视图中的 Hypothesis/Gap/Link，以及重新恢复被省略的对象均计为进展；空结果、仅返回已可见节点、仅调整排序或错误/统计更新不计进展。有效材料恢复会清零无进展计数，成功提交 Proposal 也会清零；连续3次读取/Proposal 无进展后关闭调查工具，仍保留 Finish；Finish 无论成功失败都不增加或重置无进展计数，失败后可在剩余3次 Finish 预算内修正重交。每次工具调用在本地审计和 Langfuse 记录 `read_progress/no_progress`，搜索还记录实际排除 ID。

共享8次非 Finish 预算耗尽后只提供 Finish；`budget_exhausted` 只按共享预算判断，6个有效 Proposal 上限不代表调查总预算耗尽。传给 Finish 的 `read_budget_exhausted` 表示读取阶段已关闭（共享预算耗尽、no_progress_limit 或 finish_required），`budget_exhausted` 仅表示共享8次已用尽。模型越界发出的第9次非 Finish 请求被拒绝，不执行；最终模型调用上限仍防止无限协议纠正。

错误恢复同时提供 `recovery_hint` 和 `next_action.steps`；每项包含错误位置、已知的 InfoUnit ID/标题、适用的来源索引、建议读取工具及修复动作。模型必须实际读取缺失材料，再修正原工具调用；错误提示中的目录信息不授予引用权限。模型上下文最多保留前8条错误及相应步骤，省略数量明确记录，本地审计保留完整错误。

首轮调查围绕具体信息需求搜索，不混合不同主题来覆盖全案。搜索过滤本次请求已可见的 InfoUnit，缓存按可见排除集隔离；`has_more` 不要求继续读完。取得的信息足以形成任务所需的探索性假设和缺口时，可以直接提交 Proposal 并结束检索。InfoUnit 内容足以支持探索性推理，原始来源只按具体措辞、时间、金额、主体或冲突核查需要读取；Evidence Link 和 Hypothesis Status 的来源要求保持不变。

一个 Evidence Gap 对应一个具体、可独立核查的缺失条件，说明确认内容、判断影响和明确补证动作/材料类型；独立条件分别建缺口，多份材料验证同一条件可合并。Gap 可关联一个或多个命题，不要求它们竞争；先取得实际 ID，同一事实问题只创建一次。创建工具使用 `hypothesis_targets` 对象，内含 `hypothesis_ids` 与 `hypothesis_proposal_ids`；嵌套 `anyOf` 保证至少一项非空，后端再次校验结构、最多5个目标及当前 Issue/Run 的目标真实性，再映射回原有 Proposal 字段。Proposal 类型保持不变，依赖在 Finish 后整体提交时解析为正式 ID。

Hypothesis 是可被证据支持或反驳的待验证命题；数量由任务与事实决定，单命题合法，不要求竞争或配对。每份 content 只表达该命题及必要条件，rationale 仅说明它的依据与不确定性，不放整套假设；模型复用已有 reasoning/proposals，成功提交即表示该动作已完成。后端另做有限文本归一化检查：保留 NFKC、casefold、开头 H1/假设1 等显示编号及核查指令词清理及常规分隔标点/空白归一化；完整保留冒号后和括号内的命题、主体、身份及成立条件，不删除小数点、正负号、比例、比较符号或英文词间边界。检查范围为当前 Issue 的完整正式对象（包括模型投影省略的对象）及本轮成功 Proposal，且仅适用于 Hypothesis 与 Evidence Gap。命中时返回 `duplicate_proposal`、原对象来源和 ID，不追加、合并、改写或采纳对象。该检查不使用模糊相似度，也不能保证识别任意同义改写；优先避免错误拦截不同命题，用户可事后查看、编辑和撤销。

Gap 创建工具和 Proposal 使用 `basis_info_unit_ids` 记录已有的形成依据，来源引用只能指向已取得的这些节点；不接受 `addressing_info_unit_ids`。正式 Gap 的回应列表初始为空，后续通过现有工作区编辑接口追加去重，材料关联不自动解决 Gap 或改变假设判断。上下文分别提供形成依据、回应材料及各自截断标记；这些引用本身不授予材料读取权限。Hypothesis、Evidence Link、Hypothesis Status 的 `info_unit_ids` 语义保持不变。已保存工作区的一次性迁移见 `docs/issue_reasoning.md`。

`propose_evidence_gap_update` 指定当前 Issue 已提供的 `gap_id`、1–5 个已取得的 `addressing_info_unit_ids`、建议 `status` 和已取得的来源引用。整轮提交前核对运行起点、目标 Gap 和材料哈希，再复用 Gap 编辑入口追加去重回应材料和应用建议状态；保留原 ID、内容、形成依据、Hypothesis 关联。部分回应保持 open，不强制变更 Hypothesis judgment；只有独立的新条件才创建新 Gap。同轮重复提交幂等，无实际数据变化不递增 revision。

初始投影仍最多选择三个推理对象，Hypothesis 与 Gap 共用名额。遗漏不代表正式对象不存在；有 Gap 的 Issue 开放 `search_evidence_gaps(query, info_unit_ids, limit, offset)`，按问题/关联命题关键词或已有材料关联查询，空条件可有界浏览。每次最多3个 Gap、5个相关 Hypothesis、5条 Link；问题与命题最多600字符，材料引用列表仍有截断标记。通过 offset 翻页，不默认注入全部对象或增加推理目录字段。

发现结果复用初始上下文的投影函数，替换现有 `reasoning` 视图，材料和来源工作记忆保留。实际读取的完整 Gap/关联 Hypothesis 目标进入现有后端校验与提交凭据上下文，包含原修订号；模型不接收这些审计字段。原 Gap 的完整关联在整轮提交时保留，查询和 Proposal 生成不修改正式工作区；并发修改时整轮拒绝提交。Gap 搜索只提供推理对象，不授予 InfoUnit 内容或 SourceRef 引用权限。

## 每轮上下文

首轮材料只来自请求明确选中的 InfoUnit，以及用户聚焦的 InfoUnit；聚焦 Hypothesis/Gap 不会自动取得其关联材料。仅发送有界摘要，以及两端都在这组选中材料内的既有 Relation。画布可见节点和新 Extraction 批次不会自动加入材料或来源，画布显示、布局、层级、视口和可见性独立保存。

`case_info_directory` 仅列当前材料视图之外的轻量 ID、标题、类型目录（总数仍为全案数量）；可见材料裁剪后再过滤目录，保证被省略的节点仍可发现。目录、正式 Gap 的形成依据和回应材料 ID 都不授予读取或引用权限。成功搜索的摘要才进入后续 KnowledgeState，SourceRef 按任务需要实际调用 get_evidence 取得。没有默认来源片段或首轮隐式读取。

后端读取最近成功提交的 Extraction 快照中 `selected_file_ids`，按 SourceRef.file_id 识别涉及的节点，包括追加来源的旧 InfoUnit。搜索仍按关键词匹配，但优先返回本批匹配材料；显式 get_evidence 时优先返回本批来源，保留原 SourceRef 索引及8条/4000字符上限，保证旧节点的新来源不会被前面的旧来源挤出。该优先级不进入模型首轮上下文，不修改持久 Issue 或 Canvas。失败/未提交运行不计入批次；人工 Extraction 历史登记不作为新材料，快照无效时读取工具明确报错，不回退或猜测。

工作区只保存 status、canvas、reasoning；Extraction 不再读写调查候选范围或工作区基线，也不依赖额外工作区写入。删除了调查范围字段、初始化函数和自动合并逻辑，不保留旧字段读取或迁移分支。

只发送 System Prompt 与一个动态 JSON User Message，不重复附带上一轮 Assistant/Tool 消息。动态字段：

- `goal`、当前工作议题 `current_issue`、主要争议焦点 `core_issue`（可空）。Issue 是案件争议焦点，具体待核实事实由 InfoUnit 或 Evidence Gap 承载。
- `case_info_directory`：总数、稳定 ID、标题、类型、截断及省略数量；按 ID 确定排序，最多80项、条目累计6000字符，标题最多120字符。
- `materials.info_units/relations/source_refs`：本轮实际取得的去重工具投影，各自带 `items/context_truncated/omitted_ids`。成功搜索摘要进入后续轮次；来源指针仅在 source_refs 中提供一次，不再在每个 InfoUnit 上重复列索引；搜索命中不授予来源引用权限，裁剪不会丢失完整工作记忆。
- `reasoning`：当前 Issue 中相关的正式假设、缺口和证据关联，保留用户判断、弃用/解决状态，不发送修订号、时间戳和显示布局。
- `proposals`：本轮已成功提交的提案，保留实际 ID、内容、依据、有效状态与依赖；仅在模型投影中省略 null 和空数组。完整提案与提交记录仍保存在审计和 API 结果中。
- `notices` 与 `recovery`：必要的空结果、截断范围、来源限制和当前错误。

KnowledgeState 按 InfoUnit ID、Relation ID、SourceRef 的节点/索引合并材料，完整状态不随上下文裁剪而删除。每轮从该状态重建视图；正文、关系和来源分别最多约6500/3500/7500字符，总上下文最多24000字符。优先保留本次明确读取的材料、Goal 文本相关材料和近期材料；超限明确列出省略 ID，模型可重新读取，缓存结果也重新进入视图。不发送完整工具历史、活动索引、计划状态、预算计数或运行审计。

## Prompt 和可观测性

当前唯一源文件是 `backend/prompts/investigation_common_prompt.md`，稳定注册名 `investigation_common`；使用所有 Agent 共用的 `load_prompt`。阶段 Prompt、组合注册、组合加载接口和对应测试已移除。`sync_versions.py --sync` 只递增变化文件版本，`--check` 校验实际内容 SHA；业务 Schema 与 Gold 版本不变。Git 及冻结历史运行保留原样。

输入审计保存实际 System Prompt 和工具 Schema；每次模型调用与 Langfuse Generation 使用相同名称、版本、SHA、可验证 Commit、Schema、模型和发布状态。工具参数/结果、预算、读取统计和最终 KnowledgeState 在本地审计与对应 Trace 中保留。用户数据不进入 System 指令。

## 离线验证

```powershell
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -q
backend/.venv/Scripts/python.exe -m unittest discover -s evals/case_a/tests -p 'test_*.py' -q
backend/.venv/Scripts/python.exe evals/case_a/tests/validate_eval.py
backend/.venv/Scripts/python.exe scripts/run_investigation_eval.py dry-run
backend/.venv/Scripts/python.exe scripts/sync_versions.py --sync
backend/.venv/Scripts/python.exe scripts/sync_versions.py --check
npm --prefix frontend run build
node --experimental-strip-types --test frontend/tests/investigation-presentation.test.mjs frontend/tests/investigation-summary.test.mjs
git diff --check
```

测试覆盖不建计划的事实检索、单命题与相关缺口、多个独立或竞争命题及共享缺口、跨轮引用、去重/省略/重新读取、非法来源、empty/truncated/error、预算、Proposal 部分成功和整轮提交/撤销、Issue 隔离与 API。Case A 固定 Dataset/Gold/历史 Trace 不改写；Mock 不构成语义质量验收，真实模型行为需另行授权评测。

本地 UI smoke 可用 `backend/.venv/Scripts/python.exe backend/tests/serve_agent_smoke.py` 启动隔离后端，并在 frontend 运行 `npm run dev -- --host 127.0.0.1`。它只模拟模型边界，不访问外部模型；案件修改与整轮提交只写临时目录。
