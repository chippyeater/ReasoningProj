"""Run actual case-A upload -> TXT -> configured LLM -> persisted extraction.

No Gold content enters the prompt. Manifest source labels are upload metadata.
Semantic alignment must be reviewed separately; a successful request is not a score.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
from gold_basis import current_gold_basis

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "backend"))

from starlette.datastructures import UploadFile
from app import case_store, llm_client
from app.case_service import create_case_from_uploads
from app.errors import AppError
from app.extraction_service import run_extraction


async def run(output: Path) -> dict:
    if output.exists() and any(output.iterdir()):
        raise ValueError('Evaluation output must be empty; preserve historical runs')
    output.mkdir(parents=True, exist_ok=True)
    gold_basis = current_gold_basis()
    manifest = json.loads((ROOT / "fixtures/file_manifest.json").read_text(encoding="utf-8"))
    gold_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "gold").iterdir() if p.is_file()}
    case_store.DATA_DIR = output / "runtime"
    uploads = [UploadFile(io.BytesIO((ROOT / "fixtures/raw" / f["filename"]).read_bytes()), filename=f["filename"])
               for f in manifest["files"]]
    source_kinds = {f["filename"]: f["source_kind"] for f in manifest["files"]}
    case = await create_case_from_uploads(manifest["case_title"], uploads, source_kinds)
    report = {"case_id": case.case_id, "model": None, "provider": None,
              "input_file_count": len(case.files), "gold_hashes": gold_hashes,
              "gold_basis": gold_basis}
    try:
        config = llm_client.load_config()
        report.update(model=config.model, provider=config.provider)
        result = await run_extraction(case)
    except AppError as exc:
        report.update(status="failed", error_code=exc.code, message=str(exc), details=exc.details)
    else:
        prediction = result.model_dump(mode="json")
        case_store.write_json(output / "prediction.json", prediction)
        report.update(status="success", info_unit_count=len(result.info_units),
                      source_ref_count=sum(len(u.source_refs) for u in result.info_units),
                      classification_counts={kind: sum(u.type.value == kind for u in result.info_units)
                                             for kind in ("entity", "event", "state", "rule", "position")},
                      semantic_alignment="pending_review")
        # Human review uses original fixture IDs; emitted production IDs remain untouched.
        expected_by_name = {f['filename']: f['file_id'] for f in manifest['files']}
        remap = {actual.file_id: expected_by_name[actual.filename] for actual in case.files}
        review_prediction = json.loads(json.dumps(prediction))
        for unit in review_prediction["info_units"]:
            for ref in unit["source_refs"]:
                ref["file_id"] = remap[ref["file_id"]]
        case_store.write_json(output / "prediction_for_scoring.json", review_prediction)
        case_store.write_json(output / "fixture_id_mapping.json", remap)
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "gold").iterdir() if p.is_file()}
    if after != gold_hashes:
        raise RuntimeError("Gold changed during evaluation")
    report["gold_unchanged"] = True
    saved = case_store.load_case_snapshot(case.case_id)
    extraction_runs = [r for r in saved.stage_runs if r.stage.value == "extraction"]
    if extraction_runs:
        stage = extraction_runs[-1]
        audit = json.loads((case_store.case_dir(case.case_id) / stage.output_snapshot_path).read_text(encoding="utf-8"))
        report.update(run_id=stage.id, prompt_version=stage.prompt_version,
                      model_request_attempts=audit.get('model_request_attempts'),
                      terminal_step=audit.get('active_step'))
        report['provider_usage'] = {name: audit[key].get('usage')
            for name,key in [('extraction','raw_response'),('consolidation','consolidation_raw_response')]
            if isinstance(audit.get(key),dict) and audit[key].get('usage') is not None}
    case_store.write_json(output / "run_report.json", llm_client.redact(report))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run(args.output.resolve()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "success" else 1)
