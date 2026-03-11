"""FastAPI application entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.llm import run_reasoning
from app.schemas import ReasonRequest, ReasonResponse


app = FastAPI(title="Reasoning Interface Generator API")

# Keep CORS open for local frontend demo.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    """Simple health endpoint."""

    return {"status": "ok"}


@app.post("/api/reason", response_model=ReasonResponse)
async def reason(payload: ReasonRequest) -> ReasonResponse:
    """Generate structured reasoning JSON from case text and question."""

    return await run_reasoning(payload.case_text, payload.question)
