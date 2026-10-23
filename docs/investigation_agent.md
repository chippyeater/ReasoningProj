# Investigation Agent：工具成果与简洁总结

后端入口仍为 `run_investigation(case, goal, selected_info_unit_ids=None, work_context=None, issue_key=None)`，统一 API 为 `POST /api/cases/{case_id}/agent/run`。传入当前有效 CaseData；goal 为 1–2000 字符，可选 0–5 个案件内唯一节点。案件深拷贝绑定在运行时，不向模型暴露案件选择或写接口。

## 工具与决策

完整注册表只有八个原生函数工具：

- `search_info_units`、`get_relations`、`get_evidence`：原有有限只读检索。
- `propose_hypothesis`、`propose_evidence_link`、`propose_evidence_gap`、`propose_hypothesis_status`：分别校验并提交待确认建议，不能直接写正式对象。
- `present_findings`：复用既有发现与来源界面。每次最多四项带引用的发现、待核查问题和完成状态；追加不同发现，不创建画布节点，不按展示形式继续增设工具。

使用原有 LLM Client 原生函数调用，strict 工具参数，关闭并行调用。调查请求不再带完整结果 JSON Schema 或 response_format。模型最终直接回复 1–1200 字符自然语言；最终 JSON 不作为写入、引用或建议渠道。

事实问题直接检索并核对来源。分析问题按 Prompt 先考虑通常 2–3 种可区分、可检验的解释，针对支持、反对和区分性材料检索，之后才决定保留哪些建议；没有固定 Search → Relations → Evidence 或 Proposal 流程。临时调查计划不等于正式 Hypothesis。

8 次只读尝试、6 次建议尝试、2 次发现展示分别计数，错误参数也消耗对应预算；总计最多 16 次工具尝试及一次强制总结，最多 17 次模型调用。某类预算用尽后移除对应工具，其余工具仍可用；等价重复调用立即阻止并停止。预算和限制由后端报告，不让模型虚称完整。

`empty` 只表示当前条件未命中。`truncated`、工具错误或未检索到不能证明材料不存在；运行限制保留，完成状态偏保守。不对错误输出自动模型修复或重试。

## 成果、引用和审批

后端组装兼容的 `goal/findings/relevant_info_unit_ids/relevant_relation_ids/evidence_refs/open_questions/completion_status/proposals`，来源完全是成功工具提交的成果，不从最终总结解析。额外返回 `answer`、`summary_status`（completed/failed）、`limitations` 和运行计数。证据仍由 `info_unit_id + source_ref_index` 定位，API 只为这些已校验指针补全文名、原文和偏移，绝不把补全内容送回初始模型请求。

节点、关系及引用必须属于绑定案件、在本次实际可见；SourceRef 必须由 get_evidence 返回，关系中列出的依据 ID 不等于已读证据。非法候选在工具结果中明确拒绝、记录字段位置与类别，不追加成果，不静默丢弃。校验保证可定位，不能证明陈述真实或充分支持推论。

四个 Proposal 工具复用同一个 ReasoningProposal 存储和审批服务，kind 由工具确定，不是最终模型 JSON 的分发依据。返回的临时 proposal_id 只在本次运行中有效；仅 Gap 可用 hypothesis_proposal_ids 指向已成功提出的新 Hypothesis。其余临时依赖禁止，详见 [Issue 协作推理](issue_reasoning.md)。

工具成功后立即写入独立运行审计及版本凭据。自然语言总结失败且已有合法成果时，返回 summary_status=failed、answer=null，审计标记 partial；界面明确显示总结未完成，保留已验证的待确认建议。只有用户调用采纳接口才能正式写入，失败的总结既不会撤销工具成果，也不会连带采纳。没有成果的模型失败仍返回明确错误。审计写入失败不降级为成功。

## 上下文、安全和观测

初始请求仅为目标、核心争点、类型数量、选中摘要及有限 Issue/推理摘要。不注入全案节点、关系、文件、画布、历史或全部假设。工具仍按需检索全案。

案件文本、原文及工具结果始终作为 user/tool 数据；不拼成系统指令。运行时没有任意函数、文件操作、案件切换或正式写入能力。只追加 `runs/investigation-*.input/output.json`；CaseData、Issue 状态和画布不由调查过程修改。

复用 Langfuse generation/tool/guardrail/stop 及本地审计，保存请求、响应、工具参数、成功/拒绝结果、错误位置、版本、预算、停止原因和独立最终回复。默认云端只发送哈希、大小、状态及不含案件文本的诊断位置。

## 离线验证和局限

运行 `backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -q`。Case A 的真实固定 TXT 与节点通过临时目录、MockTransport、假 Langfuse 验证；不改 Gold、历史审计，也不调用付费模型。

分析策略是 Prompt 指导，不是硬编码 Planner；Mock 证明动态执行和安全边界，不能证明真实模型的调查质量。自然语言总结的语义不由引用存在性自动证明。查询工具仍无分页，不保证遍历被截断的全部材料；成果在现有 HTTP 请求结束时显示，不增加流式协议。

## 统一前端入口（MVP）

点击画布左下角“Agent 调查”，或右键菜单首项“向 Agent 提问”，打开同一个灵动岛，输入调查目标发送。运行中禁用重复提交；完成后默认展示简洁答复和状态，发现与原文依据按需展开，建议默认在画布预览并可从灵动岛展开审核。证据通过 API 按最终合法指针补全文件名、原文及 Unicode `[start,end)` 偏移；这部分展示适配不进入模型请求。点击依据节点定位画布，原文链接跳转结果中的具体证据。

顶部标签栏将 `core_issue` 与 `issue_set` 去重展示，突出核心争点；长标题通过悬停查看全文，标签过多时横向滚动，左右方向键切换。右侧状态由用户选择待调查、调查中或已结束，已结束可重新打开；Agent 不修改状态。

案件目录的 `issue_workspaces.json` 独立于 `case.json`，保存当前 Issue、各 Issue 的状态与画布（可见节点 ID、坐标、视口、选择/聚焦、显示层级）。使用既有 `WorkspaceState` 字段校验和原子 JSON 写入，不修改业务 Schema。Issue 问题全文作为键，当前不支持重命名或层级。`GET/PATCH /api/cases/{case_id}/issue-workspaces` 只管理界面工作状态；所有 InfoUnits、Relations、SourceRefs 仍是全案共享数据。

首次进入按 `related_issue` 推荐可见节点；后续只恢复保存的集合，不因推荐或内容编辑重新布局。右键“添加已有节点”可搜索全案标题/内容，将节点加入当前画布；“从当前画布移除”仅隐藏节点，保留其坐标，不删除业务信息。来源定位可加入尚未显示的节点。只渲染可见端点间的关系。

旧 `workspace_state` 不被改写：核心 Issue 初始化时继承旧坐标、显示层级和视口，但可见节点仍按议题推荐；核心 Issue 菜单“恢复旧画布节点”可重新显示原画布信息，已有 Issue 手动位置优先。没有 Issue 的案件继续使用原画布与首次提取入口。损坏的工作区文件返回明确错误，不覆盖旧数据。保存失败会显示提示及重试按钮；旧画布未成功保存时暂不切换 Issue。

Agent 请求增加可选 `current_issue`，后端验证其属于当前案件，并从存储构造 `work_context`：当前 Issue、Issue 列表与人工状态。问题与选中 ID 沿用原字段。初始模型输入保留核心争点、信息类型数量和最多 5 个选中节点摘要，额外最多 30 条 Issue 元数据（单条标题最多 500 字，包含当前 Issue，显式返回总数与是否省略）；不包含画布节点集合、布局、历史、原始文件或全案正文。工具照常按需检索全案。前端等待排队保存完成后再开展调查，避免把旧状态传给模型。

右键节点时，若节点未选中则将其作为单选上下文；若已选中则保留多选并聚焦该节点。右键空白处保留现有选择。菜单包含适应全部节点、开始提取、新建 InfoUnit、新建关系及适用的编辑命令；右键持久化关系可进入关系编辑。点击外部、Escape、拖动或缩放会关闭菜单；菜单和标签区的滚轮不驱动画布。空案件中央保留提取入口，无可解析材料时提示先上传并禁用提取。

调查结果只保留在当前前端会话，切换 Issue 或案件会清空结果并丢弃旧请求的迟到响应；不写入 CaseData。直接编辑案件 InfoUnit/Relation、自动操作画布或布局等请求仍返回 unsupported；持续推理目标则可返回候选建议，经用户确认后由独立接口写入 Issue 工作区。详见 [Issue 协作推理](issue_reasoning.md)。该语义判断仍有模型误判风险，尚未做真实模型验收；运行时仅暴露上述八个工具，所有正式写入仍需用户明确采纳。

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
