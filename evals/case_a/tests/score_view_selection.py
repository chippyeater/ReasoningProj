"""运行视图选择预测评分：constrained 精确匹配、flexible 合理视图集合、open 不评价视图类型。

Usage: python tests/score_view_selection.py --predictions predictions.jsonl [--output report.json]
Predictions JSONL: {"id":"S001", "view_type":"timeline", "reason":"...", "payload":{...}}
"""
import argparse,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def load(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

def score(predictions):
    gold=load(ROOT/"gold/gold_view_selection.jsonl")
    records=[];constrained=flexible=0; con_ok=flex_ok=0
    for item in gold:
        id_=item["id"]; e=item["expected"]; mode=e["selection_mode"]; p=predictions.get(id_)
        if p is None:
            if mode=="constrained":constrained+=1
            elif mode=="flexible":flexible+=1
            records.append({"id":id_,"mode":mode,"status":"missing_prediction","view_pass":False if mode!="open" else None}); continue
        choice=p.get("view_type"); allowed=e["allowed_view_types"]; valid=choice in {"timeline","conflict","hypothesis"}
        entry={"id":id_,"mode":mode,"chosen_view":choice,"status":"invalid" if not valid else "valid"}
        if mode=="constrained":
            constrained+=1; good=choice==allowed[0];con_ok+=good;entry["view_pass"]=bool(good)
        elif mode=="flexible":
            flexible+=1;good=choice in allowed;flex_ok+=good;entry["view_pass"]=bool(good)
        else:
            entry["view_pass"]=None
            entry["content_review_required"]=e["content_requirements"]
            entry["note"]="open 类型不计入视图准确率；必须另行审核实际生成 payload 的Schema、内容覆盖及来源。"
        records.append(entry)
    return {"constrained_accuracy":(con_ok/constrained if constrained else None),"flexible_acceptance":(flex_ok/flexible if flexible else None),"open_view_type_accuracy":None,"records":records}

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--predictions",required=True);parser.add_argument("--output")
    args=parser.parse_args()
    ps={x["id"]:x for x in load(Path(args.predictions))};result=score(ps)
    output=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:Path(args.output).write_text(output+"\n",encoding="utf-8")
    else:print(output)
if __name__=="__main__":main()
