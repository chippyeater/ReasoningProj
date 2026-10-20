"""对案件 A 有限法规库检索输出的可机械验证部分打分。兼容 JSONL。"""
import argparse,json,pathlib
ROOT=pathlib.Path(__file__).resolve().parents[1]

def load_jsonl(path):
    return [json.loads(s) for s in pathlib.Path(path).read_text(encoding='utf-8').splitlines() if s.strip()]

def evaluate(prediction_path):
    gold={x['id']:x for x in load_jsonl(ROOT/'gold'/'gold_law_cases.jsonl')}
    lawbase={x['id']:x for x in json.loads((ROOT/'gold'/'verified_laws.json').read_text(encoding='utf-8'))}
    results=[]
    for pred in load_jsonl(prediction_path):
        cid=pred['id']
        if cid not in gold: raise ValueError('Unknown law case: '+cid)
        entries=pred.get('selected_laws',[])
        if not isinstance(entries,list): raise ValueError(cid+' selected_laws must be array')
        ids=[str(x.get('law_id','')) for x in entries]
        expected=gold[cid]['expected']
        must=set(expected.get('required_law_ids',[])); forbidden=set(expected.get('forbidden_law_ids',[])); any_of=set(expected.get('required_any_of_law_ids',[]))
        id_check=(must<=set(ids) and not forbidden.intersection(ids) and (not any_of or bool(any_of.intersection(ids))) and set(ids)<=lawbase.keys())
        exact=[]; cited=[]
        for x in entries:
            item=lawbase.get(x.get('law_id'))
            if item is None:exact.append(False);cited.append(False);continue
            exact.append(None if 'text' not in x else x['text']==item['text'])
            cited.append(None if 'source_url' not in x else x['source_url']==item['source_url'])
        results.append(dict(id=cid,id_checks_pass=id_check,selected_ids=ids,raw_text_checks=exact,source_url_checks=cited,
                            reasoning_requires_human_review=[n for n in expected.get('checks',[]) if n not in {'law_id_correct','text_exact','source_url','source_exactness','reject_mismatched_article','accurate_article_text','retrieval_coverage'}]))
    return dict(prediction_count=len(results),gold_case_count=len(gold),results=results,note='None in exact/url checks means response omitted that field; cannot claim it was verified. Application/reasoning conditions need human review.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--predictions',required=True);parser.add_argument('--output')
    a=parser.parse_args();result=evaluate(a.predictions);s=json.dumps(result,ensure_ascii=False,indent=2)
    if a.output:pathlib.Path(a.output).write_text(s+'\n',encoding='utf-8')
    print(s)
