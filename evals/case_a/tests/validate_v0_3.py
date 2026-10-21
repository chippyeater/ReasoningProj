"""Offline validation for the independent Schema v0.3 Gold projection."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "backend"))

from app.schemas import (
    ConflictView, ExtractionOutput, HypothesisView, Relation, TimelineView,
    ViewPayload, check_relations,
)


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    target = ROOT / "gold_v0_3"
    issues = json.loads((ROOT / "gold/gold_issues.json").read_text(encoding="utf-8"))
    units = rows(target / "gold_info_units.jsonl")
    extraction = ExtractionOutput(core_issue=issues["core_issue"], issue_set=issues["issue_set"], info_units=units)
    relations = [Relation.model_validate(row) for row in rows(target / "gold_relations.jsonl")]
    check_relations(extraction.info_units, relations)
    unit_ids = {unit.id for unit in extraction.info_units}
    view_counts: dict[str, int] = {}
    for row in rows(target / "gold_view_cases.jsonl"):
        payload = TypeAdapter(ViewPayload).validate_python(row["payload"])
        view_counts[payload.view_type] = view_counts.get(payload.view_type, 0) + 1
        if isinstance(payload, ConflictView):
            references = {item.info_unit_id for section in payload.sections for item in section.items}
        elif isinstance(payload, TimelineView):
            references = {unit_id for event in payload.events for unit_id in event.related_info_unit_ids}
        elif isinstance(payload, HypothesisView):
            references = {
                unit_id for step in payload.steps
                for unit_id in [*step.supporting_info_unit_ids, *step.opposing_info_unit_ids]
            }
        else:  # pragma: no cover - the discriminated union is exhaustive.
            raise AssertionError(f"unsupported view payload: {type(payload).__name__}")
        assert references, f"{payload.view_type}: expected at least one InfoUnit reference"
        assert references <= unit_ids, f"{payload.view_type}: unknown InfoUnit reference"
    mapping = json.loads((target / "label_mapping.json").read_text(encoding="utf-8"))
    assert mapping["source_gold_fingerprint"] == "198069edcf222a0bd61a4a464c6dd5521f055b8a2f5903c8ed4d07e43b411c57"
    assert len(mapping["mappings"]) == len(units)
    assert view_counts == {"conflict": 1, "timeline": 1, "hypothesis": 1}
    print("PASS: v0.3 Gold / relations / three view payloads / source mapping")
    print(f"  info_units: {len(units)}")
    print(f"  semantic_relations: {len(relations)}")
    print("  metric_stages: raw_first_round, normalized, consolidation (reported separately)")


if __name__ == "__main__":
    main()
