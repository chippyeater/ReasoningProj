"""One-time workspace migration; never used by runtime loading or audit reads."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.case_store import write_json
from app.issue_workspaces import IssueWorkspaces
from app.schemas import CaseData


def migrate_workspace(path: Path, *, apply: bool = False) -> int:
    path = path.resolve()
    if path.name != "issue_workspaces.json":
        raise ValueError("Only issue_workspaces.json may be migrated")
    original = path.read_bytes()
    payload = json.loads(original.decode("utf-8"))
    updated = deepcopy(payload)
    count = 0
    for workspace in updated["workspaces"].values():
        for gap in workspace.get("reasoning", {}).get("gaps", []):
            if "info_unit_ids" in gap:
                if "basis_info_unit_ids" in gap or "addressing_info_unit_ids" in gap:
                    raise ValueError("Mixed old/new Gap fields; no data was written")
                gap["basis_info_unit_ids"] = gap.pop("info_unit_ids")
                gap["addressing_info_unit_ids"] = []
                count += 1
            elif "basis_info_unit_ids" not in gap or "addressing_info_unit_ids" not in gap:
                raise ValueError("Incomplete Gap fields; no data was written")
    current = IssueWorkspaces.model_validate(updated)
    case = CaseData.model_validate_json((path.parent / "case.json").read_bytes())
    known = {u.id for u in case.info_units}
    for workspace in current.workspaces.values():
        if any(not set(g.basis_info_unit_ids + g.addressing_info_unit_ids) <= known for g in workspace.reasoning.gaps):
            raise ValueError("Gap references unknown current-case InfoUnits; no data was written")
    if apply and count:
        backup = path.with_suffix(path.suffix + ".before-gap-reference-split.bak")
        # Exclusive creation preserves the original snapshot on retries.
        if backup.exists():
            if backup.read_bytes() != original:
                raise ValueError("Existing migration backup differs; no data was written")
        else:
            with backup.open("xb") as stream:
                stream.write(original)
        if path.read_bytes() != original:
            raise ValueError("Workspace changed during migration; no data was written")
        write_json(path, updated)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--apply", action="store_true", help="Back up and atomically write validated workspaces")
    args = parser.parse_args()
    for path in args.paths:
        count = migrate_workspace(path, apply=args.apply)
        print(f"{path}: {count} Gap(s) {'migrated' if args.apply else 'to migrate (read only)'}")


if __name__ == "__main__":
    main()
