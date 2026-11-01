# Issue 协作推理 MVP

实现依据为用户任务 `tasks/10231658.md`。可勾选验收清单只保留本地 `tasks/10231658-checklist.md`，不纳入 Git。

## 使用闭环

在 Issue 画布右键“新建假设”，保存内容、证据判断（未证实/已证实/已证伪）与工作状态（活跃/已弃用）。弃用只降低视觉优先级，可以恢复。节点“查看与编辑”或右键进入编辑器。

从假设编辑器或右键“关联已有证据”，选择全案 InfoUnit，填写支持/反对/待核实语义与理由。支持为绿色实线，反对为红色虚线，待核实为灰色虚线；点击连线可修改或解除。来源原文在关联编辑器按需展开，可定位原信息。人工/Agent 建议已采纳的来源独立于关联语义，用户修改保留来源并标明人工修正。

“新建证据缺口”或假设中的“添加证据缺口”支持关联多个假设、可选缺口形成依据和未解决/已解决状态。“用于回应缺口的信息”单独展示并可定位 InfoUnit，暂不提供手动编辑回应列表的界面。缺口是问题，不是虚构的材料。正式节点复用当前画布选择、拖动、缩放、坐标与 Issue 恢复机制；新节点寻找空位，不重排已有节点。

保存用户修正后可“请 Agent 检查”，从现有灵动岛发送。Agent 继续按需调用同一组三个只读工具，可返回普通调查答案和可选建议；事实查询不强制生成建议。候选以半透明卡片预览，灵动岛可查看依据、采纳或忽略。采纳前不写正式推理数据，忽略只丢弃本次预览。刷新/切换不保留候选或对话历史。

## 数据与接口

沿用 `issue_workspaces.json`，每个 Issue 增加默认空的 `reasoning`：

- `hypotheses`：稳定 ID、内容、判断、工作状态、修订号、最近修改时间/来源、可选采纳凭据。
- `gaps`：稳定 ID、问题、关联假设 IDs、`basis_info_unit_ids`（提出缺口的已有信息）、`addressing_info_unit_ids`（创建后累积的回应材料）、解决状态和修订元数据。两类引用独立，不自动复制；同一 InfoUnit 可经明确操作分别关联两种角色。
- `links`：稳定 ID、InfoUnit 与 Hypothesis IDs、语义、理由、人工/Agent 来源及最近修改者。
- `accepted_proposals`：服务端运行与建议索引到正式对象的幂等映射。

所属 Issue 由工作区父键确定。业务 InfoUnits、Relations、SourceRefs 不复制、不修改；`HypothesisView` 等既有生成式视图不承担这些可编辑工作对象。旧文件缺少 `reasoning` 时读作空集合，旧坐标与字段保留。引用了某个 InfoUnit 的正式推理关联/缺口会阻止删除该共享信息，避免断开引用。

`PATCH /api/cases/{case_id}/issue-reasoning` 每次接受一个人工操作：`hypothesis`、`gap`、`link` 或 `unlink_id`，绑定 `issue`，可附新节点 `position`。后端分配 ID、修订号和来源，校验所有引用、当前 Issue 归属及重复关联，并通过现有原子 JSON 写入统一保存。Gap 创建时回应列表为空，非空回应只能追加到已有 Gap；编辑时传入 `addressing_info_unit_ids` 与原值按首次出现顺序合并去重，空列表也不会清空旧关联。省略 `basis_info_unit_ids` 或 `status` 时保留原值；显式传入依据列表可替换形成依据，显式传入状态才改变解决状态。追加回应不会改变 Hypothesis 的 judgment。两类引用都校验当前 Case ID 范围，都阻止删除被引用的 InfoUnit，引用类型分别为 `evidence_gap_basis` / `evidence_gap_addressing`。

`POST /api/cases/{case_id}/reasoning-proposals/accept` 使用 `issue/run_id/proposal_id/position`，不接受客户端重新编写建议或证据。旧审计仍可用 proposal_index；两个引用字段必须恰选一个。后端从所属运行的已验证成果读取建议，核对 Issue、材料摘要和目标修订后原子写入正式对象、画布和幂等映射。重复采纳返回已有对象；材料或用户推理改变后旧建议返回冲突。修改关联或缺口同样推进假设修订。

## Agent 边界

新建议只由五个独立 Proposal 工具产生，工具名决定种类；最终回复不携带建议 JSON。成功工具返回随机临时 proposal_id，并立即持久化至本次运行审计。kind 继续用于既有存储和 UI。新假设采纳后仍为未证实、活跃；引用可使用已核验的首轮来源或本轮 get_evidence 返回结果。探索性假设/新缺口允许空材料和空引用，界面明确提示尚无直接来源依据；证据关联、判断调整与已有缺口更新必须有可追溯材料。空列表不取消同轮依赖审批或目标版本检查。

补充证据通过 `propose_evidence_gap_update` 生成 `kind=evidence_gap_update` 建议，包含 `gap_id/addressing_info_unit_ids/status/rationale/evidence_refs`。它不接受新问题或假设关联，采纳时从原 Gap 读取这些字段；审计凭据 `target_gap_revisions` 绑定生成时的目标修订，Gap 或回应材料变化时返回 `stale_reasoning_proposal`。原回应材料与新增 ID 并集合并，部分回应由 Agent 建议保持 open，用户逐项采纳后才保存。重复采纳返回原对象，不重复登记或递增修订；Hypothesis 判断继续由独立工具建议。预览明确显示“更新已有缺口”、原问题、建议状态和回应材料，避免与创建新 Gap 混淆。

hypothesis_ids 保留引用有限上下文中已有假设的方式。Evidence Gap 可填写 hypothesis_proposal_ids，引用同一运行里已经成功返回的 Hypothesis proposal_id；两个列表合计 1–5 个目标。Evidence Link 同样复用该字段，但必须恰有一个正式或临时假设目标、一个 InfoUnit；工具以 hypothesis_id=null 加单项 hypothesis_proposal_ids 指向新假设，已有假设则传正式 ID 和空列表。模型不能自造 ID、引用未来建议、其他运行或其他建议类型。内部调查思路不自动成为 Proposal，也不写工作区。

新幂等键为 `run_id:proposal_id`，从已采纳映射找到真实假设 ID。依赖新假设的缺口或证据关联都必须等用户先采纳父假设，再独立采纳；不连带采纳、不猜测关联。父假设和材料须未过期；同轮其他已采纳缺口/关联引起的已知修订递增允许继续采纳。用户的其他修改仍会使旧建议失效。

同轮对已有假设的独立关联、缺口及已有缺口更新也共享上述规则。服务端 receipt 保存完整目标快照（不进入模型上下文），按 `accepted_proposals` 的实际采纳顺序在独立副本中复用编辑逻辑，核对预期对象和 revision；重复回应的无变化更新不计版本增量。同一缺口的同状态回应可合并，不同状态建议冲突；判断调整仍绑定原始证据状态。用户编辑、其他运行对相关对象的修改，以及当前建议或已采纳关联建议的材料变化，均不能抵扣为本轮版本变化。历史 receipt 没有完整 Gap 快照时不推测回应追加的版本增量，无法验证的旧 Gap 更新需重新调查；历史审计不改写。

Gap Proposal 只接收 `basis_info_unit_ids`，不接收通用 `info_unit_ids` 或回应列表；其他三类 Proposal 继续使用 `info_unit_ids`，不接收 Gap 专属字段。kind 判别的联合契约校验各自字段；形成依据参与已取得材料/SourceRef 校验和采纳前的材料哈希检查，正式 Gap 采纳后回应列表为空。所有建议仍需逐项采纳。

符合当前字段契约的运行若缺少 proposal_id，仍可按原顺序、hypothesis_proposal_indices 和 `run_id:index` 采纳；新工具不接收数组索引。历史审计与 Trace 不改写，含旧 Gap 字段的待采纳建议不由运行时隐式转换，需重新调查。

总结失败时，成功工具提交的合法成果独立保留，审计为 partial，界面明确显示总结失败；用户仍可明确采纳已验证建议。没有合法成果的失败不会显示成功结果，任何正式对象都不会因为工具调用或总结自动创建。

请求可传最多 3 个 `selected_reasoning_ids`。上下文优先显式选择、其关联缺口、目标关键词与最新修改，最多 3 个工作对象候选、3 个相关假设和 5 条关联；内容摘要最多 300 字、理由最多 160 字，显式标记是否仍有省略。Gap 两类 InfoUnit 引用分别最多投影 5 条，各带 `basis_info_unit_ids_truncated` / `addressing_info_unit_ids_truncated`；完整列表仍保存在工作区中。内容、判断、弃用状态和修订号来自最新用户保存值，不注入完整画布/全部推理/历史对话；其中引用的 InfoUnit IDs 不等于已读证据，仍需按需工具读取。Issue 状态仍完全由用户控制。

## 已保存 Gap 的一次性迁移

工作区没有独立 Schema 版本注册；本次不修改 Extraction/CaseData Schema 版本。生产模型只接受拆分后的字段。迁移脚本先验证整份新工作区及当前案件 InfoUnit 引用，保留其他字段、稳定 ID、状态、修订、画布和采纳记录原值；`--apply` 前保存逐字节原文件备份 `issue_workspaces.json.before-gap-reference-split.bak`，通过现有原子写入保存。重复执行不再改写，混合新旧字段或备份冲突明确失败。应在停止该工作区写入时执行，脚本不会迁移历史运行审计。

```powershell
backend/.venv/Scripts/python.exe scripts/migrate_gap_references.py backend/data/<case_id>/issue_workspaces.json
backend/.venv/Scripts/python.exe scripts/migrate_gap_references.py backend/data/<case_id>/issue_workspaces.json --apply
```

旧 Gap 的 `info_unit_ids` 原样转为 `basis_info_unit_ids`，`addressing_info_unit_ids` 初始化为空；无运行时别名或旧字段 fallback。

所有模型输入、工具返回及最终建议沿用现有审计与 Langfuse 机制。错误按 JSON 解析、结构、Proposal 语义及引用分类记录字段路径和错误码；即使追踪不采集内容，也保留不含案件文本的错误位置及类别。调查阶段只写独立运行审计；正式工作区只能由人工编辑或明确采纳接口写入。

## 离线验收

沿用 `docs/investigation_agent.md` 中的隔离后端和 Vite 启动方法。后端：`test_reasoning_workspace.py`、`test_reasoning_proposals.py`、`test_investigation_context.py`、`test_evidence_link_proposals.py`。可见浏览器：`frontend/tests/reasoning-workspace-smoke.cjs`、`frontend/tests/evidence-link-proposals-smoke.cjs`，只模拟模型边界，真实运行 API、循环、审计与持久化。两组均需从新启动的隔离后端开始，避免彼此已采纳的数据污染数量断言；其他浏览器脚本验证已有功能。

画布仅为实际 Evidence Link 建议绘制 InfoUnit 到新候选/已采纳 Hypothesis 的待确认虚线，附关系含义与待确认标识。可独立预览依据、定位材料、采纳或忽略；父假设采纳后仅重连端点，关联仍待确认。隐藏或忽略的端点不虚构连线；未在当前画布的材料可由用户点击定位加入。候选关系不写入工作区；正式关系继续复用手动 CRUD 与持久化。

尚未验证付费真实模型的建议质量。引用可定位不能证明其充分支持判断；采纳是用户决定，不代表独立核实。不提供多人冲突合并、通用审批/撤销历史、自动收集外部证据、生成布局或通用建议依赖图；仅允许上述同轮 Gap/Link 到新 Hypothesis 的引用。新的证据关系需要进一步检验，不能由片段或引用次数推导独立证据数量。
