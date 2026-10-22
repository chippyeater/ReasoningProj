"""Read-only CLI index for existing local StageRun JSON artifacts."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from app import case_store
from app.observability import build_run_name


@dataclass(frozen=True)
class RunIndexEntry:
    case_id: str
    case_title: str
    run_id: str
    run_name: str
    workflow_id: str | None
    stage: str
    status: str
    created_at: str
    stage_path: str
    input_path: str | None
    output_path: str | None


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def collect_runs(*, case_filter: str | None = None, stage: str | None = None,
                 status: str | None = None, since: str | None = None,
                 until: str | None = None) -> list[RunIndexEntry]:
    entries: list[RunIndexEntry] = []
    root = case_store.DATA_DIR
    if not root.exists():
        return entries
    case_query = case_filter.casefold() if case_filter else None
    for directory in root.iterdir():
        case_path = directory / "case.json"
        if not directory.is_dir() or not case_path.is_file():
            continue
        case_payload = _read_json(case_path)
        case_id = str(case_payload.get("case_id", directory.name))
        case_title = str(case_payload.get("case_title", case_id))
        if case_query and case_query not in case_id.casefold() and case_query not in case_title.casefold():
            continue
        runs = case_payload.get("stage_runs", [])
        if not isinstance(runs, list):
            continue
        for item in runs:
            if not isinstance(item, dict):
                continue
            run_id = str(item.get("id", ""))
            run_stage = str(item.get("stage", ""))
            run_status = str(item.get("status", ""))
            created_at = str(item.get("created_at", ""))
            if stage and run_stage != stage:
                continue
            if status and run_status != status:
                continue
            if since and created_at < since:
                continue
            if until and created_at > until:
                continue
            output_relative = item.get("output_snapshot_path")
            input_relative = item.get("input_snapshot_path")
            output_path = directory / output_relative if isinstance(output_relative, str) else None
            input_path = directory / input_relative if isinstance(input_relative, str) else None
            audit = _read_json(output_path) if output_path else {}
            stage_path = directory / "runs" / f"{run_id}.stage.json"
            entries.append(RunIndexEntry(
                case_id=case_id,
                case_title=case_title,
                run_id=run_id,
                run_name=str(audit.get("run_name") or build_run_name(
                    case_title, created_at, run_stage.replace("_", " ").title())),
                workflow_id=str(audit["workflow_id"]) if audit.get("workflow_id") else None,
                stage=run_stage,
                status=run_status,
                created_at=created_at,
                stage_path=str(stage_path.resolve()),
                input_path=str(input_path.resolve()) if input_path else None,
                output_path=str(output_path.resolve()) if output_path else None,
            ))
    return sorted(entries, key=lambda item: item.created_at, reverse=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List LogicWeaver local StageRun audit files")
    parser.add_argument("--case", dest="case_filter", help="case ID or case-title substring")
    parser.add_argument("--stage", choices=["parse", "extraction", "relation", "view_selection",
                                             "view_composition", "law_retrieval"])
    parser.add_argument("--status", choices=["success", "fallback", "failed"])
    parser.add_argument("--since", help="ISO date/time lower bound")
    parser.add_argument("--until", help="ISO date/time upper bound")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main() -> None:
    args = _parser().parse_args()
    entries = collect_runs(
        case_filter=args.case_filter, stage=args.stage, status=args.status,
        since=args.since, until=args.until,
    )
    if args.json:
        print(json.dumps([asdict(item) for item in entries], ensure_ascii=False, indent=2))
        return
    if not entries:
        print("No matching runs.")
        return
    for item in entries:
        print(f"{item.created_at}  {item.status:<8}  {item.stage:<12}  {item.run_name}")
        print(f"  case={item.case_id}  run={item.run_id}  workflow={item.workflow_id or '-'}")
        print(f"  stage={item.stage_path}")
        print(f"  input={item.input_path or '-'}")
        print(f"  output={item.output_path or '-'}")


if __name__ == "__main__":
    main()
