# Investigation Agent Loop（阶段 2）

后端入口：`app.investigation_agent.run_investigation(case, goal, selected_info_unit_ids=None)`，异步返回可 JSON 序列化的数据。传入当前有效 `CaseData`；可选选中节点为 0–5 个案件内唯一 ID。goal 为 1–2000 字符。没有新增 HTTP 路由、前端、布局生成或 Agent 框架。

```python
from app.investigation_agent import run_investigation

result = await run_investigation(case, "双方关于实际损失的说法有哪些依据？", ["U029"])
```

## 决策与预算

- 绑定案件深拷贝，初始模型上下文仅为 goal、核心争点（最多 500 字符）、五类节点数量和选中节点摘要。没有全案节点、关系、文件目录、争点集合、来源目录或案件选择参数。
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
- `completion_status`：`completed / evidence_insufficient / budget_exhausted`，分别表示目标范围内完成、依据不足、预算耗尽。
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
