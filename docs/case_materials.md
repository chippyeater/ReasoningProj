# 案件材料包

Case 侧栏的「案件材料」打开独立 HTML 档案阅读器。支持六章导航、Issue 分组、证据折叠、来源侧栏、重新生成及移动端阅读。返回工作台保留原有编辑现场。

## 数据路径与语义边界

`GET /api/cases/{case_id}/materials` 每次在既有 Workspace 锁内读取 `case.json` 的最新有效快照和该 Case 的 `issue_workspaces.json`，避免并发编辑混用不同时刻的业务数据。通过既有 `get_issue_workspaces` 投影当前全部 `core_issue / issue_set`，不按 `active_issue`、画布可见节点或当前选择裁剪。

- 案件概况：`case_title`、人物/组织 InfoUnits、事件/状态陈述、立场 InfoUnits、核心 Issue、各 Issue 状态与命题/Gap 数量。
- 事实时间线：事件类 InfoUnits；时间取已保存 `TimelineView.events.related_info_unit_ids` 对应的 `time_label / sort_date`。没有已保存时间时显示「事件时间未记录」，不使用 `InfoUnit.created_at` 作为发生时间。
- 争议与证据：每个 Issue 的 `reasoning.hypotheses`、原样保留的 `judgment / work_status`，及 `EvidenceLink.hypothesis_id → info_unit_id` 的支持/反对/相关关系与理由。
- 证据目录：全部 `CaseFile`，以 `file_id` 去重；`InfoUnit.source_refs[].file_id → CaseFile.file_id` 是唯一原始文件关联路径。同名不同 ID 的文件保留；无关联 InfoUnit 的上传文件仍列出。相关 Issue 来自 InfoUnit 的 `related_issue`、画布成员、EvidenceLink 和 Gap 的真实引用，不推测跨 Issue 关系。
- 待补充事项：全部 Issue 中 `status == open` 的 Gap，保留其 `hypothesis_ids`、形成依据及回应材料；已解决的 Gap 只计数。回应材料不等于 Gap 已解决。
- 协商准备：分开呈现 verified 事件、有效的 verified 命题、其余有效命题、待核实陈述、各方立场、communication 来源线索及未解决 Gap。停用命题在争议章节保留，但不作为当前协商结论。

现有模型没有独立的用户身份、用户诉求、当事人角色或背景摘要字段。阅读器明确说明限制，保留已有内容与陈述者，不自动认定某一方立场属于用户诉求。人物记录不自动认定为案件当事人，communication 来源也不自动认定为协商记录。

`event/verified` 仅复述已有核实标记。`event/documented` 仍标记待核实，reported/state/position 保留来源陈述性质。EvidenceLink、文件引用或 Hypothesis judgment 不自动升级 InfoUnit 的真实性。没有关联文件的内容标记来源未关联，不用 InfoUnit ID 替代文件 ID。损坏的 Case/Workspace 沿用既有接口错误，不能用空报告掩盖读取失败。

## 实现与隔离

- 后端 `app/case_materials.py` 直接渲染既有业务对象；`app/templates/case_materials.{css,js}` 提供独立文档所需的样式与固定交互。
- 前端 `CaseMaterialsPage` 通过既有 API 客户端取得 HTML，在全屏阅读页内用 `srcDoc` 浏览；关闭时中止请求。加载和错误状态可见，重新生成失败保留上次档案并明确提示。
- HTML 只保留在当前阅读页内存中，响应 `Cache-Control: no-store`。不新增持久化业务状态，不修改 Workspace/Proposal/Commit，不解析文件、不读调查运行输出、不调用 LLM。
- 所有业务文本及属性值使用 HTML 转义。DOM 目标使用本地生成的索引，与业务 ID 分离。文档 CSP 仅允许固定脚本的 SHA-256 哈希；应用内 iframe 只启用 `allow-scripts`，不允许同源访问。
- 不包含 ZIP、文件复制、DOCX/PDF 导出、来源片段定位、法律程序或诉讼策略生成。

## 验证

```powershell
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p test_case_materials.py -v
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p test_issue_workspaces.py -v
cd frontend
node --experimental-strip-types --test tests/*.test.mjs
# 使用已安装的 Playwright；不新增项目依赖。PLAYWRIGHT_MODULE 可指定模块路径。
node --test tests/case-materials-smoke.mjs
npm run build
```

浏览器测试以可见 Chromium 运行，使用临时后端数据生成真实 HTML，前端 API 使用这些隔离快照，不接触本地案件。截图位于忽略的 `tmp/case-materials-{desktop,mobile}.png`。
