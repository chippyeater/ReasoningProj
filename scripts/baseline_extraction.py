"""One-time, explicit file-scope baseline for cases without recoverable extraction history.

Preview by default. Stop the server before --apply; historical audits are never rewritten.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app import case_store
from app.case_service import now_iso
from app.errors import AppError
from app.extraction_service import pending_extraction_files
from app.schemas import ParseStatus, PipelineStage, RunStatus, StageRun


def baseline_extraction(case_id: str, file_ids: list[str], *, apply: bool = False) -> dict:
    case = case_store.load_case(case_id)
    if case is None:
        raise ValueError("Unknown case")
    try:
        pending = pending_extraction_files(case)
    except AppError as exc:
        if exc.code != "extraction_history_baseline_required":
            raise
    else:
        return {"baseline_required": False, "pending_file_ids": pending}
    parsed = {file.file_id for file in case.files if file.parse_status == ParseStatus.parsed}
    if not file_ids or len(file_ids) != len(set(file_ids)) or not set(file_ids) <= parsed:
        raise ValueError("Explicitly select unique, parsed historical file IDs")
    # Missing evidence-bearing files cannot silently be treated as new input.
    referenced = {ref.file_id for unit in case.info_units for ref in unit.source_refs}
    if not referenced <= set(file_ids):
        raise ValueError("Baseline must include every file referenced by saved InfoUnits")
    root = case_store.case_dir(case_id)
    path = root / "case.json"
    original = path.read_bytes()
    result = {"baseline_required": True, "historical_file_ids": file_ids,
              "remaining_file_ids": [file.file_id for file in case.files
                                     if file.file_id in parsed - set(file_ids)], "applied": apply}
    if not apply:
        return result
    backup = root / "case.json.before-incremental-extraction.bak"
    if backup.exists():
        if backup.read_bytes() != original:
            raise ValueError("Existing baseline backup differs; inspect before retrying")
    else:
        with backup.open("xb") as stream:
            stream.write(original)
    run_id = f"run-{uuid4().hex}"
    run = StageRun(id=run_id, stage=PipelineStage.extraction, status=RunStatus.success,
                   model=None, prompt_version=None, input_snapshot_path=f"runs/{run_id}.input.json",
                   output_snapshot_path=f"runs/{run_id}.output.json", error_message=None, created_at=now_iso())
    # Same committed StageRun/file-scope protocol as normal extraction, with explicit provenance.
    case_store.write_json(root / run.input_snapshot_path, {
        "run_id": run_id, "operation": "historical_baseline", "selected_file_ids": file_ids,
        "model_input": {"files": [{"file_id": fid} for fid in file_ids]},
        "historical_run_ids": [item.id for item in case.stage_runs
                               if item.stage == PipelineStage.extraction and item.status == RunStatus.success],
        "case_snapshot_sha256": hashlib.sha256(original).hexdigest()})
    case_store.write_json(root / run.output_snapshot_path, {
        "run_id": run_id, "status": "success", "result_status": "historical_baseline",
        "selected_file_ids": file_ids, "model_request_attempts": 0})
    case_store.write_json(root / f"runs/{run_id}.stage.json", run.model_dump(mode="json"))
    if path.read_bytes() != original:
        raise ValueError("Case changed during baseline; no case update was committed")
    case.stage_runs.append(run)
    case.updated_at = now_iso()
    case_store.save_case(case, set_current=False)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--file-id", action="append", required=True,
                        help="Historical parsed file; repeat for each file. Exclude unextracted new uploads.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(baseline_extraction(args.case_id, args.file_id, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
