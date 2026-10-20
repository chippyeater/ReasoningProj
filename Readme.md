# LogicWeaver

当前后端采用根目录《LogicWeaver｜最小 MVP 数据 Schema v0.2.md》作为唯一业务数据契约。
旧格式案件、旧卡片接口及数据转换层已移除。React 前端尚未迁移，当前不能与新后端完整联调。

## 本轮可用能力

- multipart 上传与追加文件，直接持久化 v0.2 `CaseData`。
- DOCX、PDF、UTF-8 TXT / Markdown 解析；图片和其他格式明确为 `unsupported`，不执行 OCR。
- 原文件保存在 `backend/data/{case_id}/upload_files/`，完整正文保存在 `parsed/{file_id}.txt`。
- `original_path`、`parsed_text_path` 均为案件目录下的相对路径。`case.json` 不复制完整正文。
- 提取输入仅从缓存 TXT 构建；缓存缺失、空白或编码错误均返回错误，不重新解析原文件。
- `InfoUnit`、争点、来源切片、直接关系和视图引用校验；完整定义全部 v0.2 模型。
- 解析日志使用 `StageRun`，输入输出快照保存在案件目录的 `runs/` 下。

DOCX 规范化按文档顺序读取非空段落和表格，去除段落边缘空白，以 LF 分隔段落，表格单元格以 Tab 分隔。
这与案件 A 固定 TXT 的规范化一致。TXT / Markdown 仅移除 UTF-8 BOM 并规范化换行；不截断正文。
PDF 对有可提取文本的页面加入 `[Page N]` 标记。空文件、空白 PDF、损坏文档和非 UTF-8 文本记录为 `failed`。
解析失败的原文件仍会保存，`parsed_text_path=null`，并有明确错误；上传成功不代表每份文件解析成功。
文件读取或磁盘写入失败则中止请求，回滚本次新建文件，保留原案件快照。

## 启动与验证

PowerShell，从仓库根目录执行：

```powershell
& .\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
Set-Location backend
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

本轮只验证 v0.2，不运行历史模型基线：

```powershell
& .\backend\.venv\Scripts\python.exe -m unittest discover -s backend/tests -p 'test_v02_*.py'
& .\backend\.venv\Scripts\python.exe evals/case_a/tests/check_docx_parsing.py
& .\backend\.venv\Scripts\python.exe evals/case_a/tests/validate_eval.py
& .\backend\.venv\Scripts\python.exe -m unittest discover -s evals/case_a/tests -p 'test_*.py'
```

## HTTP 接口

服务入口：`backend/app/main.py`，默认地址 `http://localhost:8000`，文档 `/docs`。
业务模型在 `schemas.py`，HTTP 响应封装在 `api_models.py`。

| 接口 | 输入 / 输出 |
| --- | --- |
| `GET /health` | 服务状态及 Schema 版本 |
| `POST /api/cases` | multipart：必填 `case_title`，一个或多个 `files`；201 `{case: CaseData}` |
| `POST /api/cases/{case_id}/files` | multipart：一个或多个 `files`；200 `{case: CaseData}` |
| `GET /api/cases` | `{cases: CaseData[], current_case_id}` |
| `GET /api/case` | `{case: CaseData \| null}` |
| `GET /api/cases/{case_id}` | `{case: CaseData}` |
| `POST /api/cases/{case_id}/select` | 设置当前案件 |
| `PATCH /api/cases/{case_id}` | `{case_title}` |
| `PATCH /api/cases/{case_id}/workspace` | 完整 v0.2 `WorkspaceState` |
| `DELETE /api/cases/{case_id}` | 删除指定案件及其文件 |
| `DELETE /api/case` | 删除当前案件；不自动切换其他案件 |
| `GET /api/cases/{case_id}/files/{file_id}/text` | `ParsedDocument`，读取已缓存正文 |
| `GET /api/cases/{case_id}/extraction-input` | `ExtractionInput`；可重复传 `?file_id=...` 选择文件 |

未指定文件时构建全部文件的提取输入；存在失败或不支持文件时明确报错。
要继续处理可用文件，应显式选择已解析的文件 ID，不会静默略过失败项。
错误使用 HTTP 4xx / 5xx；业务错误体为 `{detail: {code, message}}`。
旧格式快照返回 409；文件格式不支持通过 `CaseFile.parse_status` 表达；不接受 JSON 上传或全量旧案件 upsert。
旧卡片编辑、`/api/inference/generate`、router、QA 和 interaction 接口已移除，返回 404。

## 来源与模型执行边界

`validation.locate_source_ref` 从缓存 TXT 计算 Unicode 字符位置 `[start,end)`。
找不到引用或存在多个匹配（含重叠匹配）均报错；已有精确位置的引用必须逐字匹配切片。
`validate_extraction` 校验结果为 `ValidationResult[ExtractionOutput]`，不静默丢弃错误条目。
任何条目校验失败时返回全部已发现来源错误，`valid=false`、`data=null`。

`CaseFile` 的目标契约没有 `source_kind` 字段，不能仅凭扩展名确认材料是文档、沟通记录、笔录或交易记录。
校验器要求调用方传入独立确认的文件来源类型映射；缺失或与引用不符时拒绝结果。
本轮不扩充 `CaseFile` 字段，也不从模型自己的输出反向构造验证依据。
案件保存会再次检查结构、文件、缓存及引文切片；来源类型的独立核验由抽取接入边界负责。

以下新接口已保留入口和错误行为，当前执行均返回 **501 `pipeline_not_implemented`**：

| 接口 | 下一阶段目标 |
| --- | --- |
| `POST /api/extraction/run` | `{case_id, file_ids?}` → 读取 `ExtractionInput`、抽取、后端生成 ID / 时间并定位来源、校验后入库 |
| `POST /api/relation/run` | `{case_id}` → `RelationInput` / `RelationOutput`，不再生成重复连线 |
| `POST /api/reasoning/run` | `ReasoningInput` → 选择视图、生成 `ViewPayload`、保存 `ReasoningView` |
| `POST /api/law/retrieve` | `LawRetrieveInput` → `LawRetrieveOutput`，引用已核验 `LawEntry` |

没有 Mock、业务兜底、旧格式解析或 LLM 隐式回退。
`RunStatus.fallback` 仅因 v0.2 契约明确定义而保留枚举值；本轮没有使用它的执行分支。
全部 Prompt 当前均不在运行路径中；仅抽取和关系 Prompt 已同步新字段约束，其余需下一阶段审核。
本地 `backend/data/laws.json` 已直接整理为 `LawEntry[]`，保留条文、条号、标题、ID 与来源。
可从有效 Gold 确认的核验日期原样保留；其他条目的核验日期为 null，不虚构日期。
React 的类型、状态和三种视图渲染需下一阶段统一改用 `InfoUnit`、`Relation`、`ReasoningView`。
