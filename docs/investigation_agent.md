# Investigation Agent：自主调查与本轮工作记忆

入口为 `run_investigation(case, goal, selected_info_unit_ids=None, work_context=None, issue_key=None)` 和 `POST /api/cases/{case_id}/agent/run`。运行绑定当前案件的深拷贝；正式对象仍由用户逐项采纳 Proposal 后写入。

## 执行和交付

只有一个工具调用循环。首轮即提供 `search_info_units`、`get_relations`、`get_evidence`、五个 `propose_*` 工具及 `finish_investigation`，原生 `tool_choice=auto`。没有规划阶段、Question/InvestigationStep 模型、计划更新工具或关联参数。读取范围、工具顺序、Proposal 数量和结束时机由 Goal 与读取结果决定。

通过 finish 交付 `answer`、`answer_basis`、可选内容的 `findings`、`open_questions` 和 `completion_status`。空 Findings/Proposal 合法。answer_basis 为 general（能力/通用回答）、case_metadata（可靠案件统计）或 case_material（案件判断）；它描述答复依据，不创建另一条执行流程。涉及案件依据的成果必须选择 case_material。目录不授予材料或引用权限；尚无材料/有效读取结果时，案件判断返回可恢复错误。实际 empty、读取 error、空案件或预算限制可以支持明确的证据不足说明，不能证明事实。后端不以关键词猜测 Goal，也不能独立证明自然语言任务已经充分覆盖。

Findings、Proposal 引用须属于当前案件并实际取得；初始选中/Issue 调查材料提供的来源片段继续由缓存 TXT 核验。证据不足须列明未解条件；completed 不可同时宣称 Goal 仍有未解条件。答复保留原文、Markdown，长度上限与工具 Schema 一致为1000字符；非法答复不截断、不发布，成功 Proposal 不因之后模型失败而丢失。

读取与所有 Proposal 共享8次非 Finish 调用预算，失败、重复和缓存调用均计数；每轮最多6个有效 Proposal。finish 独立最多3次，总预算11次，最多14次模型调用（含不遵循工具协议的纠正余量）；重复无进展最多3次。预算与完整活动只由后端记录，耗尽的工具从下次 Schema 移除。连续非法调用/重复读取、模型失败和审计写入失败均有显式结束或异常，不自动重试付费请求。普通文本不能冒充已验证交付：产生 `finish_required` 后，后端持续关闭读取和 Proposal 工具，仅开放 Finish，越界调用在执行前拒绝；Finish 参数错误不会重新开放调查。

读取进展按工具合并后、下一轮模型实际可见的材料 ID 与本次请求比较：新增 InfoUnit、Relation 或 SourceRef，以及重新恢复被省略的材料均计为进展；空结果、仅返回已可见节点、仅调整排序或错误/统计更新不计进展。有效材料恢复会清零无进展计数，成功提交 Proposal 也会清零；连续3次读取/Proposal 无进展后关闭调查工具，仍保留 Finish；Finish 无论成功失败都不增加或重置无进展计数，失败后可在剩余3次 Finish 预算内修正重交。每次工具调用在本地审计和 Langfuse 记录 `read_progress/no_progress`，搜索还记录实际排除 ID。

共享8次非 Finish 预算耗尽后只提供 Finish；`budget_exhausted` 只按共享预算判断，6个有效 Proposal 上限不代表调查总预算耗尽。传给 Finish 的 `read_budget_exhausted` 表示读取阶段已关闭（共享预算耗尽、no_progress_limit 或 finish_required），`budget_exhausted` 仅表示共享8次已用尽。模型越界发出的第9次非 Finish 请求被拒绝，不执行；最终模型调用上限仍防止无限协议纠正。

错误恢复同时提供 `recovery_hint` 和 `next_action.steps`；每项包含错误位置、已知的 InfoUnit ID/标题、适用的来源索引、建议读取工具及修复动作。模型必须实际读取缺失材料，再修正原工具调用；错误提示中的目录信息不授予引用权限。模型上下文最多保留前8条错误及相应步骤，省略数量明确记录，本地审计保留完整错误。

首轮调查围绕具体信息需求搜索，不混合不同主题来覆盖全案。搜索过滤本次请求已可见的 InfoUnit，缓存按可见排除集隔离；`has_more` 不要求继续读完。取得的信息足以形成任务所需的探索性假设和缺口时，可以直接提交 Proposal 并结束检索。InfoUnit 内容足以支持探索性推理，原始来源只按具体措辞、时间、金额、主体或冲突核查需要读取；Evidence Link 和 Hypothesis Status 的来源要求保持不变，初始有效引用仍可使用。

一个 Evidence Gap 对应一个具体、可独立核查的缺失条件，说明确认内容、判断影响和明确补证动作/材料类型；独立条件分别建缺口，多份材料验证同一条件可合并。多个竞争性假设应先取得真实 ID，再关联一个共同区分性缺口。创建工具使用 `hypothesis_targets` 对象，内含 `hypothesis_ids` 与 `hypothesis_proposal_ids`；嵌套 `anyOf` 保证至少一项非空，后端再次校验结构、最多5个目标及当前 Issue/Run 的目标真实性，再映射回原有 Proposal 字段。Proposal 类型和采纳契约不变。

模型每次创建前须对照正式 reasoning 和本轮全部成功 proposals，按解释/缺失条件判断语义重复。后端另做有限文本归一化检查：保留 NFKC、casefold、明确的开头核查指令词清理及常规分隔标点/空白归一化；完整保留冒号后和括号内的命题、主体、身份及成立条件，不删除小数点、正负号、比例、比较符号或英文词间边界。检查范围为当前 Issue 的完整正式对象（包括模型投影省略的对象）及本轮成功 Proposal，且仅适用于 Hypothesis 与 Evidence Gap。命中时返回 `duplicate_proposal`、原对象来源和 ID，不追加、合并、改写或采纳对象。该检查不使用模糊相似度，也不能保证识别任意同义改写；优先避免错误拦截不同命题，用户仍逐项审阅。

Gap 创建工具和 Proposal 使用 `basis_info_unit_ids` 记录已有的形成依据，来源引用只能指向已取得的这些节点；不接受 `addressing_info_unit_ids`。正式 Gap 的回应列表初始为空，后续通过现有工作区编辑接口追加去重，材料关联不自动解决 Gap 或改变假设判断。上下文分别提供形成依据、回应材料及各自截断标记；这些引用本身不授予材料读取权限。Hypothesis、Evidence Link、Hypothesis Status 的 `info_unit_ids` 语义保持不变。已保存工作区的一次性迁移见 `docs/issue_reasoning.md`。

`propose_evidence_gap_update` 指定当前 Issue 已提供的 `gap_id`、1–5 个已取得的 `addressing_info_unit_ids`、建议 `status` 和已取得的来源引用。用户采纳前核对目标 Gap revision 与材料哈希，再复用 Gap 编辑入口追加去重回应材料和应用建议状态；保留原 ID、内容、形成依据、Hypothesis 关联。部分回应保持 open，不强制变更 Hypothesis judgment；只有独立的新条件才创建新 Gap。同一建议重复采纳幂等，无实际数据变化不递增 revision。

## 每轮上下文

Issue 的调查候选范围独立于 Canvas：`IssueWorkspace.investigation_info_unit_ids` 保存已登记的 InfoUnit；历史缺省时从原 `canvas.visible_node_ids` 中的 InfoUnit 初始化。`IssueWorkspaces.investigation_baseline_info_unit_ids` 固定记录初始化时已有节点，用来区分原先排除的旧材料和后来新增的材料。新建 Workspace 仍按 `related_issue == issue` 初始化。

Extraction 在提交 CaseData 前初始化已有 Workspace 的基线；初始化写入失败会中止提交，重试仍处理新文件。提交后读取 Workspace 时，以该基线和当前 CaseData 按正式 ID、`related_issue == issue` 幂等派生新增调查候选，下次 Workspace 保存时合并登记。无需在 Case 提交后再成功写第二个文件，因此 Workspace 暂未落盘的新 ID 仍能可靠进入 Agent 请求；Case 提交失败也不会提前暴露候选。基线初始化以前隐藏的节点保持排除，不根据时间或语义猜测其归属。

该过程不修改 Canvas 的可见节点、布局、层级、视口、选择、Focus 或当前视图。候选仍按选中项、Focus 和 Goal 相关性排序，初始最多8个 InfoUnit、12条两端均在已选材料中的 Relation，截断标志按独立调查范围计算。目录和既有只读工具能力保持原有边界。

另从案件索引中最近成功提交的 Extraction 输入快照读取 `selected_file_ids`（早期快照读取 `model_input.files[].file_id`），按 SourceRef 找出本批涉及的全部节点，包括旧 ID 追加新来源。失败/未提交运行不算新批次，人工历史基线不当作新增材料；无法读取最新范围时明确返回 unavailable，不猜测或静默回退到旧批次。本批材料作为临时候选补充，不修改持久 Issue 归属或 Canvas，也不因 related_issue 缺失或不同而排除。选中/Focus 优先后优先本批材料，再按 Goal 排序，仍共享8个节点上限；由 Agent 对照当前 Issue 的 Gap/Hypothesis 判断相关性。

`recent_extraction` 提供 run_id、有界文件 ID（最多20个）和计数、实际提供的本批节点与来源指针、明确省略标记。首轮仍最多8个来源片段、每节点1条、每条160字符；优先本批 SourceRef，并从缓存 TXT 独立核验该精确位置，旧节点的新来源也可进入首轮。`get_evidence` 本身的返回顺序、预算和无分页契约保持不变。

只发送 System Prompt 与一个动态 JSON User Message，不重复附带上一轮 Assistant/Tool 消息。动态字段：

- `goal`、`current_issue`、必要案件背景 `core_issue`。
- `case_info_directory`：总数、稳定 ID、标题、类型、截断及省略数量；按 ID 确定排序，最多80项、条目累计6000字符，标题最多120字符。
- `materials.info_units/relations/source_refs`：本轮实际取得的去重工具投影，各自带 `items/context_truncated/omitted_ids`。成功搜索摘要进入后续轮次；InfoUnit 的 `obtained_source_ref_indices` 只来自本轮实际取得的 SourceRef，空数组表示没有可用来源索引，搜索命中不等于已读取该节点全部来源。来源正文仍取自 source_refs，裁剪不会丢失完整工作记忆。
- `recent_extraction`：最近成功批次的范围和来源指针，不替代 materials 中的内容或来源校验。
- `reasoning`：当前 Issue 中相关的正式假设、缺口和证据关联，保留用户判断、弃用/解决状态，不发送修订号、时间戳和显示布局。
- `proposals`：本轮成功提交的完整提案，保留实际返回 ID 和依赖。
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

测试覆盖不建计划的事实检索、两个竞争性假设及共同缺口、跨轮引用、去重/省略/重新读取、非法来源、empty/truncated/error、预算、Proposal 部分成功和独立审批、Issue 隔离与 API。Case A 固定 Dataset/Gold/历史 Trace 不改写；Mock 不构成语义质量验收，真实模型行为需另行授权评测。

本地 UI smoke 可用 `backend/.venv/Scripts/python.exe backend/tests/serve_agent_smoke.py` 启动隔离后端，并在 frontend 运行 `npm run dev -- --host 127.0.0.1`。它只模拟模型边界，不访问外部模型；案件修改与审批只写临时目录。
