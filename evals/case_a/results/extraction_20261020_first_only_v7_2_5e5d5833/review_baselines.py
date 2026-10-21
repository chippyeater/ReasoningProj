"""Offline, explicit assistant semantic review under the current v1.7 policy.

No old alignment is imported, no LLM is invoked, no model text is altered.
"""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
CASE = HERE.parents[1]
sys.path.insert(0, str(CASE / 'tests'))
from gold_basis import current_gold_basis
from run_first_round_eval import prepare, validate_raw, save

RESULTS = CASE / 'results'
REFERENCE = RESULTS / 'extraction_20261020_first_only_5875b93d/input.json'
SOURCES = {
    'v6': RESULTS / 'extraction_20261020_v6/first_round_evaluation',
    'v7': RESULTS / 'extraction_20261020_first_only_e5b00709',
    'v7.1': RESULTS / 'extraction_20261020_first_only_5875b93d',
}
ORDER = ['U001','U002','U004','U006','U008','U010','U012','U015','U016','U019','U022','U026','U027','U029','U030','U035','U036']
# Entries: unchanged raw prediction index, whether its core content receives a match, review finding.
REVIEW = {
 'v6': [
  (0,True,'Landlord identity present; clean identity unit.'),
  (1,True,'Tenant identity present; appended actual payment duplicates unit 4 and pollutes the identity role.'),
  (3,False,'Partial: lease dates present but monthly rent 3000 absent from structured content.'),
  (None,False,'Missing: contractual deposit amount 2000 not expressed; actual transfer is not the agreed amount.'),
  (2,False,'Partial: early-termination no-refund mentioned; normal-expiry/no-breach full-refund condition absent from content.'),
  (4,True,'Transfer date, payer, payee, amount and deposit note present; original bank evidence is not verified.'),
  (12,True,'Possible early move due to work change is present, but combined with landlord conditional negotiation; speaker is null.'),
  (12,False,'Conditional negotiation appears but shares this multi-party node with U012; one-to-one allocation to U012 blocks a second match. Vague later reply is not required.'),
  (13,True,'Already-found willing friend is explicit. A vague landlord reply may remain as negotiation context; it does not itself require splitting. Repeated finding-a-friend also occurs in unit 10.'),
  (8,True,'Landlord overall refusal and its contractual justification are present across units 5/8/14, fragmented rather than integrated. Core stance matches once.'),
  (9,True,'Self-reported March 5 move-out is explicit. Cleaning/no damage are useful handover attributes, not an automatic overmerge.'),
  (10,True,'Self-reported March 8 move-in present; finding a friend and later lease are also bundled. Match only move-in.'),
  (10,False,'Later separate lease is explicit, with no invented date, but same node already allocated to U026; not an independent unit.'),
  (11,True,'Tenant no-loss assessment present; deposit remedy is an independent reasoning question bundled in this same unit.'),
  (11,False,'Do-not-deduct-all remedy is explicit but same unit used for U029; no duplicate credit.'),
  (7,True,'Vacancy over several days is present; immediate March 5 vacancy also appears in unit 14. These time-qualified sources may be integrated; their time difference alone is not an error.'),
  (7,False,'Claim of actual loss is explicit but shares vacancy node U035. Vacancy and existence/extent of loss need distinct verification.'),
 ],
 'v7': [
  (0,True,'Landlord identity present; generic identity-plus-positions summary is organizational pollution, not a second fact unit.'),
  (1,True,'Tenant identity present; identity unit also summarizes moving, replacement and no-damage positions.'),
  (2,False,'Lease dates only in quote, not structured content; monthly rent 3000 absent.'),
  (4,False,'Contractually agreed deposit amount 2000 is explicit but combined with performance. Allocate same node to complete transfer U010, not twice.'),
  (3,False,'Early-termination no-refund explicit; normal-expiry/no-breach refund condition absent.'),
  (4,True,'Transfer date, payer/payee, amount and note present; classified state/quantitative despite the payment proposition. Core coverage and classification are reported separately.'),
  (10,True,'Possible early move is present inside full-chat summary. Work-change reason is only in quote, so detailed completeness remains partial.'),
  (10,False,'Conditional proposal explicit but full multi-party chat already allocated to U012; deleted vague reply is not a scoring requirement.'),
  (6,False,'Already-found friend is present in units 6 and 10, both allocated to independent U026 and U012. This is merge-blocked coverage, not an absent fact.'),
  (11,True,'Landlord refusal justified by contract is explicit; partial stance repetition in units 8/10, not forbidden mixing of fact and reason per se.'),
  (5,True,'Self-reported March 5 move-out present; cleaning/no damage form useful handover context, not a mechanical split requirement.'),
  (6,True,'Self-reported March 8 move-in present. Finding replacement and later separate lease are bundled.'),
  (6,False,'Later separate lease explicit, undated, but shares node with U026; no second credit.'),
  (7,True,'No-loss assessment explicit, combined with remedy request; independent reasoning questions remain entangled.'),
  (7,False,'Do-not-deduct-all remedy present but shares U029 node.'),
  (9,True,'Days of vacancy and current vacancy can form a useful composite; March 5 temporal label missing. Loss assertion in the same unit is a separate verification matter.'),
  (9,False,'Loss assertion present but same unit already used for vacancy U035.'),
 ],
 'v7.1': [
  (0,True,'Landlord identity present; identity card also bundles refusal, vacancy/loss and finding-a-tenant positions.'),
  (1,True,'Tenant identity present; identity card bundles payment, move-out, replacement, later lease and remedy. Do not allocate this polluted identity to additional Gold.'),
  (3,False,'Lease dates present, monthly rent 3000 missing. Added inference about early leaving exceeds the cited lease-date fragment.'),
  (4,True,'Contractual deposit 2000 explicit; independent transfer unit 6 allows separate U010 match. This unit still repeats actual payment, so coverage does not prove clean separation.'),
  (5,False,'Early-termination no-refund provision present; normal-expiry/no-breach full-refund condition absent.'),
  (6,True,'Independent transfer node includes date, parties, 2000 and note; payment repeated in unit 4.'),
  (13,True,'Work change and possible early move dated February 25 present. Willingness to help find someone may remain context, not a required standalone node.'),
  (14,True,'Independent attributed conditional negotiation proposal is sufficient under v1.7; no penalty for omitting vague March 1 reply.'),
  (11,False,'Already-found friend present but grouped with move-in and later lease in same node allocated to U026; cannot substitute willingness to help in unit 13.'),
  (7,True,'Overall contractual refusal stance explicit but fact_assertion classification is wrong and quoted fragment omits the reasonable-assessment wording; repeated in unit 15.'),
  (10,True,'Self-reported March 5 move-out present; cleaning/no damage may be useful handover attributes.'),
  (11,True,'March 8 move-in explicit; allocation to U026 does not also credit found-friend or later-lease propositions.'),
  (11,False,'Later lease explicit and undated but combined with U026 in one node.'),
  (12,True,'No-loss assessment present, based on short vacancy; same node bundles deposit remedy.'),
  (12,False,'Do-not-deduct-all remedy present but node allocated to U029.'),
  (9,True,'Several-day vacancy present; immediate March 5 vacancy elsewhere in unit 15. Multi-time integration is allowed but not completed.'),
  (9,False,'Actual-loss claim present but same node used for U035; existence/amount of loss needs independent verification.'),
 ],
}
FINDINGS = {
 'v6': {'missing_content':['U006'],'partial_content':['U004','U008'],
  'merge_blocked':['U015','U027','U030','U036'],
  'harmful_merge_groups':[
    {'indices':[12],'gold_ids':['U012','U015'],'reason':'Tenant possible departure and landlord conditional negotiation need separate attribution and verification.'},
    {'indices':[10],'gold_ids':['U016','U026','U027'],'reason':'Finding, move-in, later lease are independently verifiable; found-friend is available separately in unit 13, later lease is not.'},
    {'indices':[11],'gold_ids':['U029','U030'],'reason':'Loss assessment and deposit remedy concern independent reasoning questions.'},
    {'indices':[7],'gold_ids':['U035','U036'],'reason':'Vacancy alone does not establish actual loss.'}],
  'fragmented_stance_groups':[[5,8,14]],'substantive_redundancy_groups':[[5,8,14],[10,13]],
  'identity_pollution_indices':[1], 'classification_indices':[12,13,14],
  'source_semantic_findings':[
    {'indices':[12,13,14],'kind':'attribution_metadata','detail':'Communication events have speaker=null while their content carries parties factual positions; direct text still names speakers.'},
    {'indices':[14,7],'kind':'time_scope','detail':'March 5 current vacancy and later several-day vacancy remain distributed; no known exact vacancy range can be inferred.'},
    {'indices':[10,9],'kind':'claim_not_verified','detail':'Move-out, move-in and later lease remain Zhang testimony; repeated Zhang sources are not independent confirmation.'}]},
 'v7': {'missing_content':[],'partial_content':['U004','U008'],
  'merge_blocked':['U006','U015','U016','U027','U030','U036'],
  'harmful_merge_groups':[
    {'indices':[4],'gold_ids':['U006','U010'],'reason':'Contractual obligation and actual performance must be independently checked.'},
    {'indices':[10],'gold_ids':['U012','U015','U016'],'reason':'Whole-chat summary combines different parties and independent facts/proposal; two critical facts cannot receive independent credit.'},
    {'indices':[6],'gold_ids':['U016','U026','U027'],'reason':'Finding, actual move-in and later separate contract are independently verifiable.'},
    {'indices':[7],'gold_ids':['U029','U030'],'reason':'Loss assessment and deposit remedy have different reasoning purposes.'},
    {'indices':[9],'gold_ids':['U035','U036'],'reason':'Vacancy is not proof of actual loss.'}],
  'fragmented_stance_groups':[[8,10,11]],'substantive_redundancy_groups':[[8,10,11],[6,10]],
  'identity_pollution_indices':[0,1], 'classification_indices':[4],
  'source_semantic_findings':[
    {'indices':[1],'kind':'citation_coverage','detail':'Identity cites contract and move-out paragraph, but content also claims replacement found and short vacancy from other paragraphs not cited here.'},
    {'indices':[10],'kind':'attribution_metadata','detail':'Whole chat has speaker=null; wording identifies parties but per-node attribution cannot represent one coherent position.'},
    {'indices':[9],'kind':'time_scope','detail':'Current vacancy quote has March 5 context in TXT, but current date not expressed in the node.'},
    {'indices':[4],'kind':'verification_strength','detail':'Transfer text transcription is treated as completed payment without stating original bank evidence remains unverified.'}]},
 'v7.1': {'missing_content':[],'partial_content':['U004','U008'],
  'merge_blocked':['U016','U027','U030','U036'],
  'harmful_merge_groups':[
    {'indices':[4],'gold_ids':['U006','U010'],'reason':'Contractual amount and actual payment conflated, despite separate transfer node 6.'},
    {'indices':[11],'gold_ids':['U016','U026','U027'],'reason':'Finding replacement, move-in and later lease need independent checking.'},
    {'indices':[12],'gold_ids':['U029','U030'],'reason':'No-loss assessment and requested remedy must remain independently addressable.'},
    {'indices':[9],'gold_ids':['U035','U036'],'reason':'Vacancy does not itself establish actual loss.'}],
  'fragmented_stance_groups':[[7,8,15]],'substantive_redundancy_groups':[[7,15],[4,6]],
  'identity_pollution_indices':[0,1], 'classification_indices':[7,8],
  'source_semantic_findings':[
    {'indices':[0],'kind':'citation_coverage','detail':'Only identity and early-termination paragraph cited, while content also asserts vacancy/loss and no duty to find replacement.'},
    {'indices':[1],'kind':'citation_coverage','detail':'Identity and transfer quotes do not support its move-out, move-in, new-contract and remedy content.'},
    {'indices':[3],'kind':'inference_as_content','detail':'Lease-date fragment alone does not establish when Zhang proposed to leave.'},
    {'indices':[7],'kind':'citation_coverage','detail':'Content says refusal is reasonable, but cite omits the final assessment paragraph; actual material supports it elsewhere.'},
    {'indices':[4],'kind':'verification_strength','detail':'Text transcription described as proving actual payment; original transaction evidence not independently verified.'}]},
}


def main():
    basis = current_gold_basis()
    for label, source in SOURCES.items():
        out = HERE / 'baseline_v1_7' / label
        out.mkdir(exist_ok=False)
        reference, model_input, case, remap = prepare(REFERENCE, out / 'runtime')
        raw = json.loads((source / 'raw_model_output.json').read_text(encoding='utf-8'))
        if label == 'v6':
            audit_path = RESULTS / 'extraction_20261020_v6/runtime/case-aaedf9d7fa1845d9b8107d83de0b590d/runs/run-2cfbe9f5ba214ff6b0e72e87cf955cad.output.json'
            content = json.loads(audit_path.read_text(encoding='utf-8'))['model_content']
            original = json.loads((source / 'prediction.json').read_text(encoding='utf-8'))
        else:
            audit_path = source / 'provider_response.json'
            content = json.loads(audit_path.read_text(encoding='utf-8'))['choices'][0]['message']['content']
            fn = 'candidate_with_backend_metadata.json' if label == 'v7' else 'prediction.json'
            original = json.loads((source / fn).read_text(encoding='utf-8'))
        assert json.loads(content) == raw
        candidate, validation = validate_raw(content, case, 'historical-'+label,
            ids=[u['id'] for u in original['info_units']], created=original['info_units'][0]['created_at'])
        assert candidate == original, 'Historical candidate differs after deterministic verification'
        scoring = copy.deepcopy(candidate)
        for unit in scoring['info_units']:
            for ref in unit['source_refs']:ref['file_id'] = remap[ref['file_id']]
        assert scoring == json.loads((source / 'prediction_for_scoring.json').read_text(encoding='utf-8'))
        save(out / 'prediction_for_scoring.json', scoring)
        save(out / 'validation_result.json', validation)
        rows=[]
        for gold_id,(index,matched,comment) in zip(ORDER, REVIEW[label]):
            rows.append({'gold_version':basis['version'],'gold_basis_sha256':basis['sha256'],
                         'gold_id':gold_id,'predicted_id':scoring['info_units'][index]['id'] if index is not None else None,
                         'match':'yes' if matched else 'no','review_comment':comment,
                         'reviewer':'assistant_offline_semantic_review_v1_7',
                         'scope':'accepted_first_round' if validation['accepted'] else 'diagnostic_rejected_raw_candidate'})
        (out / 'alignment.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf-8')
        findings=copy.deepcopy(FINDINGS[label])
        findings.update(gold_basis=basis,scoring_scope=rows[0]['scope'],content_rewritten=False,
                        reasonable_composites={'U008':'Missing normal refund condition; no penalty merely for combining rules.',
                         'U019':'Whole stance and its reasons may be grouped; fragmentation is recorded.',
                         'U035':'Different-time vacancy sources may be grouped; loss assertion remains independently verifiable.'},
                        exact_duplicate_nodes=0,unsupported_wholly_invented_nodes=0,
                        important_note='Repeated-person testimony is not independent proof. Core matching does not certify all details, classification or source completeness.')
        for key in ('harmful_merge_groups','source_semantic_findings'):
            for item in findings[key]:item['predicted_ids']=[scoring['info_units'][i]['id'] for i in item['indices']]
        save(out / 'semantic_review.json',findings)
        save(out / 'provenance.json',{'source_directory':str(source),'provider_content_path':str(audit_path),
             'provider_content_sha256':hashlib.sha256(content.encode('utf-8')).hexdigest(),
             'all_raw_semantics_unchanged':True,'candidate_exactly_equals_original':True,
             'scoring_prediction_exactly_equals_original':True,'original_ids_retained':True,
             'historical_gold_version':'historical-16 / rubric-1.5',
             'historical_gold_info_units_sha256':'3735b786c0f9a1932a55051b049058cefa052a6694166164fcf64e74f9449a24',
             'current_gold_basis':basis,'gaps':[]})
        command=[sys.executable,'-X','utf8','-B',str(CASE/'tests/score_predictions.py'),
                 '--prediction',str(out/'prediction_for_scoring.json'),'--alignment',str(out/'alignment.jsonl'),
                 '--output',str(out/'extraction_score.json')]
        completed=subprocess.run(command,capture_output=True,text=True,encoding='utf-8')
        assert completed.returncode==0,completed.stderr
        score=json.loads((out/'extraction_score.json').read_text(encoding='utf-8'))
        print(label,len(candidate['info_units']),validation['accepted'],score['reviewed_matches'],score['gold_units'],score['reference_invalid'])
    save(HERE/'offline_baseline_complete.json',{'completed':True,'model_calls':0,'gold_basis':basis,
         'reviewed_by':'assistant manual semantic alignment','baselines':list(SOURCES),
         'policy':'Core content with one-to-one matching; missing crucial conditions/rent is partial; do not penalize legitimate composites; attribution, independent reasoning value, completeness and source support reported separately.'})

if __name__=='__main__':main()
