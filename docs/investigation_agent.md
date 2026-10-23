# Investigation Agent Loop（阶段 2）

后端入口：`app.investigation_agent.run_investigation(case, goal, selected_info_unit_ids=None)`，异步返回可 JSON 序列化的数据。传入当前有效 `CaseData`；可选选中节点为 0–5 个案件内唯一 ID。goal 为 1–2000 字符。统一入口通过 `POST /api/cases/{case_id}/agent/run` 调用，无新增布局生成或 Agent 框架。

```python
from app.investigation_agent import run_investigation

result = await run_investigation(case, "双方关于实际损失的说法有哪些依据？", ["U029"])
```

## 决策与预算

- 绑定案件深拷贝，初始模型上下文仅为 goal、核心争点（最多 500 字符）、五类节点数量和选中节点摘要。没有全案节点、关系、文件目录、来源目录或案件选择参数；统一入口另可提供下述有限 Issue 元数据。
- 使用现有 Client 的配置、认证、超时和单次 HTTP 传输，增加原生 Chat Completions 函数调用入口。三个查询工具每轮均可选择；模型依据前轮结果决定换词、筛选、扩展一跳、读原文或结束，没有固定工具顺序。
- 工具定义为 strict，关闭并行调用；最终输出使用严格 JSON Schema。协议依据：[OpenAI 官方函数调用文档](https://developers.openai.com/api/docs/guides/function-calling)。配置模型须同时支持函数调用和结构化输出，失败不换模型、不降级、不自动重试。
- 最多 8 次工具调用尝试，包括错误参数或未授权工具请求。实际只读函数执行次数不超过尝试次数。每次结果仍受[阶段 1 工具预算](investigation_tools.md)约束。
- 等价查询不重复执行：搜索忽略归一化大小写、词序和重复关键词，补齐可选参数默认值，类型筛选与关系节点顺序不影响等价性。证据节点顺序保留，因为顺序会影响可返回引用前缀。
- 预算用尽或重复查询时，关闭后续工具能力，仅允许再进行一次模型总结；最多 9 次模型调用。模型可提前结束。停止后仍请求工具、返回格式错误或引用非法时明确失败，不再模型修复。
- `empty` 允许换词、换工具，不能作为“证据不存在”的证明；`invalid_request/error` 允许纠正参数或调查别的材料。所有截断、无效请求和工具错误保留为运行限制，保守地阻止本轮报告 completed，即使后来取得了部分材料。
- 最终没有使用精确原文引用或没有发现时，不能报告 completed；预算状态由运行时决定。空检索之后若改变查询并取得完整相关原文，可报告在目标范围内完成，不能声称全案检索完备。

## 结果与引用

返回字段：

- `goal`：必须与请求相同。
- `findings`：每项有 `statement/info_unit_ids/relation_ids/evidence_refs`，至少一项实际获得的依据；最多 12 项发现。
- `relevant_info_unit_ids/relevant_relation_ids/evidence_refs`：最终使用的引用清单；证据指针为 `{info_unit_id, source_ref_index}`，不复制原文或模型生成的偏移。
- `open_questions`：尚未解决问题及运行时限制。
- `completion_status`：`completed / evidence_insufficient / budget_exhausted / unsupported`，分别表示目标范围内完成、依据不足、预算耗尽、当前不支持的操作。
- 运行元数据：`run_id/stop_reason/tool_call_count/tool_attempt_count/model_call_count`。

后端只接受初始选中摘要、本轮搜索/一跳关系实际返回的节点，以及证据工具实际返回的节点与引用索引。关系依据列表中出现某个节点 ID，不等于读到了该节点。所有引用还需属于绑定案件；每项发现的引用必须列在顶层清单，证据指针的节点也必须列入相关节点。非法/重复/未取回的引用拒绝整份结果，原始失败输出保留在审计中，不静默丢弃。

该校验保证引用存在及本轮可见性，不能自动证明陈述被引用充分支持或事实为真。单方陈述、关系推断和已核实事实由 Prompt 明确区分，语义质量仍需真实模型评估或人工复核。

## 追踪与只读范围

复用 `RunTrace` 的 generation、tool、guardrail observations，记录请求、响应、usage、Prompt 版本/哈希、停止原因和最终输出。沿用隐私策略：默认云端只发长度、哈希和状态摘要，只有显式启用 capture_content 才发送脱敏内容。

本地审计写入 `runs/investigation-<uuid>.input.json` 与 `.output.json`：保存完整脱敏模型请求/响应、工具原始与规范化参数、结果、是否执行、停止原因和最终调查结果；本地审计不可写时明确失败。只写独立审计，不保存 CaseData，不新增/修改节点、关系、StageRun 或工作区状态。现有 StageRun CLI 索引不会列出调查运行；按返回 run_id 定位审计即可。

目标和工具结果始终作为 user/tool 数据消息，禁止把原文拼成系统消息或执行命令。运行时只有三个固定只读分发入口，没有任意函数、文件读写或案件切换能力。这不等于已经验证真实模型能抵抗所有提示注入。

## 验收与局限

```powershell
& backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p 'test_investigation_agent.py' -v
& backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p 'test_llm_tool_calls.py' -v
```

使用案件 A 当前 Gold/固定 TXT，在临时目录和 Mock 模型/MockTransport/假 Langfuse 后端下验证。包括直接证据核对、先关系后证据、空检索后换词再核对、预算与重复终止、错误恢复、引用拒绝和案件字节不变。无真实付费模型调用，不改 Gold 或历史记录。

尚无真实模型效果或各 provider 部署兼容性结论。阶段 1 无分页，单节点引用超出预算仍无法遍历；运行时不判断后续收窄查询是否已经覆盖此前截断内容，因此完成状态偏保守。最终发现的事实真实性、语义支持程度和调查充分性不能仅靠引用 ID 校验保证。

## 统一前端入口（MVP）

点击画布左下角“Agent 调查”，或右键菜单首项“向 Agent 提问”，打开同一个灵动岛，输入调查目标发送。运行中禁用重复提交；完成后展示发现、相关节点、来源原文、未解决问题与任务状态。证据通过 API 按最终合法指针补全文件名、原文及 Unicode `[start,end)` 偏移；这部分展示适配不进入模型请求。点击依据节点定位画布，原文链接跳转结果中的具体证据。

顶部标签栏将 `core_issue` 与 `issue_set` 去重展示，突出核心争点；长标题通过悬停查看全文，标签过多时横向滚动，左右方向键切换。右侧状态由用户选择待调查、调查中或已结束，已结束可重新打开；Agent 不修改状态。

案件目录的 `issue_workspaces.json` 独立于 `case.json`，保存当前 Issue、各 Issue 的状态与画布（可见节点 ID、坐标、视口、选择/聚焦、显示层级）。使用既有 `WorkspaceState` 字段校验和原子 JSON 写入，不修改业务 Schema。Issue 问题全文作为键，当前不支持重命名或层级。`GET/PATCH /api/cases/{case_id}/issue-workspaces` 只管理界面工作状态；所有 InfoUnits、Relations、SourceRefs 仍是全案共享数据。

首次进入按 `related_issue` 推荐可见节点；后续只恢复保存的集合，不因推荐或内容编辑重新布局。右键“添加已有节点”可搜索全案标题/内容，将节点加入当前画布；“从当前画布移除”仅隐藏节点，保留其坐标，不删除业务信息。来源定位可加入尚未显示的节点。只渲染可见端点间的关系。

旧 `workspace_state` 不被改写：核心 Issue 初始化时继承旧坐标、显示层级和视口，但可见节点仍按议题推荐；核心 Issue 菜单“恢复旧画布节点”可重新显示原画布信息，已有 Issue 手动位置优先。没有 Issue 的案件继续使用原画布与首次提取入口。损坏的工作区文件返回明确错误，不覆盖旧数据。保存失败会显示提示及重试按钮；旧画布未成功保存时暂不切换 Issue。

Agent 请求增加可选 `current_issue`，后端验证其属于当前案件，并从存储构造 `work_context`：当前 Issue、Issue 列表与人工状态。问题与选中 ID 沿用原字段。初始模型输入保留核心争点、信息类型数量和最多 5 个选中节点摘要，额外最多 30 条 Issue 元数据（单条标题最多 500 字，包含当前 Issue，显式返回总数与是否省略）；不包含画布节点集合、布局、历史、原始文件或全案正文。工具照常按需检索全案。前端等待排队保存完成后再开展调查，避免把旧状态传给模型。

右键节点时，若节点未选中则将其作为单选上下文；若已选中则保留多选并聚焦该节点。右键空白处保留现有选择。菜单包含适应全部节点、开始提取、新建 InfoUnit、新建关系及适用的编辑命令；右键持久化关系可进入关系编辑。点击外部、Escape、拖动或缩放会关闭菜单；菜单和标签区的滚轮不驱动画布。空案件中央保留提取入口，无可解析材料时提示先上传并禁用提取。

调查结果只保留在当前前端会话，切换 Issue 或案件会清空结果并丢弃旧请求的迟到响应；不写入 CaseData。直接编辑案件 InfoUnit/Relation、自动操作画布或布局等请求仍返回 unsupported；持续推理目标则可返回候选建议，经用户确认后由独立接口写入 Issue 工作区。详见 [Issue 协作推理](issue_reasoning.md)。该语义判断仍有模型误判风险，尚未做真实模型验收；运行时始终只暴露三个只读工具。

前端不再调用 InfoUnit Agent 提案/确认接口，但后端函数、接口与提案能力完整保留，手动 InfoUnit/Relation CRUD 与画布交互继续工作。

浏览器端到端离线验收（复用已有 Playwright，不安装为应用依赖）：

1. 运行 `backend/.venv/Scripts/python.exe backend/tests/serve_agent_smoke.py`，启动仅监听本机 8018 的隔离案件 A 后端，模型与追踪外发关闭，CRUD 与调查循环使用真实代码。
2. 在 frontend 目录设置进程环境 `VITE_API_BASE=http://127.0.0.1:8018`，运行 `node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5178 --strictPort`。
3. 在项目根目录设置 `PLAYWRIGHT_MODULE` 为本机已有 Playwright 或 playwright-core 模块路径，运行 `node frontend/tests/agent-smoke.cjs`。默认使用已安装的 `@playwright/test`，以可见浏览器测试。

脚本覆盖前端提交到真实循环的两条不同路径、原文与偏移展示、节点定位、运行/失败/不支持状态、案件数据不变、手动节点保存、关系编辑器、画布适应和跨案件迟到响应隔离。测试仅修改临时案件，结束后停止两个本地测试进程。API 测试另外通过 MockTransport 检查实际 HTTP 模型请求体，初始请求不含全案正文。

在相同离线服务与 Playwright 环境下，`node frontend/tests/canvas-navigation-smoke.cjs` 验证 Issue 标签切换不重载案件/不修改业务数据、空白与节点菜单、Agent 请求上下文、多选、编辑与关系操作、拖动缩放、空案件提取入口。生成接口在测试中被拦截，不触发真实模型。

### 调查结果展示

灵动岛默认展示第一条发现、重要限制、与该发现直接关联的一段原文及一个待核实问题。其他发现、来源、节点关联和问题按需展开；不在界面直接显示运行 ID、文件 ID、节点 ID 或工具诊断。答案沿用 Agent 返回的发现，不由展示层补充日期或事实。

来源仅按 `(file_id, start, end, quote)` 完全一致合并展示；同名文件、不同区间、内容不同的原文不会合并。全部证据指针与节点关联保留在原结果中，展示组可定位每个关联节点。原文保持原样；重复引用不计为独立佐证。没有与主要发现直接关联的原文时，不将其他发现的来源默认放在其下作为依据。

运行 `node --experimental-strip-types --test frontend/tests/investigation-presentation.test.mjs` 验证展示转换；在上述离线测试服务启动后，以相同 Playwright 环境运行 `node frontend/tests/investigation-result-smoke.cjs` 验证“替租人什么时候搬进去的？”的首屏与展开界面。重复引用在浏览器响应 fixture 中构造，取自案件 A 原文，不改 Gold、缓存或模型输出记录。


`node frontend/tests/issue-workspaces-smoke.cjs` 使用相同可见浏览器与隔离 Mock 后端，验证初始相关节点、添加共享节点、拖动缩放、A/B 切换与刷新恢复、状态重新打开、跨 Issue 共享编辑、Agent 当前议题/选中上下文、跨议题证据定位、可见端点连线及旧布局保留。`backend/tests/test_issue_workspaces.py` 另检查独立存储、非法输入/损坏文件保护及实际 Mock HTTP 模型请求体。

MVP 使用单客户端按顺序保存；不提供跨浏览器并发编辑冲突合并。调查结果与对话历史不写入 Issue 工作区，重新进入时通过现有 Agent 继续调查。
