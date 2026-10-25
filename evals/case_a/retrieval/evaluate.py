"""Offline search diagnostics. Candidate strategies are experiments, never runtime tools."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys

from app import investigation_tools as tools
from app.schemas import InfoUnitType
from evals.case_a.investigation.data import ROOT, build_case, digest, load_snapshot

QUERIES = Path(__file__).with_name("queries.json")
STRATEGIES = ("production", "manual_focus", "literal_and", "bigram_coverage", "bigram_bm25")
KEY_IDS = ("U004", "U022", "U026", "U027", "U029", "U035", "U036")


def load_queries(path=QUERIES):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data["annotation_status"] != "requires_human_review":
        raise ValueError("These diagnostic labels have not been human reviewed")
    if data["snapshot_sha256"] != load_snapshot()["sha256"]:
        raise ValueError("Query relevance labels belong to a different Case A snapshot")
    ids = {u.id for u in build_case().info_units}
    for need in data["needs"].values():
        relevant, context = need["relevant_ids"], need["context_ids"]
        if (len(set(relevant)) != len(relevant) or len(set(context)) != len(context)
                or set(relevant) & set(context) or not set(relevant + context) <= ids):
            raise ValueError("Need labels must be unique, disjoint, current-snapshot IDs")
    if len({q["id"] for q in data["queries"]}) != len(data["queries"]):
        raise ValueError("Duplicate query IDs")
    for query in data["queries"]:
        need = data["needs"][query["need"]]
        if query["aggregate"] and (not need["relevant_ids"] or not query["focused_query"]):
            raise ValueError("Aggregate queries require positive labels and a manual focus input")
    return data


def literal_terms(query):
    return list(dict.fromkeys(re.findall(r"\w+", tools._normalized(query))))


def bigrams(text):
    """Generic overlapping Han bigrams; single Han chars and Latin/digits retained.

    No Case A vocabulary, synonym dictionary, Gold labels or LLM tokenizer.
    This intentionally small prototype only tokenizes Han/ASCII alphanumerics.
    """
    result = []
    for run in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", tools._normalized(text)):
        if re.fullmatch(r"[\u3400-\u9fff]{2,}", run):
            result.extend(run[i:i + 2] for i in range(len(run) - 1))
        else:
            result.append(run)
    return result


def rank(case, query, types=None, strategy="production"):
    """Full ranking for diagnostics, not an unbounded model-facing retrieval API."""
    if strategy not in STRATEGIES:
        raise ValueError("Unknown diagnostic strategy")
    check = tools.search_info_units(case, query, types=types, limit=5)
    if check["error"]:
        return {"terms": [], "ranking": [], "error": check["error"], "status": "invalid_request"}
    allowed = {t.value for t in InfoUnitType} if types is None else set(types)
    use_bigrams = strategy.startswith("bigram_")
    terms = list(dict.fromkeys(bigrams(query))) if use_bigrams else literal_terms(query)
    documents = {u.id: Counter(bigrams(u.title + " " + u.content)) for u in case.info_units}
    lengths = {uid: sum(doc.values()) for uid, doc in documents.items()}
    average_length = sum(lengths.values()) / len(lengths)
    document_frequency = Counter(term for doc in documents.values() for term in doc)
    rows = []
    for unit in case.info_units:
        if unit.type.value not in allowed:
            continue
        title, content = tools._normalized(unit.title), tools._normalized(unit.content)
        matched = [term for term in terms if term in title or term in content]
        if not terms or not matched or strategy == "literal_and" and len(matched) != len(terms):
            continue
        coverage, weighted = tools._keyword_score(unit, terms)
        score = None
        if strategy == "bigram_bm25":
            # Robertson positive IDF, k1=1.2, b=0.75, title+content without a field boost.
            score = 0.0
            for term in terms:
                frequency = documents[unit.id][term]
                if frequency:
                    df = document_frequency[term]
                    inverse_frequency = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
                    score += inverse_frequency * frequency * 2.2 / (
                        frequency + 1.2 * (0.25 + 0.75 * lengths[unit.id] / average_length))
        rows.append({"id": unit.id, "coverage": coverage, "weighted": weighted,
                     "bm25": score, "matched_terms": matched})
    if strategy == "bigram_bm25":
        rows.sort(key=lambda row: (-row["bm25"], row["id"]))
    else:
        rows.sort(key=lambda row: (-row["coverage"], -row["weighted"], row["id"]))
    for index, row in enumerate(rows, 1):
        row["rank"] = index
    if strategy == "production" or strategy == "manual_focus":
        for k in (5, 8):
            actual = tools.search_info_units(case, query, types=types, limit=k)
            if [r["id"] for r in actual["items"]] != [r["id"] for r in rows[:k]]:
                raise ValueError("Diagnostic ranking differs from production")
            if actual["has_more"] != (len(rows) > k):
                raise ValueError("Diagnostic truncation differs from production")
    return {"terms": terms, "ranking": rows, "error": None,
            "status": "ok" if rows else "empty"}


def metrics(ranking, relevant_ids):
    ids, relevant = [r["id"] for r in ranking], set(relevant_ids)
    positions = {uid: ids.index(uid) + 1 if uid in ids else None for uid in relevant_ids}
    result = {"matched_count": len(ids), "relevant_ranks": positions,
              "first_relevant_rank": min((p for p in positions.values() if p is not None), default=None),
              "missed_ids": [uid for uid, p in positions.items() if p is None],
              "query_coverage": len(relevant & set(ids)) / len(relevant) if relevant else None}
    for k in (5, 8):
        returned = ids[:k]
        hits = len(relevant & set(returned))
        result.update({f"recall_at_{k}": hits / len(relevant) if relevant else None,
                       f"precision_at_{k}": hits / k,
                       f"returned_precision_at_{k}": hits / len(returned) if returned else None,
                       f"has_more_at_{k}": len(ids) > k,
                       f"relevant_beyond_{k}": [uid for uid, pos in positions.items() if pos is not None and pos > k]})
        # Simulated offset pages: ranking stays identical. No production pagination exists.
        result[f"pagination_{k}"] = {"calls_to_exhaust": max(1, math.ceil(len(ids) / k)),
            "relevant_first_page": {uid: math.ceil(pos / k) if pos is not None else None for uid, pos in positions.items()},
            "exhaustive_recall": result["query_coverage"]}
    return result


def aggregate(rows):
    names = ("recall_at_5", "recall_at_8", "precision_at_5", "precision_at_8", "query_coverage",
             "returned_precision_at_5", "returned_precision_at_8")
    result = {"query_count": len(rows)}
    for name in names:
        values = [row[name] for row in rows if row[name] is not None]
        result[name] = sum(values) / len(values) if values else None
        result[name + "_denominator"] = len(values)
    result["no_hit_count"] = sum(row["matched_count"] == 0 for row in rows)
    result["full_query_coverage_count"] = sum(row["query_coverage"] == 1 for row in rows)
    for k in (5, 8):
        result[f"has_more_count_at_{k}"] = sum(row[f"has_more_at_{k}"] for row in rows)
        result[f"mean_page_calls_at_{k}"] = sum(row[f"pagination_{k}"]["calls_to_exhaust"] for row in rows) / len(rows) if rows else None
    return result


def verify_recorded_call(case, definition, audit_path=None):
    recorded = definition["original_call"]
    actual = tools.search_info_units(case, recorded["query"], types=recorded["types"], limit=recorded["limit"])
    if ([r["id"] for r in actual["items"]] != recorded["returned_ids"]
            or actual["status"] != recorded["status"] or actual["has_more"] != recorded["has_more"]):
        raise ValueError("Recorded v15 query no longer reproduces its original response")
    audit_evidence = {"local_audit_verified": False}
    if audit_path:
        audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
        if audit["run_id"] != recorded["run_id"]:
            raise ValueError("Wrong source run")
        calls = [c for c in audit["tool_calls"] if c["call_id"] == recorded["call_id"]]
        if len(calls) != 1 or calls[0]["name"] != "search_info_units":
            raise ValueError("Original tool call not found exactly once")
        call = calls[0]
        if any(call["arguments"].get(k) != recorded[k] for k in ("query", "types", "limit")) or call["result"] != actual:
            raise ValueError("Original audit arguments/results differ from the recorded query")
        audit_evidence = {"local_audit_verified": True, "file_sha256": hashlib.sha256(Path(audit_path).read_bytes()).hexdigest(),
            "subsequent_tool_names": [c["name"] for c in audit["tool_calls"][audit["tool_calls"].index(call) + 1:]],
            "final_read_budget_used": audit["result"]["tool_budget_used"]["read"],
            "final_limitations": audit["result"]["limitations"]}
    full = rank(case, recorded["query"], recorded["types"])
    by_id = {row["id"]: row for row in full["ranking"]}
    key_units = {}
    for uid in KEY_IDS:
        unit = next(u for u in case.info_units if u.id == uid)
        source_text = tools._normalized(" ".join(ref.quote for ref in unit.source_refs))
        key_units[uid] = {"rank": by_id.get(uid, {}).get("rank"), "returned": uid in recorded["returned_ids"],
            "indexed_matched_terms": by_id.get(uid, {}).get("matched_terms", []),
            "source_only_terms": [term for term in full["terms"] if term in source_text
                and term not in tools._normalized(unit.title + " " + unit.content)],
            "reason": "returned" if uid in recorded["returned_ids"] else "below_limit" if uid in by_id else "no_indexed_keyword_overlap"}
    return {**recorded, "full_ranking": full, "key_units": key_units, "audit": audit_evidence,
            "content_truncated": [row["content_truncated"] for row in actual["items"]]}


def evaluate(definition=None, audit_path=None):
    definition = load_queries() if definition is None else definition
    case = build_case()
    before = case.model_dump_json()
    results = []
    for query in definition["queries"]:
        relevant = definition["needs"][query["need"]]["relevant_ids"]
        variants = {}
        for strategy in STRATEGIES:
            if strategy == "manual_focus" and query["focused_query"] is None:
                continue
            text = query["focused_query"] if strategy == "manual_focus" else query["query"]
            response = rank(case, text, query["types"], strategy)
            variants[strategy] = {"query": text, **response, "metrics": metrics(response["ranking"], relevant)}
        production = tools.search_info_units(case, query["query"], types=query["types"], limit=5)
        if query.get("expected_status") and production["status"] != query["expected_status"]:
            raise ValueError(f"Boundary query changed: {query['id']}")
        results.append({**query, "relevant_ids": relevant, "production_status_at_5": production["status"], "variants": variants})
    summaries = {}
    for strategy in STRATEGIES:
        groups, kinds = defaultdict(list), defaultdict(list)
        for query in results:
            if query["aggregate"]:
                row = query["variants"][strategy]["metrics"]
                groups[query["need"]].append(row)
                kinds[query["kind"]].append(row)
        by_need = {need: aggregate(rows) for need, rows in groups.items()}
        primary = {name: sum(row[name] for row in by_need.values()) / len(by_need)
                   for name in ("recall_at_5", "recall_at_8", "precision_at_5", "precision_at_8", "query_coverage")}
        summaries[strategy] = {"primary_need_macro": primary, "query_macro": aggregate([r for rows in groups.values() for r in rows]),
                               "by_need": by_need, "by_kind": {kind: aggregate(rows) for kind, rows in kinds.items()}}
    union, plan_calls = set(), []
    for text in definition["manual_a1_plan"]["queries"]:
        response = tools.search_info_units(case, text, limit=5)
        ids = [row["id"] for row in response["items"]]
        union.update(ids)
        plan_calls.append({"query": text, "returned_ids": ids, "has_more": response["has_more"]})
    plan_metrics = {"calls": len(plan_calls), "queries": plan_calls,
                    "relevant_ids_found": sorted(union & set(definition["needs"]["a1"]["relevant_ids"])),
                    "missed_ids": sorted(set(definition["needs"]["a1"]["relevant_ids"]) - union),
                    "returned_unique_ids": sorted(union), "not_an_agent_experiment": True}
    recorded = verify_recorded_call(case, definition, audit_path)
    if case.model_dump_json() != before:
        raise ValueError("Evaluation mutated CaseData")
    repo = ROOT.parents[1]
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain=v1"], cwd=repo, capture_output=True, text=True, check=True).stdout)
    return {"mode": "offline", "paid_model_calls": 0, "annotation_status": definition["annotation_status"],
        "metadata": {"queries_version": definition["version"], "queries_sha256": digest(definition),
            "snapshot_sha256": load_snapshot()["sha256"], "case_data_sha256": digest(case.model_dump(mode="json")),
            "production_code_sha256": hashlib.sha256(Path(tools.__file__).read_bytes()).hexdigest(),
            "evaluation_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "code_git_commit": commit, "worktree_dirty": dirty, "python_version": sys.version.split()[0]},
        "metric_policy": "R@k=hits/need positives; P@k=hits/k even for short results. Empty positives have undefined R; excluded with all diagnostic rows from primary macro. Primary is equal-weight mean of need means. All labels and manual focus inputs pending human review.",
        "recorded_v15": recorded, "summary": summaries, "manual_a1_plan": plan_metrics, "queries": results}


def render_report(result):
    lines = ["# Case A offline retrieval diagnostic", "", "All relevance judgments require human review; no LLM or remote writes.",
        "No production Prompt/tool/Gold changes. Offline access metrics do not establish investigation quality.", "",
        f"Primary: equal-weight mean of {len(result['summary']['production']['by_need'])} information needs "
        f"({sum(q['aggregate'] for q in result['queries'])} positive queries); "
        f"{sum(not q['aggregate'] for q in result['queries'])} diagnostic queries excluded.",
        result["metric_policy"], "", "| Strategy | R@5 | R@8 | P@5 | P@8 | Full lexical coverage |", "|---|---:|---:|---:|---:|---:|"]
    for strategy, summary in result["summary"].items():
        row = summary["primary_need_macro"]
        lines.append("| " + strategy + " | " + " | ".join(f"{row[k]:.4f}" for k in ("recall_at_5", "recall_at_8", "precision_at_5", "precision_at_8", "query_coverage")) + " |")
    lines += ["", "manual_focus is human-designed input substitution, NOT tested Tool Description compliance. It may narrow a multi-goal need.",
        "Pagination changes later accessibility, not first-page R@5/R@8; exhaustive coverage is the lexical upper bound, with per-query page costs in JSON.",
        "Bigram prototypes use no synonym dictionary. BM25(k1=1.2,b=.75) is not tuned/held-out. AND is an explicit literal-token experiment, not a default recommendation.",
        "", "## Recorded v15 ranking", "", "| Rank | ID | Coverage | Weighted | Matched terms |", "|---:|---|---:|---:|---|"]
    for row in result["recorded_v15"]["full_ranking"]["ranking"]:
        lines.append(f"| {row['rank']} | {row['id']} | {row['coverage']} | {row['weighted']} | {' / '.join(row['matched_terms'])} |")
    lines += ["", "## All query baseline metrics", "", "| Query | Need | R@5 | R@8 | P@5 | P@8 | Full coverage | Matches | More@5/8 | First relevant | Missed IDs |", "|---|---|---:|---:|---:|---:|---:|---:|---|---:|---|"]
    for query in result["queries"]:
        row = query["variants"]["production"]["metrics"]
        fmt = lambda value: "NA" if value is None else f"{value:.3f}"
        lines.append(f"| {query['id']} | {query['need']} | " + " | ".join(fmt(row[k]) for k in ("recall_at_5", "recall_at_8", "precision_at_5", "precision_at_8", "query_coverage"))
            + f" | {row['matched_count']} | {row['has_more_at_5']}/{row['has_more_at_8']} | {row['first_relevant_rank']} | {','.join(row['missed_ids'])} |")
    lines += ["", "Full candidate rankings/scores, per-query metrics, first relevant and individual ID ranks, missed IDs, page costs, query/type diagnostics and hashes are in the paired JSON.", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New JSON path; also creates adjacent Markdown; existing outputs rejected")
    parser.add_argument("--audit", type=Path, help="Optional original v15 *.output.json for exact local audit verification")
    args = parser.parse_args(argv)
    markdown = args.output.with_suffix(".md")
    if args.output.suffix != ".json" or args.output.exists() or markdown.exists():
        parser.error("Use a new .json output path; historical output cannot be overwritten")
    result = evaluate(audit_path=args.audit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    with markdown.open("x", encoding="utf-8") as file:
        file.write(render_report(result))
    print(json.dumps({"output": str(args.output), "paid_model_calls": 0,
        "local_audit_verified": result["recorded_v15"]["audit"]["local_audit_verified"],
        "summary": {s: row["primary_need_macro"] for s, row in result["summary"].items()}}, ensure_ascii=False, indent=2))
