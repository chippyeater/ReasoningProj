# 案件材料包

Case 侧栏的「案件材料」打开独立 HTML 档案阅读器。支持六章导航、Issue 分组、证据折叠、来源侧栏、原件打开、重新生成、ZIP 下载及移动端阅读。返回工作台保留原有编辑现场。

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
- 所有业务文本及属性值使用 HTML 转义。DOM 目标使用本地生成的索引，与业务 ID 分离。文档 CSP 仅允许固定脚本的 SHA-256 哈希；应用内 iframe 启用固定脚本、弹窗及下载权限，不允许同源访问。来源链接使用 `target="_blank" rel="noopener noreferrer"`；弹窗脱离 iframe sandbox，使浏览器可打开或下载原件。
- 不包含 DOCX/PDF 报告生成、来源片段定位、法律程序或诉讼策略生成。

## 原始文件与离线 ZIP

`GET /api/cases/{case_id}/files/{file_id}/original` 校验 ID，只从当前 Case 的 `CaseFile` 查找 `original_path`，经既有 `artifact_path()` 限定在案件目录内。接口不接收文件系统路径，不依赖解析缓存。PDF、常见图片、纯文本及常见音视频使用明确 MIME 类型 inline 打开；Office、HTML/SVG 等主动内容及未知格式作为 attachment 下载。响应加 `nosniff`、文档 sandbox CSP 和安全文件名。

`GET /api/cases/{case_id}/materials.zip` 返回 `case-materials.zip`：

```text
index.html
manifest.json
sources/{file_id}/{安全文件名}
```

导出在既有 Workspace 锁内重新读取 Case 和全部 Issue Workspace，并在锁内读取、打包全部原件，包括未关联 InfoUnit、解析失败或不支持解析的文件。任何原件缺失都会以 `original_file_missing` 报错，不返回残缺 ZIP。标准库 `zipfile` 在内存生成响应，不新增服务器档案状态；大体积导出会占用对应内存。

Manifest 记录 Case ID、标题及时间，以及每个文件的 `file_id / original_filename / zip_path / file_type / source_kind / parse_status`。不包含服务器路径、Case/Workspace 原始 JSON、解析缓存、运行日志或 Trace。原始文件名保留在 Manifest；ZIP 文件名转为安全单个路径组件，处理路径分隔符、控制字符、Windows 保留名及长度。文件 ID 只接受有界 ASCII 字母、数字、下划线和连字符；不能构成目录路径，大小写目录冲突会明确拒绝。

预览与导出复用 `render_case_materials(..., source_url=...)`。预览链接指向原件 API；导出链接是经过 URL 编码的 `sources/` 相对路径，正确处理中文、空格、`#`、`%` 等文件名。CSS/JS 继续内嵌，解压后直接打开 `index.html` 即可使用六章交互和来源链接，无需 API 或网络。浏览器不能预览的原件仍需本地相应软件。

## 验证

```powershell
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p test_case_materials.py -v
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p test_case_materials_export.py -v
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p test_issue_workspaces.py -v
cd frontend
node --experimental-strip-types --test tests/*.test.mjs
# 使用已安装的 Playwright；不新增项目依赖。PLAYWRIGHT_MODULE 可指定模块路径。
node --test tests/case-materials-smoke.mjs
npm run build
```

浏览器测试以可见 Chromium 运行，使用临时后端数据生成真实 HTML、原件响应和 ZIP，前端 API 使用这些隔离快照，不接触本地案件。覆盖原件新标签页、Office 下载、ZIP 下载、缺件错误、解压后 `file://` 离线导航及来源打开，并阻断 HTTP 网络请求验证离线独立性。截图和离线解压包位于忽略的 `tmp/`。
