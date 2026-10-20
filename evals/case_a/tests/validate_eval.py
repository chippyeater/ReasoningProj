"""案件 A 评测集一致性检查：零第三方依赖。运行 python tests/validate_eval.py。"""
import json
import hashlib
import pathlib
import re
from collections import Counter

ROOT=pathlib.Path(__file__).resolve().parents[1]
def jl(name):
    p=ROOT/'gold'/name
    return [json.loads(line) for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
def js(name):return json.loads((ROOT/name).read_text(encoding='utf-8'))
def assure(test,msg):
    if not test:raise AssertionError(msg)
def assert_unique(xs,what):
    ids=[x['id'] for x in xs]
    assure(len(set(ids))==len(ids),f'{what} duplicate ids: {list(k for k,v in Counter(ids).items() if v>1)}')

def validate():
    issues=js('gold/gold_issues.json')['issue_set']
    um=jl('gold_info_units.jsonl');rs=jl('gold_relations.jsonl');ng=jl('negative_relations.jsonl')
    for what,xs in [('units',um),('relations',rs),('negatives',ng)]:assert_unique(xs,what)
    assert_unique(jl('gold_view_cases.jsonl'),'view cases')
    assert_unique(jl('gold_view_selection.jsonl'),'selector cases')
    assert_unique(jl('gold_law_cases.jsonl'),'law cases')
    assert_unique(jl('gold_end_to_end.jsonl'),'end to end')
    assert_unique(jl('gold_parse_cases.jsonl'),'parse cases')
    mf=js('fixtures/file_manifest.json')
    docs={x['file_id']:x for x in mf['files']}
    raw={}
    for fid,file in docs.items():
        path=ROOT/file['fixture_text_path'];assure(path.is_file(),f'file missing {path}')
        b=path.read_bytes();v=b.decode('utf-8')
        assure(hashlib.sha256(b).hexdigest()==file['text_sha256'],f'bad source sha {fid}')
        assure(len(v)==file['chars'],f'bad chars {fid}')
        raw[fid]=v
    type_map={'entity':{'person','organization','object','location'},'event':{'action','occurrence','transfer','communication'},'state':{'temporal','quantitative','descriptive'},'statement':{'fact_assertion','assessment','provision'}}
    required={'id','type','subtype','title','summary','content','source_refs','speaker','side','actor','target','stance_target','created_at'}
    unitids=set()
    for u in um:
        uid=u['id'];unitids.add(uid)
        assure(set(u)==required,f'{uid} fields unexpected or missing {set(u)^required}')
        assure(u['type'] in type_map and u['subtype'] in type_map[u['type']],f'{uid} illegal type')
        assure(u['stance_target'] is None or u['stance_target'] in issues,f'{uid} illegal issue')
        assure(u['side'] is None or (isinstance(u['side'],str) and bool(u['side'].strip())),f'{uid} illegal side')
        assure(u['source_refs'],f'{uid} no source')
        assure(all(u[k] for k in ('id','title','summary','content')),f'{uid} no content')
        assure(u['created_at'].endswith('Z'),f'{uid} wrong timestamp format')
        for s in u['source_refs']:
            fid=s['file_id'];assure(fid in docs,f'{uid} missing file {fid}')
            assure(s['source_kind'] in {'document','communication','testimony','record','other'},f'{uid} invalid source kind')
            assure(docs[fid]['source_kind']==s['source_kind'],f'{uid} incorrect source kind')
            q=s['quote'];p=s['start'];end=s['end']
            assure(set(s)=={'file_id','source_kind','quote','start','end'},f'{uid} malformed source_ref')
            assure(type(p) is int and type(end) is int and 0<=p<end<=len(raw[fid]),f'{uid} invalid slice')
            assure(raw[fid][p:end]==q,f'{uid} quote mismatch: {repr(raw[fid][p:end])}')
    types={x['id']:x['type'] for x in um}
    allowed={'actor_of','target_of','about','support','contradict','occur_at'}
    seen=set()
    for r in rs:
        a=r['source_id'];b=r['target_id'];kind=r['relation_type']
        assure(a in unitids and b in unitids and a!=b,f'{r["id"]} bad end')
        assure(kind in allowed,f'{r["id"]} bad type')
        assure(not (a,b,kind) in seen,f'{r["id"]} duplicate edge')
        seen.add((a,b,kind))
        assure(r['evidence_unit_ids'] and all(i in unitids for i in r['evidence_unit_ids']),f'{r["id"]} bad evidence')
        assure(r['certainty'] in {'explicit','inferred'},f'{r["id"]} bad certainty')
        if kind in ('actor_of','target_of'):
            assure(types[a]=='event' and types[b]=='entity',f'{r["id"]} wrong actor/target types')
        if kind=='occur_at':assure(types[a]=='event' and types[b]=='state' and next(u for u in um if u['id']==b)['subtype']=='temporal',f'{r["id"]} wrong time relation types')
        if kind=='contradict':assure(types[a]=='statement' and types[b]=='statement',f'{r["id"]} bad contradict')
        if kind=='about':assure(types[a]=='statement',f'{r["id"]} bad about source')
        if kind=='support':assure(types[b]=='statement',f'{r["id"]} bad support target type')
    for r in ng:
        assure(r['relation_type'] in allowed and r['source_id'] in unitids and r['target_id'] in unitids,f'{r["id"]} negative malformed')
        assure((r['source_id'],r['target_id'],r['relation_type']) not in seen,f'{r["id"]} negative collides positive')
    selections=jl('gold_view_selection.jsonl')
    for x in selections:
        expected=x['expected']
        mode=expected.get('selection_mode')
        assure(mode in {'constrained','flexible','open'},f'{x["id"]} invalid selection mode')
        allowed=expected.get('allowed_view_types',[])
        assure(bool(allowed) and set(allowed)<={'timeline','conflict','hypothesis'},f'{x["id"]} invalid allowed views')
        if mode=='constrained':assure(len(allowed)==1,f'{x["id"]} constrained needs exactly one view')
        if mode=='open':assure(expected.get('content_requirements'),f'{x["id"]} open selection missing content requirements')

    views=jl('gold_view_cases.jsonl')
    for case in views:
        x=case['expected']['reference_payload'];type_=x['view_type']
        assure(type_==case['expected']['view_type'],f'{case["id"]} view mismatch')
        references=set()
        if type_=='conflict':
            ids=[]
            for sec in x['sections']:
                assure(sec['placement'] in {'left','right','center'},f'{case["id"]} bad placement')
                for c in sec['items']:
                    references.add(c['info_unit_id']);ids.append(c['info_unit_id'])
                    assure(c['role'] in {'claim','evidence','event','context'},f'{case["id"]} bad role')
            assure(len(ids)>0,f'{case["id"]} empty conflict view')
        elif type_=='timeline':
            dates=[]
            for e in x['events']:
                references.update(e['related_info_unit_ids'])
                if e['sort_date']:dates.append(e['sort_date'])
                assure(e['time_precision'] in {'day','month','year','unknown'},f'{case["id"]} bad precision')
            assure(dates==sorted(dates),f'{case["id"]} unsorted dates')
        elif type_=='hypothesis':
            steps=x['steps'];stepids={e['id'] for e in steps}
            for s in steps:
                references.update(s['supporting_info_unit_ids']+s['opposing_info_unit_ids'])
                assure(set(s['depends_on_step_ids']) <= stepids-{s['id']},f'{case["id"]} bad step deps')
            def cycle(node,trace):
                assure(node not in trace,f'{case["id"]} hypothesis dependency cycle')
                for d in next(s for s in steps if s['id']==node)['depends_on_step_ids']:cycle(d,trace|{node})
            for sid in stepids:cycle(sid,set())
        else:raise AssertionError(f'{case["id"]} invalid view_type')
        assure(references<=unitids,f'{case["id"]} unknown ref {references-unitids}')
        assure(set(case['expected']['must_include_info_unit_ids'])<=references,f'{case["id"]} required not in fixture')
    laws=js('gold/verified_laws.json');lawids={l['id'] for l in laws}
    assert_unique(laws,'laws')
    for l in laws:
        assure(l['text'] and l['source_url'].startswith('https://www.court.gov.cn/'),f'{l["id"]} law content')
        assure(l['verified_at'] is not None,f'{l["id"]} law not verified')
    # Same law IDs and exact clause text must be served by the actual runtime corpus.
    repo_root=ROOT.parents[1]   # evals/case_a -> repository root
    runtime_path=repo_root/'backend'/'data'/'laws.json'
    assure(runtime_path.is_file(),'missing v0.2 runtime laws.json')
    if runtime_path.is_file():
        runtime=json.loads(runtime_path.read_text(encoding='utf-8'))
        assert_unique(runtime,'runtime laws')
        runtime_by_id={item['id']:item for item in runtime}
        for entry in laws:
            r=runtime_by_id.get(entry['id'])
            assure(r is not None, f'{entry["id"]} missing in backend laws.json')
            assure(all(r.get(k)==entry[k] for k in ('title','article_no','text','source_url')),f'{entry["id"]} runtime law mismatch')
        for entry in runtime:
            assure(set(entry)=={'id','title','article_no','text','source_url','effective_status','verified_at'},f'{entry["id"]} invalid LawEntry fields')
            assure(entry['effective_status'] in {'effective','unknown'},f'{entry["id"]} invalid effective_status')
    for x in jl('gold_law_cases.jsonl'):
        expected=x['expected']
        allids=sum((expected.get(k,[]) for k in ('required_law_ids','acceptable_law_ids','forbidden_law_ids','required_any_of_law_ids','preferred_law_ids')),[])
        assure(set(allids)<=lawids,f'{x["id"]} invalid law ID')
        assure(not (set(expected.get('required_law_ids',[])+expected.get('required_any_of_law_ids',[]))&set(expected.get('forbidden_law_ids',[]))),f'{x["id"]} require/forbid collision')
        assure(set(x['input']['selected_info_unit_ids'])<=unitids,f'{x["id"]} invalid selected info')
    for x in jl('gold_end_to_end.jsonl'):
        src=x['input'].get('source_file_ids',[])
        assure(set(src)<=set(docs),f'{x["id"]} unknown source files')
        exp=x['expected']
        assure(set(exp.get('minimum_info_unit_gold_ids',[])+exp.get('forbidden_info_unit_gold_ids',[]))<=unitids,f'{x["id"]} invalid info id')
        assure(set(exp.get('forbidden_source_file_ids',[]))<=set(docs),f'{x["id"]} invalid forbidden source files')
        assure(set(exp.get('allowed_views',[]))<={'timeline','conflict','hypothesis'},f'{x["id"]} invalid allowed views')
        if 'acceptable_law_ids' in exp:
            assure(set(exp['acceptable_law_ids'])<=lawids and exp.get('minimum_law_count',1)>0,f'{x["id"]} bad open law set')
        assure(set(exp.get('minimum_relation_gold_ids',[]))<={r['id'] for r in rs},f'{x["id"]} invalid relation id')
        if 'append_fixture_path' in x['input']:assure((ROOT/x['input']['append_fixture_path']).is_file(),f'{x["id"]} missing append fixture')
    n=js('tests/test_manifest.json')['counts']
    for key,li in [('gold_info_units',um),('positive_relations',rs),('negative_relations',ng),('view_selection_cases',selections),('view_compositions',views),('law_entries',laws),('law_cases',jl('gold_law_cases.jsonl')),('parse_cases',jl('gold_parse_cases.jsonl')),('end_to_end_cases',jl('gold_end_to_end.jsonl'))]:
        assure(n[key]==len(li),f'wrong manifest count {key}')
    return n
if __name__=='__main__':
    r=validate()
    print('PASS: JSONL / schema / file hashes / spans / relations / view references / laws / scenario IDs')
    for k,v in r.items():print(f'  {k}: {v}')
    print('NOTE: semantic/legal expert review is not automated by this script.')
