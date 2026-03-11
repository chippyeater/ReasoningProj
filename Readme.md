# Reasoning Interface Generator

Minimal runnable demo for this chain:

User inputs case text + reasoning question -> backend calls LLM and returns structured JSON -> frontend auto-renders a reasoning interface from `recommended_view`.

## Project Structure

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
  openclaw-integration/
    reasoningTool.ts
  README.md
```

## 1. Run Backend (FastAPI)

```bash
cd backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Health check:

```bash
curl http://localhost:8000/health
```

### LLM Configuration (Optional)

If no key is configured, backend automatically returns mock JSON.

```bash
# Optional
set OPENAI_API_KEY=your_key
set OPENAI_BASE_URL=https://api.openai.com/v1
set OPENAI_MODEL=gpt-4o-mini
```

## 2. Run Frontend (React + Vite + TypeScript)

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

## 3. API Contract

`POST /api/reason`

Request:

```json
{
  "case_text": "...",
  "question": "..."
}
```

Response:

```json
{
  "entities": [],
  "events": [],
  "claims": [],
  "conflicts": [],
  "evidence_paths": [],
  "recommended_view": "conflict_compare",
  "summary": "..."
}
```

## 4. Demo Case Built In

Default frontend input:

- 证词A：A 在 22:00 出现在仓库
- 证词B：A 在 22:00 在公司加班
- 监控：A 在 21:45 进入仓库
- 银行记录：A 在 22:10 收到转账

## 5. Error Handling

- Backend: any LLM call/JSON parsing failure falls back to mock data.
- Frontend: if API request fails (network/HTTP), a visible error message is shown.

## 6. OpenClaw Integration

File: `openclaw-integration/reasoningTool.ts`

- Input: `case_text`, `question`
- Calls: `http://localhost:8000/api/reason`
- Returns: structured JSON for OpenClaw agent use

## 7. Quick Test Steps

1. Start backend on `:8000`
2. Start frontend on `:5173`
3. Click submit in UI and verify right panel shows entities and one of 3 views
4. Stop backend and submit again to verify frontend displays error message

