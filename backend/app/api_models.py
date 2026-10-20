"""HTTP transport envelopes; business objects are defined only in schemas.py."""
from app.schemas import CaseData, ContractModel, Text


class CaseResponse(ContractModel):
    case: CaseData | None


class CasesResponse(ContractModel):
    cases: list[CaseData]
    current_case_id: str | None


class UpdateCaseInput(ContractModel):
    case_title: Text


class ExtractionRunInput(ContractModel):
    case_id: Text


class RelationRunInput(ContractModel):
    case_id: Text
