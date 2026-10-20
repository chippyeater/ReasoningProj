"""Score v0.2 {id, matches: LawMatch[]} JSONL against unchanged case-A Gold."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate(prediction_path):
    gold = {x["id"]: x for x in load_jsonl(ROOT / "gold/gold_law_cases.jsonl")}
    laws = {x["id"]: x for x in json.loads((ROOT / "gold/verified_laws.json").read_text(encoding="utf-8"))}
    runtime = {x["id"]: x for x in json.loads(
        (ROOT.parents[1] / "backend/data/laws.json").read_text(encoding="utf-8"))}
    unit_ids = {x["id"] for x in load_jsonl(ROOT / "gold/gold_info_units.jsonl")}
    results = []
    seen = set()
    for prediction in load_jsonl(prediction_path):
        if set(prediction) != {"id", "matches"}:
            raise ValueError("Expected {id, matches: LawMatch[]}")
        cid = prediction["id"]
        if cid not in gold or cid in seen:
            raise ValueError(f"Unknown or duplicate law case: {cid}")
        seen.add(cid)
        matches = prediction["matches"]
        if not isinstance(matches, list):
            raise ValueError(f"{cid}: matches must be an array")
        ids = []
        for match in matches:
            if not isinstance(match, dict) or set(match) != {"law_id", "issue", "related_info_unit_ids", "relevance_reason"}:
                raise ValueError(f"{cid}: invalid LawMatch fields")
            if any(not isinstance(match[k], str) or not match[k].strip()
                   for k in ("law_id", "issue", "relevance_reason")):
                raise ValueError(f"{cid}: empty LawMatch text")
            refs = match["related_info_unit_ids"]
            if not isinstance(refs, list) or any(not isinstance(uid, str) or uid not in unit_ids for uid in refs):
                raise ValueError(f"{cid}: invalid InfoUnit reference")
            ids.append(match["law_id"])
        if len(ids) != len(set(ids)):
            raise ValueError(f"{cid}: duplicate law IDs")
        expected = gold[cid]["expected"]
        required = set(expected.get("required_law_ids", []))
        forbidden = set(expected.get("forbidden_law_ids", []))
        any_of = set(expected.get("required_any_of_law_ids", []))
        selected = set(ids)
        passed = (required <= selected and not forbidden & selected
                  and (not any_of or bool(any_of & selected)) and selected <= laws.keys())
        exact = [lid in runtime and lid in laws and all(runtime[lid].get(k) == laws[lid][k]
                 for k in ("title", "article_no", "text", "source_url")) for lid in ids]
        results.append({"id": cid, "id_checks_pass": passed, "selected_ids": ids,
                        "corpus_exactness_checks": exact, "relevance_requires_human_review": True})
    return {"prediction_count": len(results), "gold_case_count": len(gold), "results": results,
            "note": "Corpus exactness checks inspect referenced LawEntry records; semantic relevance still needs human review."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    output = json.dumps(evaluate(args.predictions), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    print(output)
