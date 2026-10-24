"""Investigation-only reference criteria and reproducible existing Case A data."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app import case_store
from app.case_service import create_empty_case
from app.schemas import CaseData

ROOT = Path(__file__).resolve().parents[1]
DEFINITION = Path(__file__).with_name("dataset.json")
SNAPSHOT = Path(__file__).with_name("snapshot.json")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TaskInput(StrictModel):
    task_id: Literal["A1", "A2", "A3"]
    case_id: str
    case_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_state_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    goal: str = Field(min_length=1, max_length=2000)
    core_issue: str
    selected_info_unit_ids: list[str]


class EvidenceGroup(StrictModel):
    id: str = Field(min_length=1)
    unit_ids: list[str] = Field(min_length=1)
    mode: Literal["all", "any"]
    necessary: bool


class Criteria(StrictModel):
    objectives: list[str] = Field(min_length=1)
    necessary_evidence: list[str] = Field(min_length=1)
    optional_leads: list[str]
    acceptable_boundaries: list[str] = Field(min_length=1)
    critical_errors: list[str] = Field(min_length=1)
    evidence_groups: list[EvidenceGroup] = Field(min_length=1)
    scoring_basis: str = Field(min_length=1)


def load_definition():
    definition = json.loads(DEFINITION.read_text(encoding="utf-8"))
    if definition["name"] != "logicweaver/investigation/case-a-v1" or definition["version"] != "case-a-v1":
        raise ValueError("Unexpected Investigation dataset identity")
    if [item["task_id"] for item in definition["items"]] != ["A1", "A2", "A3"]:
        raise ValueError("Exactly the ordered A1/A2/A3 tasks are required")
    if set(definition["human_scores"]) != {"task_coverage", "evidence_support", "gap_handling", "completion_calibration"}:
        raise ValueError("Unexpected human score dimensions")
    if any(len(labels) != 3 or any(not isinstance(s, str) or not s.strip() for s in labels)
           for labels in definition["human_scores"].values()):
        raise ValueError("Human score rubrics require descriptions for 0, 1 and 2")
    for item in definition["items"]:
        Criteria.model_validate(item["expected_output"])
        if len({g["id"] for g in item["expected_output"]["evidence_groups"]}) != len(item["expected_output"]["evidence_groups"]):
            raise ValueError("Evidence group IDs must be unique per task")
    return definition


def load_snapshot():
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if snapshot["sha256"] != digest({k: v for k, v in snapshot.items() if k != "sha256"}):
        raise ValueError("Snapshot manifest hash differs")
    for relative, expected in snapshot["files"].items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Case A snapshot changed: {relative}; create a new evaluation version after review")
    return snapshot


def build_case():
    snapshot = load_snapshot()
    manifest = json.loads((ROOT / "fixtures/file_manifest.json").read_text(encoding="utf-8"))
    issues = json.loads((ROOT / "gold/gold_issues.json").read_text(encoding="utf-8"))
    payload = create_empty_case("Case A Investigation evaluation").model_dump(mode="json")
    payload.update(case_id=snapshot["case_id"], created_at=snapshot["timestamp"], updated_at=snapshot["timestamp"],
                   core_issue=issues["core_issue"], issue_set=issues["issue_set"])
    for field, filename in (("info_units", "gold_info_units.jsonl"), ("relations", "gold_relations.jsonl")):
        payload[field] = [json.loads(line) for line in (ROOT / "gold" / filename).read_text(encoding="utf-8").splitlines() if line.strip()]
    for record in manifest["files"]:
        file = {k: record[k] for k in ("file_id", "filename", "file_type", "source_kind", "original_path",
                                      "parsed_text_path", "parse_status", "error_message")}
        file.update(file_size=0, uploaded_at=snapshot["timestamp"])
        payload["files"].append(file)
    return CaseData.model_validate(payload)


def dataset_items():
    definition, snapshot, case = load_definition(), load_snapshot(), build_case()
    initial = definition["initial_state"]
    if initial["case_id"] != case.case_id or initial["core_issue"] != case.core_issue:
        raise ValueError("Dataset initial state does not match Case A")
    ids = {u.id for u in case.info_units}
    if not set(initial["selected_info_unit_ids"]) <= ids:
        raise ValueError("Unknown initial InfoUnit")
    result = []
    for item in definition["items"]:
        for group in item["expected_output"]["evidence_groups"]:
            if len(set(group["unit_ids"])) != len(group["unit_ids"]) or not set(group["unit_ids"]) <= ids:
                raise ValueError("Evidence groups must use unique current snapshot InfoUnit IDs")
        values = TaskInput(task_id=item["task_id"], case_id=case.case_id, goal=item["goal"], core_issue=case.core_issue,
            case_snapshot_sha256=snapshot["sha256"], initial_state_sha256=digest(initial),
            selected_info_unit_ids=initial["selected_info_unit_ids"]).model_dump()
        result.append({"id": digest([definition["name"], item["task_id"]]), "input": values,
            "expected_output": item["expected_output"], "metadata": {
                "task_id": item["task_id"], "title": item["title"], "dataset_version": definition["version"],
                "case_snapshot_id": snapshot["snapshot_id"], "case_snapshot_sha256": snapshot["sha256"],
                "initial_state_sha256": digest(initial), "criteria_sha256": digest(item["expected_output"]),
                "historical_trace_id": item["historical_trace_id"]},
            "source_trace_id": item["historical_trace_id"], "source_observation_id": item["historical_observation_id"]})
    return result


def work_context(case):
    return {"current_issue": case.core_issue,
            "issues": [{"issue": issue, "status": "pending"} for issue in case.issue_set],
            "reasoning": load_definition()["initial_state"]["reasoning"]}


@contextmanager
def isolated_case():
    """Standalone sequential runner only; never point the real server here."""
    case = build_case()
    before = case.model_dump_json()
    manifest = json.loads((ROOT / "fixtures/file_manifest.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="logicweaver-investigation-eval-") as directory:
        data_dir, previous = Path(directory), case_store.DATA_DIR
        for record in manifest["files"]:
            path = data_dir / case.case_id / record["parsed_text_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / record["fixture_text_path"]).read_bytes())
        initial_files = {p.relative_to(data_dir).as_posix(): p.read_bytes() for p in data_dir.rglob("*") if p.is_file()}
        try:
            case_store.DATA_DIR = data_dir
            yield case, data_dir / case.case_id
        finally:
            case_store.DATA_DIR = previous
            if case.model_dump_json() != before:
                raise ValueError("Evaluation mutated its initial CaseData")
            current = {p.relative_to(data_dir).as_posix(): p.read_bytes() for p in data_dir.rglob("*") if p.is_file()}
            if any(current.get(p) != raw for p, raw in initial_files.items()) or any("/runs/" not in p for p in current.keys() - initial_files.keys()):
                raise ValueError("Only isolated run audits may be written during evaluation")
