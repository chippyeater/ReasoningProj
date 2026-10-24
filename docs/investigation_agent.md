# Investigation Agent：结构化工作记忆与执行闭环

入口仍为 `run_investigation(case, goal, selected_info_unit_ids=None, work_context=None, issue_key=None)` 和 `POST /api/cases/{case_id}/agent/run`。本轮绑定当前 CaseData 深拷贝，不提供案件切换或正式对象写入工具。

## 工作状态

`investigation_state.py` 仅定义本轮状态，不修改持久化 Schema。`KnowledgeState` 直接保存已有工具投影：InfoUnit 摘要（内容、类型、speaker）、有向 Relation（类型、依据及截断标记）、已核验 SourceRef（索引、原文/片段及文件位置）、初始上下文的遗漏与来源错误、历次检索条件/状态/返回 ID，以及成功 Findings 和 Proposals。选中节点、画布与初始核验来源直接入库；成功只读响应按 ID/来源索引合并，失败和空结果仅记录其实际状态，不创造知识。完整工具响应始终保存在运行审计。

`QuestionState` 以用户目标初始化根任务 Q1，无规划模型调用；最多 20 个 Question。每项包含稳定 `id/question/status/related_info_unit_ids/evidence_refs/finding/block_reason`，以及区分证据缺失、工具故障、不支持和预算限制的 `block_kind`。`update_questions` 可一次新增、拆分或调整多项任务；不会删除已有任务。只读/Proposal 工具的 `question_ids` 可以关联任务，null 自动选择 in_progress 任务或第一个 pending 任务。后端记录活动及取得的材料/来源，**不会因为工具 ok 自动回答问题**。回答/阻塞状态由模型明确更新，并校验材料权限和状态一致性。

Q1 的问题全文不可缩减；独立请求和条件性后续问题由模型登记为独立 Question。只要有登记的方面 blocked，汇总 Q1 就不能 answered。`evidence_insufficient` 必须保留具体 blocked 任务，禁止所有任务 answered 却报告不足。代码检查已登记任务的一致性，不能确定自然语言目标是否正确、完整地拆分；简单问题仍可在第一次 finish 中直接完成，无需规划或额外检索。

Question 内的 `planned_actions` 登记已识别的重要候选解释、证据关联和缺口（每项最多六个）。计划必须引用实际成功返回、类型匹配的 Proposal ID，或用 `omission_reason` 说明不提交原因；Hypothesis/Gap 还要求 description 与 Proposal content 相符，一个 Proposal 不能消解两个计划。已登记计划不能通过删除或改名绕过结束检查。Question 受阻不要求创建 Gap，Hypothesis 获得支持不自动 verified；正式依赖和采纳服务不变。

## 每轮模型上下文

每轮重新组装以下消息，不累计重放历史：

1. 稳定 System Prompt，仅说明角色、证据标准和实际工具契约。
2. 一条 user JSON：原始目标、核心/当前 Issue、类型统计与有限画布/人工推理上下文，Question 进度与当前任务，KnowledgeState 实际材料及成功成果，剩余分类/总工具/模型预算和恢复错误。
3. 最近一次完成的 Assistant Tool Call + Tool Result 配对；最多保留一组。只有结果已经入状态并写入审计后才退役；大于 16000 字符的整组消息一并退役，不留下孤立 tool 消息。

状态 JSON 使用 48000 字符预算作为确定性的 Token 规模代理，材料按当前任务优先选择；正文必要时展示 500 字符片段，分区预算控制节点、关系、来源、活动和成果。所有省略通过 `context_truncated/omitted_ids` 表达，完整数据不从审计或内存删除。最近工具结果在 12000 字符内原样展示，超限使用有截断标记的片段；不调用 LLM 总结。成功的重复只读请求返回本轮缓存，支持重新获取被省略来源而不重复执行查询；同状态失败和重复 Proposal 不重执行。

`investigation_opportunities` 按 Question 从当前案件索引和已有 KnowledgeState/活动记录即时派生：关联材料尚未提供的来源/片段、未获取的一跳节点，以及问题/缺失条件文本匹配的候选材料和截断检索范围。它是有数量/字符预算的启发式发现索引，保留候选 ID、标题、原因及可用只读动作；不复制原文，不授予 seen 引用权限，不维护独立的线索生命周期。模型可继续调查，也可在现有 Question 的 `skipped_leads[{lead_id,reason}]` 说明某项为何不能补足缺失条件。删除 Question 的材料 ID 不能抹去已经记录的获取活动。

## 结束工具与恢复

注册九个原生工具：三个只读查询、四个原有 `propose_*`、`update_questions` 和 `finish_investigation`。继续使用现有 Chat Completions 客户端，strict 参数、关闭并行调用，无 result response_format。

`finish_investigation(answer, findings, question_updates, completion_status)` 合并原 present_findings 路径。后端用副本原子校验最终 Question 更新：所有任务 answered 或具体 blocked、回答具备已获取材料、已登记计划已提交或说明放弃、最终成果包含回答所用材料及来源、Proposal/引用/Issue 依赖一致、状态与实际预算一致，并沿用简洁回复校验（默认 40–80 字，上限 240 个非空白字符）。`completed` 需要有依据的 Finding 且无 blocked；证据不足可以空 Findings、blocked + evidence_insufficient 结束。

对 blocked 的实质调查，重新派生可用线索；尚未读取或逐项说明不继续理由的线索会产生 `uninvestigated_lead`，返回对应工具动作并允许在既有预算内恢复。检索已耗尽时可明确标记预算阻塞，不能悄悄等同于案件缺证；只耗尽 Proposal 预算不能绕过仍可读取的线索。没有最低读取次数、固定假设数量或固定案件关键词要求，也不要求扫描全案或读取每条来源；合理的范围限制与无助于当前条件的候选可明确说明后结束。

不通过时返回带校验路径和待处理项的结构化工具错误，在剩余预算内修正；不采纳失败的 Findings 或最终 Question 更新。通过后直接返回 answer 与完整成果，**没有后续总结模型调用**。已删除旧消息 append、临时 System Completion Check、动态预算 System 消息和普通文本直接结束路径。提前输出文本仅产生 user 状态中的 `finish_required` 恢复信息；下一轮要求原生工具调用，不伪造 Assistant/Tool ID。

8 次只读、6 次 Proposal、8 次 Question 更新、3 次结束尝试独立计数，最多 25 次工具尝试、28 次模型调用。错误也消耗相应预算；连续三次失败、重复无进展或提前文本会异常停止。预算/异常停止不冒充通过 finish：保留 unresolved 状态，返回 answer=null、summary_status=failed 和明确 stop_reason；已有合法 Proposal 仍可审核。没有成果的模型传输失败仍返回错误。审计写入失败不降级为成功。

## 成果、审批与审计

继续返回 `goal/findings/relevant_info_unit_ids/relevant_relation_ids/evidence_refs/open_questions/completion_status/proposals/answer/summary_status/limitations`，补充 `question_state` 和运行计数；`open_questions` 仅从未回答的 Question 派生。现有前端 Findings、来源和建议组件无需新任务界面。API 按已验证来源指针补全文名、原文和偏移。

引用必须属于当前案件、实际提供的节点/关系/来源；关系依据 ID 不授予未读来源权限。可定位与充分支持不同，保留当事人陈述、推论与事实的边界。探索性 Hypothesis/Gap 仍可无直接材料，Link/判断调整仍需要引用。同轮依赖、过期校验和幂等采纳沿用现有服务，所有正式写入仍需用户逐项采纳。

本轮记忆只写入既有 runs 审计，不写 CaseData、Issue ReasoningState 或画布。审计保留原始模型请求/响应、所有工具参数/结果、最终 KnowledgeState、QuestionState、版本及停止原因；Langfuse 保留 generation/tool 与最终状态观察，继续遵循现有隐私配置。案件材料只进入 user/tool 数据，不变为 System 指令。

## 离线验证与局限

运行 `backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -q`。Mock 覆盖连续多任务、压缩后材料可用、空/错/截断状态、多候选遗漏、Gap 依赖、正常/非法结束、预算和恢复；原有 HTTP、来源权限、Issue 隔离、Proposal 依赖/过期/幂等采纳继续回归。Case A TXT/Gold 仅作为临时案件输入，不修改固定评测或历史记录，不调用付费模型。

校验能检查明确登记的任务和计划，不能自动发现模型从未识别的解释，也不能仅凭引用确定结论语义真实或充分。Question 的重要性与是否值得继续调查仍由模型判断；Mock 不代表真实模型质量。字符预算是规模代理，非提供商精确 Token 计数；查询仍无分页，不保证遍历全部截断材料。没有跨会话长期记忆、通用 Agent 框架或额外总结模型。

线索文本匹配及一跳关联可能误命中或漏掉相关材料，`skipped_leads.reason` 的存在只证明模型明确作过取舍说明，不保证该理由在语义上合理。仍需真实模型验证目标拆分、缺失条件识别、检索选择及拒绝后是否实际推进；不能以这套结构校验宣称已证明调查充分。

## 统一前端入口（MVP）

点击画布左下角“Agent 调查”，或右键菜单首项“向 Agent 提问”，打开同一个灵动岛，输入调查目标发送。运行中禁用重复提交；完成后默认展示简洁答复和状态，发现与原文依据按需展开，建议默认在画布预览并可从灵动岛展开审核。证据通过 API 按最终合法指针补全文件名、原文及 Unicode `[start,end)` 偏移；这部分展示适配不进入模型请求。点击依据节点定位画布，原文链接跳转结果中的具体证据。

顶部标签栏将 `core_issue` 与 `issue_set` 去重展示，突出核心争点；长标题通过悬停查看全文，标签过多时横向滚动，左右方向键切换。右侧状态由用户选择待调查、调查中或已结束，已结束可重新打开；Agent 不修改状态。

案件目录的 `issue_workspaces.json` 独立于 `case.json`，保存当前 Issue、各 Issue 的状态与画布（可见节点 ID、坐标、视口、选择/聚焦、显示层级）。使用既有 `WorkspaceState` 字段校验和原子 JSON 写入，不修改业务 Schema。Issue 问题全文作为键，当前不支持重命名或层级。`GET/PATCH /api/cases/{case_id}/issue-workspaces` 只管理界面工作状态；所有 InfoUnits、Relations、SourceRefs 仍是全案共享数据。

首次进入按 `related_issue` 推荐可见节点；后续只恢复保存的集合，不因推荐或内容编辑重新布局。右键“添加已有节点”可搜索全案标题/内容，将节点加入当前画布；“从当前画布移除”仅隐藏节点，保留其坐标，不删除业务信息。来源定位可加入尚未显示的节点。只渲染可见端点间的关系。

旧 `workspace_state` 不被改写：核心 Issue 初始化时继承旧坐标、显示层级和视口，但可见节点仍按议题推荐；核心 Issue 菜单“恢复旧画布节点”可重新显示原画布信息，已有 Issue 手动位置优先。没有 Issue 的案件继续使用原画布与首次提取入口。损坏的工作区文件返回明确错误，不覆盖旧数据。保存失败会显示提示及重试按钮；旧画布未成功保存时暂不切换 Issue。

Agent 请求增加可选 `current_issue`，后端验证其属于当前案件，并从存储构造 `work_context`：当前 Issue、Issue 列表与人工状态。问题与选中 ID 沿用原字段。初始模型输入保留核心争点、信息类型数量和最多 5 个选中节点摘要，额外最多 30 条 Issue 元数据（单条标题最多 500 字，包含当前 Issue，显式返回总数与是否省略）；包含有限画布语义摘要，不包含布局、历史、原始文件或全案正文。工具照常按需检索全案。前端等待排队保存完成后再开展调查，避免把旧状态传给模型。

右键节点时，若节点未选中则将其作为单选上下文；若已选中则保留多选并聚焦该节点。右键空白处保留现有选择。菜单包含适应全部节点、开始提取、新建 InfoUnit、新建关系及适用的编辑命令；右键持久化关系可进入关系编辑。点击外部、Escape、拖动或缩放会关闭菜单；菜单和标签区的滚轮不驱动画布。空案件中央保留提取入口，无可解析材料时提示先上传并禁用提取。

调查结果只保留在当前前端会话，切换 Issue 或案件会清空结果并丢弃旧请求的迟到响应；不写入 CaseData。直接编辑案件 InfoUnit/Relation、自动操作画布或布局等请求仍返回 unsupported；持续推理目标则可返回候选建议，经用户确认后由独立接口写入 Issue 工作区。详见 [Issue 协作推理](issue_reasoning.md)。该语义判断仍有模型误判风险，尚未做真实模型验收；运行时仅暴露上述九个工具，所有正式写入仍需用户明确采纳。

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
