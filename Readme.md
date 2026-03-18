# Reasoning Interface Generator

这是一个最小可运行 demo，用来验证这条链路：

1. 用户先提交案件介绍和证据
2. 后端解析证据并执行阶段 1 抽取，生成案件结构化数据
3. 后端把“当前案件”保存到本地
4. 用户再单独提交问题
5. 后端基于当前案件执行阶段 2+3，生成推理结构和界面推荐
6. 前端根据 `recommended_view` 渲染对应视图

当前实现特点：

- 前端：React + Vite + TypeScript
- 后端：Python + FastAPI
- LLM：OpenAI 兼容写法接 GitHub Models
- 存储：单案件本地 JSON 文件，不使用数据库
- 输入：支持手工证据和真实文件上传
- Prompt：系统 prompt 单独存放在 `backend/prompts/*.md`

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
      case_store.py
      log_store.py
      evidence_tools.py
      file_parsers.py
    data/
      current_case.json
    logs/
    prompts/
      extraction_system_prompt.md
      question_reasoning_system_prompt.md
  openclaw-integration/
    reasoningTool.ts
  Readme.md
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
GITHUB_TOKEN="your_github_pat"
GITHUB_ENDPOINT="https://models.github.ai/inference"
GITHUB_MODEL_ID="openai/gpt-4o-mini"
```

也兼容以下环境变量：

```bash
OPENAI_BASE_URL="https://models.github.ai/inference"
OPENAI_MODEL="openai/gpt-4.1-mini"
OPENAI_API_KEY="your_key"
```

## 当前 Pipeline

### 阶段 1：案件结构化

输入：

- `case_text`
- 用户提交的手工证据
- 上传文件解析后的证据

输出：

- `parsed_evidences`
- `evidence_items`
- `entities`
- `relations`
- `events`
- `claims`

说明：

- `evidence_items` 是系统内部生成对象，不是 LLM 输出字段
- 阶段 1 结果会保存为“当前案件”

### 阶段 2+3：问题推理

输入：

- 当前案件
- 用户问题 `question`

输出：

- `conflicts`
- `evidence_paths`
- `recommended_view`
- `summary`

说明：

- 阶段 2 和阶段 3 当前合并在一次 LLM 调用里执行
- `recommended_view` 用于驱动前端视图切换

## 当前数据结构

当前代码里已经强类型定义的主要对象在 [schemas.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/schemas.py)：

- `EvidenceItem`
  - `id`
  - `type`
  - `original_content`
  - `source_file`
  - `page_or_paragraph`
  - `time`
  - `producer_or_speaker`
  - `is_original_evidence`
  - `notes`
- `Entity`
  - `id`
  - `name`
  - `type`
  - `aliases`
  - `source_evidence_ids`
- `Relation`
  - `id`
  - `subject_entity`
  - `object_entity`
  - `relation_type`
  - `time`
  - `evidence_sources`
  - `confidence_status`
- `Event`
  - `id`
  - `event_type`
  - `participant_entities`
  - `time`
  - `location`
  - `description`
  - `source_evidence_ids`
- `Claim`
  - `id`
  - `content`
  - `source`
  - `target_ids`
  - `stance`
  - `credibility_status`
  - `quote`

当前仍未独立建模为强类型 schema 的部分：

- `Conflict`
- `Hypothesis`
- `ProvenanceLink`

现在它们还没有完整落到后端 schema 中，尤其 `conflicts` 和 `evidence_paths` 仍然是 `list[dict]`。

## Prompt 文件

系统 prompt 已经独立到 Markdown 文件：

- [extraction_system_prompt.md](/d:/VSCode/VSProj/ReasoningProj/backend/prompts/extraction_system_prompt.md)
- [question_reasoning_system_prompt.md](/d:/VSCode/VSProj/ReasoningProj/backend/prompts/question_reasoning_system_prompt.md)

[llm.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/llm.py) 会在运行时优先读取这两个 `.md` 文件；如果文件不存在或读取失败，则回退到代码内置默认 prompt。

阶段 1 prompt 当前已经强化为：

- 顶层字段固定
- 每类对象字段名固定
- 明确禁止错误别名
- 缺失值使用默认空字符串、空数组或枚举默认值

## 接口说明

### 1. 创建或替换当前案件

`POST /api/case`

支持两种请求方式：

- `application/json`
- `multipart/form-data`

没有文件时可直接发 JSON：

```json
{
  "case_text": "...",
  "evidences": []
}
```

有文件时发送 `multipart/form-data`：

- `case_text`：案件介绍
- `manual_evidences`：手工证据数组的 JSON 字符串
- `files`：多个上传文件

返回：

```json
{
  "case": {
    "case_id": "case-xxxxxxx",
    "case_text": "...",
    "parsed_evidences": [],
    "evidence_items": [],
    "entities": [],
    "relations": [],
    "events": [],
    "claims": [],
    "extraction_log": {
      "provider": "github_models",
      "model": "openai/gpt-4o-mini",
      "endpoint": "https://models.github.ai/inference",
      "pipeline_llm_used": true,
      "fallback_reason": "",
      "stages": []
    }
  }
}
```

### 2. 读取当前案件

`GET /api/case`

返回：

```json
{
  "case": null
}
```

或：

```json
{
  "case": {
    "case_id": "case-xxxxxxx",
    "case_text": "...",
    "parsed_evidences": [],
    "evidence_items": [],
    "entities": [],
    "relations": [],
    "events": [],
    "claims": [],
    "extraction_log": {
      "provider": "github_models",
      "model": "openai/gpt-4o-mini",
      "endpoint": "https://models.github.ai/inference",
      "pipeline_llm_used": true,
      "fallback_reason": "",
      "stages": []
    }
  }
}
```

说明：

- 页面刷新后，前端会自动调用这个接口恢复当前案件
- 恢复的是后端保存的案件快照，不是浏览器文件输入框状态

### 3. 针对当前案件提问

`POST /api/case/question`

请求体：

```json
{
  "question": "..."
}
```

返回：

```json
{
  "case_id": "case-xxxxxxx",
  "question": "...",
  "conflicts": [],
  "evidence_paths": [],
  "recommended_view": "conflict_compare",
  "summary": "...",
  "reasoning_log": {
    "provider": "github_models",
    "model": "openai/gpt-4o-mini",
    "endpoint": "https://models.github.ai/inference",
    "pipeline_llm_used": true,
    "fallback_reason": "",
    "stages": []
  }
}
```

### 4. 清空当前案件

`DELETE /api/case`

返回：

```json
{
  "status": "cleared"
}
```

## 日志与存储

### 当前案件存储

当前案件保存在：

- [current_case.json](/d:/VSCode/VSProj/ReasoningProj/backend/data/current_case.json)

用途：

- 页面刷新后恢复当前案件
- 后续提问时复用结构化结果
- 调试阶段 1 抽取结果

### 请求日志

每次创建案件和每次提问都会把完整响应写入：

- `backend/logs/case-create-*.json`
- `backend/logs/case-question-*.json`

日志里会记录：

- `prompt_system`
- `prompt_user`
- `raw_response`
- `raw_content`
- `usage`
- `limits`
- `fallback_reason`
- `error`

## 前端行为

当前前端页面是“两步式”：

1. 左侧提交案件介绍和证据
2. 右侧顶部输入问题
3. 右侧下方展示结构化结果、推理结果和日志

左侧“当前案件证据”会显示每条证据的状态：

- `pending`
- `submitted`
- `success`

页面初始化时会自动请求 `GET /api/case` 恢复：

- `EvidenceItem`
- `Entity`
- `Relation`
- `Event`
- `Claim`

但不会恢复浏览器本地 `File` 对象本身。

## 核心文件职责

[frontend/src/App.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/App.tsx)

- 页面主状态管理
- 提交案件
- 提交问题
- 渲染日志和结果区

[frontend/src/api.ts](/d:/VSCode/VSProj/ReasoningProj/frontend/src/api.ts)

- 封装 `POST /api/case`
- 封装 `GET /api/case`
- 封装 `DELETE /api/case`
- 封装 `POST /api/case/question`

[frontend/src/components/ConflictCompare.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/components/ConflictCompare.tsx)

- 冲突对比视图

[frontend/src/components/TimelineReasoning.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/components/TimelineReasoning.tsx)

- 时间线推理视图

[frontend/src/components/HypothesisBoard.tsx](/d:/VSCode/VSProj/ReasoningProj/frontend/src/components/HypothesisBoard.tsx)

- 假设看板视图

[backend/app/main.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/main.py)

- FastAPI 接口入口
- 处理 `json` 与 `multipart/form-data`

[backend/app/schemas.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/schemas.py)

- 定义当前案件、阶段响应、阶段日志等 schema

[backend/app/llm.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/llm.py)

- 加载系统 prompt
- 调用 LLM
- 执行阶段 1 和阶段 2+3

[backend/app/case_store.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/case_store.py)

- 保存当前案件
- 读取当前案件
- 清空当前案件

[backend/app/log_store.py](/d:/VSCode/VSProj/ReasoningProj/backend/app/log_store.py)

- 保存接口返回日志到 `backend/logs/`

## 已知限制

- 当前只支持单案件，不支持多用户和多案件并发
- 当前未引入数据库
- `Conflict`、`Hypothesis`、`ProvenanceLink` 还没有单独 schema 化
- 阶段 1 虽然已强化 prompt，但模型仍可能偶发返回不完全符合 schema 的 JSON
- 如果模型仍然漂字段，下一步建议在后端增加归一化适配层
