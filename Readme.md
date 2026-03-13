# Reasoning Interface Generator

这是一个最小可运行 demo，用来验证这条链路：

用户输入案件材料 + 推理问题 + 手工证据/上传文件 -> 后端解析证据 -> 后端调用 LLM 输出结构化推理 JSON -> 前端根据 `recommended_view` 自动渲染推理界面。

目前项目支持：

- 前端 React + Vite + TypeScript
- 后端 Python + FastAPI
- LLM 通过 OpenAI 兼容写法接 GitHub Models
- 无数据库
- 支持手工证据和真实文件上传

## 目录结构

```text
project-root/
  frontend/
    src/
      components/
        ConflictCompare.tsx
        TimelineReasoning.tsx
        HypothesisBoard.tsx
      App.tsx
      api.ts
  backend/
    app/
      main.py
      schemas.py
      llm.py
      mock_data.py
      evidence_tools.py
      file_parsers.py
  openclaw-integration/
    reasoningTool.ts
  README.md
```

## 运行方式

### 1. 启动后端

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

健康检查：

```bash
curl http://localhost:8000/health
```

### 2. 启动前端

```bash
cd frontend
npm install
npm run dev
```

浏览器打开：

```text
http://localhost:5173
```

## .env 配置

后端会自动读取项目根目录 `.env`。

示例：

```bash
GITHUB_TOKEN=your_github_pat
GITHUB_ENDPOINT=https://models.inference.ai.azure.com
GITHUB_MODEL_ID=gpt-4o-mini
GITHUB_API_VERSION=2022-11-28
```

如果没有配置 token，后端会自动回退到 mock 数据。

兼容的备用环境变量名也支持：

```bash
OPENAI_BASE_URL=https://models.github.ai/inference
OPENAI_MODEL=openai/gpt-4.1-mini
OPENAI_API_KEY=your_key
```

## 接口说明

### 1. JSON 接口

`POST /api/reason`

请求体：

```json
{
  "case_text": "...",
  "question": "...",
  "evidences": []
}
```

### 2. 文件上传接口

`POST /api/reason-upload`

使用 `multipart/form-data`：

- `case_text`: 案件材料
- `question`: 推理问题
- `manual_evidences`: 手工证据数组的 JSON 字符串
- `files`: 多个上传文件

### 3. 返回结构

```json
{
  "parsed_evidences": [],
  "entities": [],
  "events": [],
  "claims": [],
  "conflicts": [],
  "evidence_paths": [],
  "recommended_view": "conflict_compare",
  "summary": "..."
}
```

其中：

- `parsed_evidences`：后端已经解析并标准化后的证据
- `recommended_view`：前端据此决定渲染哪种推理视图

## 支持的证据类型

统一证据结构定义在 `backend/app/schemas.py` 的 `EvidenceInput`。

支持类型：

- `text`：纯文本证据
- `document`：文档证据
- `image`：图片证据
- `video`：视频证据
- `audio`：音频证据

当前真实文件上传已实现的解析类型：

- `txt / md`：直接读取文本
- `pdf`：通过 `pypdf` 提取文本
- `docx`：通过 `python-docx` 提取文本
- `image`：通过 `Pillow` 提取图片元信息

当前还没有实现：

- 图片 OCR
- 音频转写
- 视频转写 / 抽帧分析

所以图片目前通常会显示为“部分解析”。

## 函数调用链路

这一节按“用户点击提交后，代码实际怎么走”来写。

### A. 前端发起上传和推理

1. 用户在 [App.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/App.tsx) 中：
   - 输入 `caseText`
   - 输入 `question`
   - 添加手工证据
   - 选择上传文件
2. 文件选择触发 `onFileChange()`
   - 把浏览器中的 `File[]` 存进 `uploadedFiles`
3. 点击“提交推理”触发 `onSubmit()`
4. `onSubmit()` 调用 [api.ts](/d:/VSCode/VSProj/ReasoningProj/frontend/src/api.ts) 里的 `postReasonUpload()`
5. `postReasonUpload()`：
   - 创建 `FormData`
   - 写入 `case_text`
   - 写入 `question`
   - 写入 `manual_evidences`
   - 逐个追加 `files`
   - 请求 `POST /api/reason-upload`

### B. 后端接收 multipart 请求

1. 请求进入 [main.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/main.py) 的 `reason_upload()`
2. `reason_upload()` 先调用 `_parse_manual_evidences()`
   - 把前端传来的 `manual_evidences` JSON 字符串转成 `EvidenceInput[]`
3. 然后调用 [file_parsers.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/file_parsers.py) 的 `parse_uploaded_files()`
4. `parse_uploaded_files()` 遍历每个文件并调用 `parse_uploaded_file()`
5. `parse_uploaded_file()` 根据文件后缀分发到：
   - `_parse_txt_file()`
   - `_parse_pdf_file()`
   - `_parse_docx_file()`
   - `_parse_image_file()`
6. 文件被解析成统一的 `EvidenceInput`
7. `reason_upload()` 把：
   - 手工证据 `manual_items`
   - 文件解析证据 `uploaded_items`
   合并后传给 `run_reasoning()`

### C. 后端构造 LLM 上下文并调用模型

1. 调用 [llm.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/llm.py) 的 `run_reasoning(case_text, question, evidences)`
2. `run_reasoning()` 先调用 `_load_local_env()`
   - 加载项目根目录 `.env`
3. 然后调用 `_format_evidence_context(evidences)`
4. `_format_evidence_context()` 再调用 [evidence_tools.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/evidence_tools.py) 的 `build_evidence_context()`
5. `build_evidence_context()` 逐条调用 `parse_evidence()`
6. `parse_evidence()` 根据证据类型分发到：
   - `parse_text_evidence()`
   - `parse_document_evidence()`
   - `parse_image_evidence()`
   - `parse_video_evidence()`
   - `parse_audio_evidence()`
7. 这些函数返回 `ParsedEvidence`
   - 包含 `normalized_text`
   - 包含 `metadata.parse_status`
   - 包含 `metadata.parser_detail`
8. `_format_evidence_context()` 把这些解析结果拼成发送给 LLM 的证据上下文文本
9. `run_reasoning()` 再构造 OpenAI 兼容请求体：
   - `model`
   - `messages`
   - `temperature`
10. 调用 GitHub Models 的 `/chat/completions`
11. 如果模型失败或没配 token：
   - 回退到 [mock_data.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/mock_data.py) 的 `get_mock_reasoning()`

### D. 前端接收结果并渲染

1. 前端拿到 `ReasonResponse`
2. [App.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/App.tsx) 把结果存入 `result`
3. 右侧结果区展示：
   - `summary`
   - `parsed_evidences`
   - `entities`
4. 然后按 `recommended_view` 选择组件：
   - `conflict_compare` -> [ConflictCompare.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/components/ConflictCompare.tsx)
   - `timeline_reasoning` -> [TimelineReasoning.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/components/TimelineReasoning.tsx)
   - `hypothesis_board` -> [HypothesisBoard.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/components/HypothesisBoard.tsx)

## 各文件主要功能

### 前端

[App.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/App.tsx)
- 页面主入口
- 管理案件材料、问题、手工证据、上传文件、请求状态
- 提交推理请求
- 展示解析结果和推理结果

[api.ts](/d:/VSCode/VSProj/ReasoningProj/frontend/src/api.ts)
- 定义前端用到的请求/响应类型
- 封装 `/api/reason`
- 封装 `/api/reason-upload`

[ConflictCompare.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/components/ConflictCompare.tsx)
- 渲染冲突对比视图

[TimelineReasoning.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/components/TimelineReasoning.tsx)
- 渲染时间线推理视图

[HypothesisBoard.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/components/HypothesisBoard.tsx)
- 渲染假设面板视图

[styles.css](/d:/VSCode/VSProj/ReasoningProj/frontend/src/styles.css)
- 定义页面布局、证据卡片、状态徽标等样式

### 后端

[main.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/main.py)
- FastAPI 入口
- 暴露 `/health`
- 暴露 `/api/reason`
- 暴露 `/api/reason-upload`
- 负责把请求参数交给推理层

[schemas.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/schemas.py)
- 定义 `EvidenceInput`
- 定义 `ParsedEvidence`
- 定义 `ReasonRequest`
- 定义 `ReasonResponse`

[file_parsers.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/file_parsers.py)
- 负责解析真实上传文件
- 将 `txt/pdf/docx/image` 转成统一 `EvidenceInput`

[evidence_tools.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/evidence_tools.py)
- 负责把 `EvidenceInput` 标准化为 `ParsedEvidence`
- 给每条证据打解析状态和说明
- 供 LLM 构造证据上下文使用

[llm.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/llm.py)
- 加载 `.env`
- 构造 GitHub Models 的 OpenAI 兼容请求
- 调用模型
- 解析模型返回 JSON
- 出错时回退 mock

[mock_data.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/mock_data.py)
- 提供最小演示用的固定推理结果

### OpenClaw

[reasoningTool.ts](/d:/VSCode/VSProj/ReasoningProj/openclaw-integration/reasoningTool.ts)
- 给 OpenClaw agent 调用后端推理接口用

## 快速测试

1. 启动后端 `:8000`
2. 启动前端 `:5173`
3. 录入案件材料与问题
4. 选择一个 `txt/pdf/docx/image` 文件
5. 点击“提交推理”
6. 在右侧“解析结果”查看每条证据的：
   - 解析状态
   - 解析工具
   - 文件名
   - 解析说明
   - 标准化文本

## 当前限制

- 图片只解析元信息，未做 OCR
- 不支持 `doc`
- 不支持音频/视频真实转写
- 若 GitHub Models 不可用，会自动回退 mock 数据
