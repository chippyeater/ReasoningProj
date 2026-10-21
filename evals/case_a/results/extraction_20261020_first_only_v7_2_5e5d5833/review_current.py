"""Offline semantic review of the one returned v7.2 candidate; no network."""
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
CASE = HERE.parents[1]
sys.path.insert(0, str(CASE / 'tests'))
from gold_basis import current_gold_basis
from run_first_round_eval import save

OUT = HERE / 'current_668248a9'
BASIS = current_gold_basis()
PRED = json.loads((OUT / 'prediction_for_scoring.json').read_text(encoding='utf-8'))
UNITS = PRED['info_units']
# No prediction node is split. In this diagnostic alignment, multiple overlapping
# model-generated nodes can carry different Gold, but utility is reviewed separately.
REVIEWS = [
 ('U001',0,True,'Landlord identity supported by contract. Full testimony also cited, but does not make it an independent proof source.'),
 ('U002',1,True,'Tenant identity supported by contract. This identity node improperly also summarizes move-out and replacement/lease claims.'),
 ('U004',2,False,'Both lease dates and monthly rent 3000 now appear, but are inside the same contract-rules node allocated to U008. Not missing content; merge-blocked independent background/amount use.'),
 ('U006',2,False,'Contractually agreed deposit 2000 now appears, distinct from actual transfer in unit 3, but is not an independent node because contract unit 2 already matches U008.'),
 ('U008',2,True,'Both normal-expiry/no-breach full refund and early-termination no refund now explicit. Combining these two rules is appropriate; additional bundling of lease/rent/deposit is reviewed separately.'),
 ('U010',3,True,'Date, payer, payee, amount and deposit note supported by transfer text transcription; this is not independent verification of original banking evidence.'),
 ('U012',6,True,'February 25 possible departure due to work change explicit. Other party conditional proposal shares same communication node; one match only.'),
 ('U015',6,False,'Conditional negotiation proposal explicit in unit 6, but same node allocated to U012. Omission of vague March 1 reply is not penalized.'),
 ('U016',5,False,'Already-found friend explicit in units 1/5, allocated to U002/U027. Unit 6 only says willingness to help, not already found. March 1 chat detail omitted, not replaced by willingness.'),
 ('U019',7,True,'March 5 chat explicitly conveys refusal due to early termination. Overall stance also appears in unit 8; do not score it twice. This choice leaves unit 8 available for its explicit several-day vacancy claim.'),
 ('U022',4,True,'Self-reported March 5 move-out supported by Zhang testimony. Cleaning/no damage can remain useful handover attributes; event/action classification incorrectly reifies testimony, but coverage is separate.'),
 ('U026',10,True,'The model-generated timeline explicitly says Zhang claims March 8 move-in, with a valid Zhang-testimony quote. Its unrelated contract quote is invalid: the entire batch is rejected, and this is diagnostic content credit only. No node was split or repaired.'),
 ('U027',5,True,'Later separate lease explicit in original content; assigned to this existing node, with move-in assigned to existing timeline node 10. This allocation does not mean finding/move-in/signing were cleanly separated. Title/summary wrongly suggest March 8 signing.'),
 ('U029',9,False,'No-loss assessment explicit, but same unit primarily titled deposit-remedy stance and allocated to U030. Still a harmful merge; changing the allocation would swap the credited Gold without adding coverage.'),
 ('U030',9,True,'Do-not-deduct-all remedy explicit as primary stance. Also bundles no-loss assessment, so no second Gold credit.'),
 ('U035',8,True,'Several-day vacancy explicit in Li testimony; March 5 current vacancy also appears in unit 7. Both temporal scopes may be combined; still not integrated into one useful vacancy node. This content match is separate from ideal node organization.'),
 ('U036',8,False,'Loss assertion explicit but unit 8 allocated to U035. Lack of an independently addressable loss claim impairs comparison with the tenant loss assessment.'),
]
rows=[]
for gold,index,match,comment in REVIEWS:
 rows.append({'gold_version':BASIS['version'],'gold_basis_sha256':BASIS['sha256'],
              'gold_id':gold,'predicted_id':UNITS[index]['id'],'match':'yes' if match else 'no',
              'review_comment':comment,'reviewer':'assistant_offline_semantic_review_v1_7',
              'scope':'diagnostic_rejected_raw_candidate'})
(OUT/'alignment.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf-8')
findings={
 'gold_basis':BASIS,'scope':'diagnostic_rejected_raw_candidate','key_core_content_present':17,
 'key_core_content_absent':[], 'merge_blocked_unmatched_gold':['U004','U006','U015','U016','U029','U036'],
 'detailed_completeness_notes':[
   'U016 exact March 1 already-found statement and its communication source are absent; early-March contact claim remains.',
   'U022 precise move-out date is testimony, while March 5 chat says this week; no independent third-party date proof.',
   'U035 immediate vacancy and several-day vacancy are available across nodes, but are not integrated or independently verified.'],
 'reasonable_composites':[
   {'indices':[2],'gold_ids':['U008'],'verdict':'appropriate','detail':'Mutually exclusive refund conditions form one complete useful deposit rule.'},
   {'indices':[8],'gold_ids':['U019'],'verdict':'partly_appropriate','detail':'Contractual justification and overall refusal can be grouped; embedding the only several-day vacancy and loss assertion blocks independent checking.'},
   {'indices':[7,8],'gold_ids':['U035'],'verdict':'allowed_but_not_achieved','detail':'Different-time vacancy sources may be grouped, but remain distributed across a chat summary and broad landlord statement.'},
   {'indices':[4],'gold_ids':['U022'],'verdict':'appropriate_attributes','detail':'Cleaning/no damage are useful move-out handover attributes, not a mandatory sentence-level split.'}],
 'harmful_merge_groups':[
   {'indices':[2],'gold_ids':['U004','U006','U008'],'detail':'Lease/rent for loss calculation, contractual deposit amount and refund rules need separately addressable uses. Error is not merely mixed semantic categories.'},
   {'indices':[5],'gold_ids':['U016','U026','U027'],'detail':'Finding replacement, move-in and later lease have distinct verification evidence; timeline overlap enables two diagnostic matches without resolving this merge.'},
   {'indices':[6],'gold_ids':['U012','U015'],'detail':'Tenant possible departure and landlord conditional proposal need distinct attribution and independent use.'},
   {'indices':[8],'gold_ids':['U019','U035','U036'],'detail':'Refusal stance with reasons may be grouped, but vacancy and actual loss need independent checking; repeated chat stance enables one-to-one allocation, not clean structure.'},
   {'indices':[9],'gold_ids':['U029','U030'],'detail':'No-loss assessment and requested deposit remedy are independent reasoning questions.'}],
 'fragmented_stance_groups':[[7,8]], 'substantive_redundancy_groups':[[7,8],[2,3,4,5,10]],
 'redundant_summary_indices':[10], 'identity_pollution_indices':[1], 'exact_duplicate_nodes':0,
 'classification_definite_error_indices':[4], 'classification_note':'Unit 4 is an event/action even though its source and own content are an unverified first-person assertion; should be statement/fact_assertion. Communication-event labels are not automatically invalid.',
 'source_location':{'references':15,'exact_unique_locations':14,'unlocatable':1,'unknown_file_ids':0,
                    'located_source_kinds_match_upload_metadata':True,'failed_reference':{'index':10,'reference_index':0,'reason':'Contract quote rearranges noncontiguous text and adds blank lines.'}},
 'source_semantic_findings':[
   {'indices':[1,5],'kind':'time_scope','detail':'Title/summary attach new-lease signing to March 8, whereas original content says later; no signing date is established.'},
   {'indices':[7],'kind':'verification_strength','detail':'Title calls March 5 the move-out day; chat itself only says this week. Precise day is Zhang testimony elsewhere, not independently verified.'},
   {'indices':[9],'kind':'citation_coverage','detail':'Summary adds no damage, but this node cites only loss/remedy paragraphs; no-damage is supported elsewhere in TXT, not by its bound reference.'},
   {'indices':[0,1,8],'kind':'overbroad_quote','detail':'Identity nodes cite entire testimony and Li stance pastes entire testimony; exact quote locating succeeds but does not ensure minimal task-oriented organization.'}],
 'wholly_invented_factual_nodes':0, 'invented_quote_count':1,
 'truth_verification':'No party statement, transcript or screenshot transcription was externally corroborated.',
 'allocation_caution':'U026/U027 both score because TWO unchanged, overlapping model nodes carry them. One prediction still matches at most one Gold. This is content coverage, not proof of clean independent units; source failure rejects the whole output.',
 'causal_inference':'Exploratory single-run comparison only; does not establish stable Prompt effects.'}
for key in ('reasonable_composites','harmful_merge_groups','source_semantic_findings'):
 for item in findings[key]:item['predicted_ids']=[UNITS[i]['id'] for i in item['indices']]
save(OUT/'semantic_review.json',findings)
extras=[]
used={x['predicted_id'] for x in rows if x['match']=='yes'}
for u in UNITS:
 if u['id'] not in used:extras.append({'predicted_id':u['id'],'disposition':'duplicate','review_comment':'Overlapping summary; inspect semantic_review.json for source/utility limitations.'})
(OUT/'extra_review.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in extras),encoding='utf-8')
command=[sys.executable,'-X','utf8','-B',str(CASE/'tests/score_predictions.py'),
 '--prediction',str(OUT/'prediction_for_scoring.json'),'--alignment',str(OUT/'alignment.jsonl'),
 '--extra-review',str(OUT/'extra_review.jsonl'),'--output',str(OUT/'diagnostic_extraction_score.json')]
p=subprocess.run(command,capture_output=True,text=True,encoding='utf-8')
assert p.returncode==0,p.stderr
score=json.loads((OUT/'diagnostic_extraction_score.json').read_text(encoding='utf-8'))
print({k:score[k] for k in ('gold_units','predicted_units','reviewed_matches','gold_coverage','reference_invalid')})
