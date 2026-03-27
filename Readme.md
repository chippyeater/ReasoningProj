# ReasoningProj

这是一个前后端分离的推理工作台项目：左侧负责文件管理与上传，右侧是可拖拽节点画布和右键灵动岛，后端负责文件解析、结构化抽取与案例状态持久化。

## 工程架构

### 1. 前端（`frontend/`）
- 技术栈：React + TypeScript + Vite
- 入口：`frontend/src/main.tsx`
- 主界面：`frontend/src/App.tsx`
- 样式：`frontend/src/styles.css`
- 接口封装：`frontend/src/api.ts`
- 资源：`frontend/src/assets/`

当前界面结构：
- 左侧栏（固定 360 宽）：头像、搜索、文件树、拖拽上传区
- 右侧画布：无限网格背景、实体/事件/主张节点、关系连线
- 交互：画布拖拽平移、滚轮缩放、节点拖拽、右键唤起可拖拽灵动岛（底部吸附）

上传区当前逻辑：
- 拖拽/点击选文件后自动发起上传
- 上传中显示环形进度条 + 文件名列表（中文逗号分隔，过多显示 `...`）
- 上传完成后按钮从 `Cancel` 变为 `Confirm`

### 2. 后端（`backend/`）
- 技术栈：FastAPI + Python
- 入口：`backend/app/main.py`
- 结构化模型：`backend/app/schemas.py`
- LLM 调用与流程：`backend/app/llm.py`
- 文件解析：`backend/app/file_parsers.py`
- 证据工具：`backend/app/evidence_tools.py`
- 当前案例存储：`backend/app/case_store.py`
- 日志落盘：`backend/app/log_store.py`
- Prompt：`backend/prompts/`
  - `extraction_system_prompt.md`
  - `question_reasoning_system_prompt.md`

数据目录：
- 当前案例：`backend/data/current_case.json`
- 调试日志：`backend/logs/*.json`

## 主要接口

- `GET /health`
  - 健康检查
- `GET /api/case`
  - 获取当前案例
- `POST /api/case`
  - 创建/更新当前案例（支持 `multipart/form-data` 文件上传）
- `DELETE /api/case`
  - 清空当前案例

## 运行方式

### 1) 启动后端

```bash
cd backend
python -m venv .venv
# Windows
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### 2) 启动前端

```bash
cd frontend
npm install
npm run dev
```

访问：`http://localhost:5173`

## 环境变量

项目默认从根目录 `.env` 读取模型配置。常见变量：

```bash
GITHUB_TOKEN="<your_token>"
GITHUB_ENDPOINT="https://models.github.ai/inference"
GITHUB_MODEL_ID="openai/gpt-4o-mini"
```

也兼容 OpenAI 风格变量：

```bash
OPENAI_API_KEY="<your_key>"
OPENAI_BASE_URL="<your_base_url>"
OPENAI_MODEL="<model_name>"
```

## 当前状态说明

- 目前是单案例模式（`current_case.json`）
- 文件上传、解析、案例回写已联通
- 前端画布视图与文件树联动已完成
- `openclaw-integration/` 目录保留，但当前主流程不依赖 openclaw
