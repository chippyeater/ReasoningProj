# Investigation Agent v18：首轮决策与阶段化执行

入口仍为 `run_investigation(case, goal, selected_info_unit_ids=None, work_context=None, issue_key=None)` 和 `POST /api/cases/{case_id}/agent/run`。本轮绑定当前 CaseData 深拷贝，不提供案件切换或正式对象写入工具。

## 工作状态

`investigation_state.py` 仅定义本轮状态，不修改持久化 Schema。`KnowledgeState` 直接保存已有工具投影：InfoUnit 摘要（内容、类型、speaker）、有向 Relation（类型、依据及截断标记）、已核验 SourceRef（索引、原文/片段及文件位置）、初始上下文的遗漏与来源错误、历次检索条件/状态/返回 ID，以及成功 Findings 和 Proposals。选中节点、画布与初始核验来源直接入库；成功只读响应按 ID/来源索引合并，失败和空结果仅记录其实际状态，不创造知识。完整工具响应始终保存在运行审计。

Run 的原始 `goal` 始终完整保留，独立于 Question；层级为 Goal → Question → InvestigationStep。`QuestionState()` 初始为空，不创建特殊根问题。每个 Question 保存稳定 `id/question/status/related_info_unit_ids/evidence_refs/finding/block_reason/block_kind`，最多20项；`update_questions` 增量创建/调整且不删除已登记问题。所有 ID 地位相同，问题文本可以随调查细化，但不能改变 Run 的 Goal。

Run 初始 `phase=entry`，每次 Entry 请求 `tool_choice=required`，实际只注册 `finish_investigation` 和 `update_investigation_steps`；未开放的读取、Question 更新或 Proposal 工具即使被模型发出也不会执行。Entry 的 `update_investigation_steps(questions, steps)` 在副本上复用 Question 与 Step 校验，整批原子登记；每个 Question 至少关联一个 Step，初态均为 pending/in_progress。失败保持空状态和 Entry，成功才进入 Execution。计划消耗现有 Question/Step 共享更新预算，不增加额外 Agent 或规划循环。

Execution 开放十个现有工具，`update_investigation_steps(steps)` 恢复为行动增量更新；新子问题通过 `update_questions` 登记。`current_question_ids` 优先选择全部 in_progress Question，否则选择第一个 pending Question，无活动任务时为[]。只读/Proposal 的 null question_ids 使用此焦点；显式 ID 可以关联已登记问题。状态正文、节点、关系和来源优先呈现当前焦点的材料；后端记录活动与材料，不因工具 ok 自动回答问题。

简单的能力介绍、准确类型计数及上下文已明确提供的信息可以在 Entry 直接 finish：`completion_status=completed`，`findings/question_updates/step_updates=[]`，不建立调查任务或案件证据 Finding。需要调查或生成推理建议时先提交计划。Execution 的 finish 要求已登记子问题全部 answered 或具体 blocked，所有 Step completed/deferred；completed 必须有依据的 Finding，且没有 blocked Question。`evidence_insufficient` 必须有具体 blocked 条件。代码检查已登记任务及其依据的一致性，不能保证自然语言 Goal 已被完整拆分或答案在语义上充分。

调查行动独立于 Question 的回答字段，以 `QuestionState.steps` 保存，输出在 `question_state.investigation_steps`。最多20项，既不是正式画布对象也不是 Proposal。Question 可以独立存在，简单任务不必建立计划；Question 受阻不要求创建 Gap，Hypothesis 获得支持不自动 verified。

`InvestigationStep` 完整结构：`id: string`（1–80字符）、`question_id: string`（1–80字符）、`objective: string`（1–600字符）、`status: pending|in_progress|completed|deferred`、`result_note: string|null`（非空时1–600字符）、`basis_info_unit_ids: string[]`（最多80个唯一ID，单项1–600字符）、`activity_indices: integer[]`（全部关联活动的非负索引）、`activity_start_index: integer`（当前目标/本次重新打开的执行依据起点）。后两个字段由后端维护。文本须包含非空白字符。`update_investigation_steps(steps=[...])` 每次只提交需新增/调整的行动，字段为上述结构除两个活动索引字段；省略的行动不会删除，模型提交执行索引会被拒绝。整批验证后原子接受。

状态转换：新行动可从任意状态登记；pending 可保持或进入其余状态；in_progress 可保持、completed 或 deferred；completed/deferred 可保持原状态，或以原目标重新打开为 in_progress。已开始的工作不能重置 pending。Question 归属不能改变，终态目标不能覆写；重新打开后可调整目标，旧参数和状态变更仍保留在已有工具审计中。completed/deferred 必须记录结果/具体延期理由。非completed状态的 basis_info_unit_ids 须为[]。

completed 必须有以下至少一种明确依据：当前目标/重新打开后，以step_id关联的实际执行或缓存访问的合法只读结果（ok/empty/truncated），或显式 basis_info_unit_ids。每个指定ID均须属于当前案件、已提供/取得，且在所属Question的 related_info_unit_ids/evidence_refs 范围内；即使已有合法活动，错误材料ID仍会被拒绝。Question有任意材料不再自动构成依据。空搜索可以证明该Query未命中，不能证明案件不存在相关事实；失败或未经执行的响应不能充当完成依据。

创建、重新打开或变更目标时，后端把 activity_start_index 设为当前活动日志长度；旧 activity_indices 保留，但起点之前的记录不再自动支持新一轮完成。重新打开须传空材料列表，之后如要重用已有材料必须明确重新选择ID；当前状态及历史更新/读取都保存在原有活动和审计中，不复制来源或建立第二套执行日志。

三个只读工具增加可选 `step_id`，属于当前运行并且与唯一关联 Question 一致；question_ids=null/省略时使用该 Step 的 Question。未关联 Step 的读取仍有效。后端复用 `KnowledgeState.activities` 的稳定 activity_index，记录 step_id、executed、cached、状态和结果；Step 只保存指针，无第二套执行日志。真正执行或读取缓存的调用会将 pending 改为 in_progress，成功不会自动 completed。非法 Step/Question 关联不执行工具、不添加 Step 活动。终态需先重新打开才能关联新读取；已有上下文足够时无需重复调用工具。

## 每轮模型上下文

每轮重新组装以下消息，不累计重放历史：

1. 按阶段确定性组合的 System Prompt：Entry 为 Common + Entry，Execution 为 Common + Execution。
2. 一条 user JSON：phase、完整原始 goal、核心/当前 Issue、类型统计与有限画布/人工推理上下文，Question 进度、InvestigationStep 状态/活动指针与有限详情，KnowledgeState 实际材料及成功成果，剩余分类/总工具/模型预算和恢复错误。
3. 最近一次完成的 Assistant Tool Call + Tool Result 配对；最多保留一组。只有结果已经入状态并写入审计后才退役；大于 16000 字符的整组消息一并退役，不留下孤立 tool 消息。

状态 JSON 使用 48000 字符预算作为确定性的 Token 规模代理，材料按当前任务优先选择；正文必要时展示 500 字符片段，分区预算控制节点、关系、来源、活动和成果。所有省略通过 `context_truncated/omitted_ids` 表达，完整数据不从审计或内存删除。最近工具结果在 12000 字符内原样展示，超限使用有截断标记的片段；不调用 LLM 总结。成功的重复只读请求返回本轮缓存，支持重新获取被省略来源而不重复执行查询；同状态失败和重复 Proposal 不重执行。

`investigation_opportunities` 按 Question 从当前案件索引和已有 KnowledgeState/活动记录即时派生：关联材料尚未提供的来源/片段、未获取的一跳节点，以及问题/缺失条件文本匹配的候选材料和截断检索范围。它是有数量/字符预算的启发式检索建议，保留候选 ID、标题、原因及可用只读动作；不复制原文，不授予 seen 引用权限，不维护独立的线索生命周期。该算法暂时仅用于本地及 Langfuse 最终工作状态审计，不向模型请求注入候选 ID、标题或工具推荐，也不参与结束校验。模型根据 Question、已获取材料、调查进展及未解决条件，自主使用现有检索工具决定调查范围与结束时机。

## 结束工具与恢复

Entry 只注册两个决策工具；Execution 注册十个原生工具：三个只读查询、四个原有 `propose_*`、`update_questions`、`update_investigation_steps` 和 `finish_investigation`。继续使用现有 Chat Completions 客户端，strict 参数、关闭并行调用，无 result response_format。Entry 始终 required；Execution 默认 auto，普通文本后的恢复请求 required。

`finish_investigation(answer, findings, question_updates, step_updates, completion_status)` 增加必填 step_updates，无更新时传[]。先在副本中审核Question，然后复用同一个 update_steps 对Step增量更新进行案件、归属、转换与依据校验；最后统一检查所有问题 answered 或具体 blocked、登记的Step均completed/deferred、成果/引用/Proposal/Issue/预算一致，并沿用简洁回复校验（默认40–80字，上限240个非空白字符）。pending/in_progress即使被写成evidence_missing也不能结束；错误指向Step及finish.step_updates，仍可在调查过程中使用update_investigation_steps。某Question的行动全部deferred时不能answered；全部延期仍在limitations显式提示。completed需要有依据的Finding且无blocked，证据不足可以空Findings、blocked+evidence_insufficient结束。

存在未读取的候选线索、未提供的来源或截断检索建议，不会阻止结构和状态均合法的结束。`evidence_insufficient` 仍需具体 blocked Question，`completed` 仍不得存在 blocked Question；引用只能使用实际取得的材料，预算类状态仍校验实际预算。模型自主判断调查是否充分，后端不以启发式候选清单决定结束，也不要求固定读取次数、假设数量或案件关键词。Question 不保存候选跳过清单。

Proposal 不再绑定“交付计划”。其 kind/字段约束、当前运行实际返回的 proposal_id、Hypothesis 依赖类型和先后顺序、已获 InfoUnit/SourceRef、Issue 隔离及用户审批全部由现有 Proposal 工具、成果校验和独立采纳服务负责。Proposal 调用的 question_ids 仍须为已登记问题，归属记录在同一活动日志，合法 Hypothesis 依赖可以跨子问题共享。原交付计划的一对一绑定、材料交集、不可替换绑定检查随该关系删除；新 Step 不接受 Proposal ID，也不能通过提交 Proposal 自动完成。没有另建相同绑定或兼容旧字段。

不通过时，一次返回可独立发现的Question、Step、状态、引用和答复问题，每项带loc、argument_loc、message、fix。step_updates非空时，Step更新、Question更新及最终成果构成同一提交边界：任何校验失败都不提交这批状态或成果，保留调用前合法状态；修正后重发Step和Question更新。错误明确返回step_updates_applied=false与question_updates_applied=false。step_updates=[]时，沿用既有机制：合法Question更新可以作为工作记忆保留，下一次question_updates=[]复用；非法Question批次不接受。诊断副本不授权引用、不提交失败成果。通过后直接返回answer与完整成果，**没有后续总结模型调用**。普通文本仅触发finish_required恢复，不伪造Assistant/Tool ID。

8次只读、6次Proposal、8次Question/Step共享更新、3次结束尝试，最多25次工具尝试、28次模型调用，所有上限不变。更新预算耗尽后，仍能在本次finish请求携带step_updates合法收尾，只消耗原finish/total/model预算，不调用额外模型、不自动延期、不跳过依据或未完成状态校验。错误也消耗预算；连续三次无进展仍停止。预算/异常停止不冒充通过finish，保留未完成工作、answer=null及summary_status=failed；已有合法Proposal仍可审核。模型传输或审计写入失败不降级为成功。

## 成果、审批与审计

继续返回 `goal/findings/relevant_info_unit_ids/relevant_relation_ids/evidence_refs/open_questions/completion_status/proposals/answer/summary_status/limitations`，补充 `question_state` 和运行计数；`open_questions` 仅从未回答的 Question 派生。现有前端 Findings、来源和建议组件无需新任务界面。API 按已验证来源指针补全文名、原文和偏移。

引用必须属于当前案件、实际提供的节点/关系/来源；关系依据 ID 不授予未读来源权限。可定位与充分支持不同，保留当事人陈述、推论与事实的边界。探索性 Hypothesis/Gap 仍可无直接材料，Link/判断调整仍需要引用。同轮依赖、过期校验和幂等采纳沿用现有服务，所有正式写入仍需用户逐项采纳。

本轮记忆只写入既有 runs 审计，不写 CaseData、Issue ReasoningState 或画布。审计保留原始模型请求/响应、所有工具参数/结果、最终 KnowledgeState、QuestionState、phase、版本及停止原因；Langfuse 保留 generation/tool 与最终状态观察，继续遵循现有隐私配置。案件材料只进入 user/tool 数据，不变为 System 指令。

## Prompt 源版本与组合追踪

`versions.json.prompts` 独立登记 `investigation_common`、`investigation_entry`、`investigation_execution` 的稳定路径、数字版本与 SHA-256；`compositions` 登记 `investigation_agent_entry/execution` 的有序源名称。版本同步脚本校验所有源与组合引用，`--sync` 只递增内容变化的源版本，组合没有独立计数器。其他 Agent 继续使用同一加载器的单文件接口。

通用 `load_composed_prompt` 复用 `load_prompt` 校验组件，将规范化读取的源文本以两个 LF (`\n\n`) 原样连接，不 trim、不调用模型总结。组合 SHA-256 来自实际发送的完整 UTF-8 文本；版本为 `c1:<name>@vN+<name>@vN:<完整组合SHA256>`。规则固定为 concat-v1，组件名称、版本和顺序均进入版本标识。

Run 开始时冻结两种组合，`.input.json.system_prompts` 保存全文与组件元数据，输出审计保存 `prompt_manifest`。每条 model_calls 与 Langfuse Generation 使用同一份 phase、prompt_name/version/sha256、prompt_components、prompt_composition、schema_version、model 和发布状态。组件 Commit 是源文件最后变更 Commit；组合 Commit 在组件及注册表均可由 Git 验证时记录当前 HEAD，否则为 null/uncommitted。工具审计记录调用时 phase 和 phase_after，成功计划明确记录 entry → execution。

旧单文件移到 `backend/prompts/archive/investigation_agent_prompt_v17.md`，不参与当前注册、加载或同步。冻结历史审计及原版本字段不改写。

## 离线验证与局限

运行 `backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -q`。Mock 覆盖连续多任务、压缩后材料可用、空/错/截断状态、未读建议下合法结束、Step 执行/延期/遗漏与共享预算、Gap 依赖、正常/非法结束、预算和恢复；原有 HTTP、来源权限、Issue 隔离、Proposal 依赖/过期/幂等采纳继续回归。Case A TXT/Gold 仅作为临时案件输入，不修改固定评测或历史记录，不调用付费模型。

结构检查能排除虚构活动索引、未知问题、跨Question的Step读取、无明确依据的completed和旧执行记录被自动复用；不能凭ID判断指定材料与目标是否相关，也不能保证调查充分或延期合理。自然语言声称执行过某工具不能靠文本校验保证，以真实活动为准。全延期不能被标成任务已回答；明确blocked的证据不足交付仍可接受，供审计发现形式化延期。

校验能检查明确登记的任务和计划，不能自动发现模型从未识别的解释，也不能仅凭引用确定结论语义真实或充分。Question 的重要性与是否值得继续调查仍由模型判断；Mock 不代表真实模型质量。字符预算是规模代理，非提供商精确 Token 计数；查询仍无分页，不保证遍历全部截断材料。没有跨会话长期记忆、通用 Agent 框架或额外总结模型。

线索文本匹配及一跳关联可能误命中或漏掉相关材料，因此仅作为检索建议。调查充分性和结束时机属于模型的语义判断；仍需真实模型验证目标拆分、缺失条件识别、检索选择及恢复行为，不能以这套结构校验宣称已证明调查充分。

## v18 离线验收（2026-10-27）

- `backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -q`：358项通过。
- `backend/.venv/Scripts/python.exe -m unittest discover -s evals/case_a/tests -p 'test_*.py' -q`：21项通过；`evals/case_a/tests/validate_eval.py` 的文件哈希、来源切片、Schema及引用完整性检查通过。
- `backend/.venv/Scripts/python.exe scripts/run_investigation_eval.py dry-run`：A1/A2/A3各3次Mock模型调用，均通过finish校验、0次工具错误，共36个内存Span；0次付费调用，不代表真实任务语义质量。
- frontend下 `npm run build` 与 `node --experimental-strip-types --test tests/investigation-presentation.test.mjs tests/investigation-summary.test.mjs`：构建成功，14项通过。
- `scripts/sync_versions.py --sync/--check`、`git diff --check`通过；其他Agent的Prompt版本/哈希、Schema与Gold保持不变，v17归档文本未改写。

真实模型验收仍需检查：简单能力/计数请求是否正确直接完成；复杂Goal是否进入计划并完整拆分；A1/A2/A3的检索、来源核对与判断是否充分；错误依赖/引用、截断、受阻问题及预算限制后的恢复与结束是否合理。直接回答的语义正确性和计划对自然语言Goal的覆盖不由结构校验保证。

## 统一前端入口（MVP）

点击画布左下角“Agent 调查”，或右键菜单首项“向 Agent 提问”，打开同一个灵动岛，输入调查目标发送。运行中禁用重复提交；完成后默认展示简洁答复和状态，发现与原文依据按需展开，建议默认在画布预览并可从灵动岛展开审核。证据通过 API 按最终合法指针补全文件名、原文及 Unicode `[start,end)` 偏移；这部分展示适配不进入模型请求。点击依据节点定位画布，原文链接跳转结果中的具体证据。

顶部标签栏将 `core_issue` 与 `issue_set` 去重展示，突出核心争点；长标题通过悬停查看全文，标签过多时横向滚动，左右方向键切换。右侧状态由用户选择待调查、调查中或已结束，已结束可重新打开；Agent 不修改状态。

案件目录的 `issue_workspaces.json` 独立于 `case.json`，保存当前 Issue、各 Issue 的状态与画布（可见节点 ID、坐标、视口、选择/聚焦、显示层级）。使用既有 `WorkspaceState` 字段校验和原子 JSON 写入，不修改业务 Schema。Issue 问题全文作为键，当前不支持重命名或层级。`GET/PATCH /api/cases/{case_id}/issue-workspaces` 只管理界面工作状态；所有 InfoUnits、Relations、SourceRefs 仍是全案共享数据。

首次进入按 `related_issue` 推荐可见节点；后续只恢复保存的集合，不因推荐或内容编辑重新布局。右键“添加已有节点”可搜索全案标题/内容，将节点加入当前画布；“从当前画布移除”仅隐藏节点，保留其坐标，不删除业务信息。来源定位可加入尚未显示的节点。只渲染可见端点间的关系。

旧 `workspace_state` 不被改写：核心 Issue 初始化时继承旧坐标、显示层级和视口，但可见节点仍按议题推荐；核心 Issue 菜单“恢复旧画布节点”可重新显示原画布信息，已有 Issue 手动位置优先。没有 Issue 的案件继续使用原画布与首次提取入口。损坏的工作区文件返回明确错误，不覆盖旧数据。保存失败会显示提示及重试按钮；旧画布未成功保存时暂不切换 Issue。

Agent 请求增加可选 `current_issue`，后端验证其属于当前案件，并从存储构造 `work_context`：当前 Issue、Issue 列表与人工状态。问题与选中 ID 沿用原字段。初始模型输入保留核心争点、信息类型数量和最多 5 个选中节点摘要，额外最多 30 条 Issue 元数据（单条标题最多 500 字，包含当前 Issue，显式返回总数与是否省略）；包含有限画布语义摘要，不包含布局、历史、原始文件或全案正文。工具照常按需检索全案。前端等待排队保存完成后再开展调查，避免把旧状态传给模型。

右键节点时，若节点未选中则将其作为单选上下文；若已选中则保留多选并聚焦该节点。右键空白处保留现有选择。菜单包含适应全部节点、开始提取、新建 InfoUnit、新建关系及适用的编辑命令；右键持久化关系可进入关系编辑。点击外部、Escape、拖动或缩放会关闭菜单；菜单和标签区的滚轮不驱动画布。空案件中央保留提取入口，无可解析材料时提示先上传并禁用提取。

调查结果只保留在当前前端会话，切换 Issue 或案件会清空结果并丢弃旧请求的迟到响应；不写入 CaseData。直接编辑案件 InfoUnit/Relation、自动操作画布或布局等请求仍返回 unsupported；持续推理目标则可返回候选建议，经用户确认后由独立接口写入 Issue 工作区。详见 [Issue 协作推理](issue_reasoning.md)。该语义判断仍有模型误判风险，尚未做真实模型验收；运行时仅暴露上述十个工具，所有正式写入仍需用户明确采纳。

前端不再调用 InfoUnit Agent 提案/确认接口，但后端函数、接口与提案能力完整保留，手动 InfoUnit/Relation CRUD 与画布交互继续工作。

浏览器端到端离线验收（复用已有 Playwright，不安装为应用依赖）：

1. 运行 `backend/.venv/Scripts/python.exe backend/tests/serve_agent_smoke.py`，启动仅监听本机 8018 的隔离案件 A 后端，模型与追踪外发关闭，CRUD 与调查循环使用真实代码。
2. 在 frontend 目录设置进程环境 `VITE_API_BASE=http://127.0.0.1:8018`，运行 `node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5178 --strictPort`。
3. 在项目根目录设置 `PLAYWRIGHT_MODULE` 为本机已有 Playwright 或 playwright-core 模块路径，运行 `node frontend/tests/agent-smoke.cjs`。默认使用已安装的 `@playwright/test`，以可见浏览器测试。

脚本覆盖前端提交到真实循环的两条不同路径、原文与偏移展示、节点定位、运行/失败/不支持状态、案件数据不变、手动节点保存、关系编辑器、画布适应和跨案件迟到响应隔离。测试仅修改临时案件，结束后停止两个本地测试进程。API 测试另外通过 MockTransport 检查实际 HTTP 模型请求体，初始请求不含全案正文。

在相同离线服务与 Playwright 环境下，`node frontend/tests/canvas-navigation-smoke.cjs` 验证 Issue 标签切换不重载案件/不修改业务数据、空白与节点菜单、Agent 请求上下文、多选、编辑与关系操作、拖动缩放、空案件提取入口。生成接口在测试中被拦截，不触发真实模型。

### 调查结果展示

灵动岛默认只展示最终自然语言答复和重要限制；调查依据、来源、问题和建议分别按需展开，不把全部推理成果重复放在首屏。新运行重置展开状态，总结失败明确显示失败并保留待确认成果。不在界面直接显示运行 ID、临时 Proposal ID、文件 ID、节点 ID 或工具诊断。

来源仅按 `(file_id, start, end, quote)` 完全一致合并展示；同名文件、不同区间、内容不同的原文不会合并。全部证据指针与节点关联保留在原结果中，展示组可定位每个关联节点。原文保持原样；重复引用不计为独立佐证。没有与主要发现直接关联的原文时，不将其他发现的来源默认放在其下作为依据。

运行 `node --experimental-strip-types --test frontend/tests/investigation-presentation.test.mjs` 验证展示转换；在上述离线测试服务启动后，以相同 Playwright 环境运行 `node frontend/tests/investigation-result-smoke.cjs` 验证“替租人什么时候搬进去的？”的首屏与展开界面。重复引用在浏览器响应 fixture 中构造，取自案件 A 原文，不改 Gold、缓存或模型输出记录。


`node frontend/tests/issue-workspaces-smoke.cjs` 使用相同可见浏览器与隔离 Mock 后端，验证初始相关节点、添加共享节点、拖动缩放、A/B 切换与刷新恢复、状态重新打开、跨 Issue 共享编辑、Agent 当前议题/选中上下文、跨议题证据定位、可见端点连线及旧布局保留。`backend/tests/test_issue_workspaces.py` 另检查独立存储、非法输入/损坏文件保护及实际 Mock HTTP 模型请求体。

MVP 使用单客户端按顺序保存；不提供跨浏览器并发编辑冲突合并。调查结果与对话历史不写入 Issue 工作区，重新进入时通过现有 Agent 继续调查。
