"""人工语义对齐后计算抽取覆盖，并自动检查预测引用。标准库，无需服务。
Usage: python tests/score_predictions.py --prediction path/to/predicted_extraction.json --alignment path/to/reviewed_alignment.jsonl
alignment line: {"gold_id":"U001","predicted_id":"info-...","match":"yes","review_comment":"..."}
"""
import argparse,json,pathlib,collections
from validate_eval import ROOT, js, jl

def main():
    p=argparse.ArgumentParser();p.add_argument('--prediction',required=True);p.add_argument('--alignment',required=True);p.add_argument('--output');p.add_argument('--extra-review',help='Optional JSONL of unmatched predicted_id with disposition: valid_extra, duplicate, irrelevant, unsupported');a=p.parse_args()
    pred=json.loads(pathlib.Path(a.prediction).read_text(encoding='utf-8'))
    units=pred.get('info_units',[])
    ids=[u.get('id') for u in units]
    assert len(set(ids))==len(ids),'prediction unit IDs must be unique'
    candidates=set(ids)
    allgold={u['id'] for u in jl('gold_info_units.jsonl')}
    documents={f['file_id']:f for f in js('fixtures/file_manifest.json')['files']}
    source={fid:(ROOT/f['fixture_text_path']).read_text(encoding='utf-8') for fid,f in documents.items()}
    references_invalid=[]
    for u in units:
        for r in u.get('source_refs',[]):
            try:
                fid=r['file_id'];doc=source[fid];q=r['quote'];s=r['start'];e=r['end'];correct=doc[s:e]==q and s<e and r.get('source_kind')==documents[fid]['source_kind']
            except (KeyError,TypeError,IndexError):correct=False
            if not correct:references_invalid.append({'predicted_id':u.get('id'),'source':r})
    matches=[json.loads(l) for l in pathlib.Path(a.alignment).read_text(encoding='utf-8').splitlines() if l.strip()]
    matched=[]
    for m in matches:
        assert m['gold_id'] in allgold,f'unknown gold ID {m["gold_id"]}'
        if m.get('match')=='yes':
            assert m.get('predicted_id') in candidates,f'unknown prediction id {m.get("predicted_id")}'
            matched.append((m['gold_id'],m['predicted_id']))
    assert len(set(x for x,y in matched))==len(matched),'gold ID aligned twice'
    assert len(set(y for x,y in matched))==len(matched),'one predicted unit should not match multiple gold units without manually splitting it'
    g=len(allgold);m=len(matched);n=len(units)
    extras_review=[]
    if a.extra_review:
        extras_review=[json.loads(line) for line in pathlib.Path(a.extra_review).read_text(encoding='utf-8').splitlines() if line.strip()]
        for item in extras_review:
            assert item['predicted_id'] in candidates-set(y for _,y in matched),f'review item must be unmatched: {item}'
            assert item['disposition'] in {'valid_extra','duplicate','irrelevant','unsupported'},f'bad disposition: {item}'
    reviewed_dist=collections.Counter(x['disposition'] for x in extras_review)
    result={'gold_units':g,'predicted_units':n,'reviewed_matches':m,'gold_coverage':round(m/g,4),'extra_predictions_reviewed':len(extras_review),'extra_dispositions':dict(reviewed_dist),'reference_invalid':len(references_invalid),'reference_invalid_details':references_invalid,'unmatched_predictions_need_review':sorted(candidates-set(y for _,y in matched)),'note':'Unmatched predicted nodes are not automatically hallucinations. Extra predictions need a manual disposition; duplicate/irrelevant/unsupported items contribute to overextraction review, not Gold recall.'}
    s=json.dumps(result,ensure_ascii=False,indent=2)
    if a.output:pathlib.Path(a.output).write_text(s+'\n',encoding='utf-8')
    print(s)
if __name__=='__main__':main()
