"""One configured first-round request; no production writes, retries or consolidation.

Reuse a reviewed historical input snapshot to hold IDs/order/title constant, but
read all text from the current fixed TXT and check the upload source metadata.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import ValidationError, create_model

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / 'backend'))
from app import case_store, llm_client
from app.case_service import now_iso
from app.errors import AppError
from app.extraction import ModelExtraction, parse_model_extraction
from app.schemas import CaseData, CaseFile, ExtractionOutput, InfoUnit, ValidationInput, WorkspaceState
from app.validation import locate_source_ref, validate_extraction
from gold_basis import current_gold_basis


def save(path, value):
    case_store.write_json(path, llm_client.redact(value))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def prepare(reference_path: Path, runtime: Path):
    reference = json.loads(reference_path.read_text(encoding='utf-8'))
    original = reference['model_input']
    manifest = json.loads((ROOT / 'fixtures/file_manifest.json').read_text(encoding='utf-8'))
    by_name = {f['filename']: f for f in manifest['files']}
    if len(original['files']) != len(by_name) or len({f['filename'] for f in original['files']}) != len(by_name):
        raise ValueError('Reference must contain all fixed documents exactly once')
    if original['case_title'] != manifest['case_title']:
        raise ValueError('Case title differs from fixed metadata')
    model_input = copy.deepcopy(original)
    files = []
    remap = {}
    stamp = now_iso()
    case_id = 'case-first-round-evaluation'
    for f in model_input['files']:
        metadata = by_name[f['filename']]
        raw = (ROOT / metadata['fixture_text_path']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != metadata['text_sha256'] or raw.decode('utf-8') != f['text']:
            raise ValueError('Fixed TXT does not match the reference request')
        if model_input['confirmed_source_kinds'][f['file_id']] != metadata['source_kind']:
            raise ValueError('Confirmed source kind differs from manifest')
        f['text'] = raw.decode('utf-8')
        parsed_path = f"parsed/{f['file_id']}.txt"
        target = runtime / case_id / parsed_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        files.append(CaseFile(file_id=f['file_id'], filename=f['filename'], file_type='docx',
                              source_kind=metadata['source_kind'], file_size=(ROOT / 'fixtures/raw' / f['filename']).stat().st_size,
                              original_path=f"upload_files/{f['file_id']}.docx", parsed_text_path=parsed_path,
                              parse_status='parsed', error_message=None, uploaded_at=stamp))
        remap[f['file_id']] = metadata['file_id']
    if model_input != original:
        raise ValueError('Request input changed')
    case = CaseData(case_id=case_id, case_title=original['case_title'], files=files,
                    core_issue=None, issue_set=[], info_units=[], relations=[], reasoning_views=[], law_matches=[],
                    workspace_state=WorkspaceState(selected_node_ids=[], focused_node_id=None, current_view_id=None,
                        node_positions={}, display_levels={}, viewport={'zoom': 1, 'offset_x': 0, 'offset_y': 0}),
                    stage_runs=[], created_at=stamp, updated_at=stamp)
    case_store.DATA_DIR = runtime
    return reference, model_input, case, remap


def validate_raw(content: str, case: CaseData, evaluation_id: str, ids=None, created=None):
    # Same field schema, without root validators that JSON Schema cannot express.
    wire = create_model('WireModelExtraction', __config__=ModelExtraction.model_config,
                        **{k: (f.annotation, f) for k, f in ModelExtraction.model_fields.items()})
    wire_schema = wire.model_json_schema()
    actual_schema = copy.deepcopy(ModelExtraction.model_json_schema())
    wire_schema.pop('title'); actual_schema.pop('title')
    if wire_schema != actual_schema:
        raise ValueError('Schema-only projection no longer equals provider schema')
    result = {'wire_schema_validator': 'Pydantic field-schema projection, identical JSON Schema except root title',
              'json_schema_valid': False, 'model_contract_valid': False, 'extraction_contract_valid': False,
              'source_location_valid': False, 'errors': [], 'semantic_rewrites': 0, 'merges': 0}
    try:
        raw = json.loads(content)
        wire.model_validate_json(content, strict=True)
        result['json_schema_valid'] = True
    except (ValueError, TypeError) as exc:
        result['errors'].append({'stage': 'json_or_field_schema', 'message': str(exc)})
        return None, result
    try:
        parse_model_extraction(content)
        result['model_contract_valid'] = True
    except AppError as exc:
        result['errors'].append({'stage': 'model_contract', 'code': exc.code, 'details': getattr(exc, 'errors', [])})
    candidate = {k: copy.deepcopy(raw[k]) for k in ('core_issue', 'issue_set', 'info_units')}
    stamp = created or now_iso()
    files = {f.file_id: f for f in case.files}
    source_errors = []
    for index, unit in enumerate(candidate['info_units']):
        unit['id'] = ids[index] if ids else f'info-{uuid5(NAMESPACE_URL, evaluation_id + ":" + str(index)).hex}'
        unit['created_at'] = stamp
        refs = []
        for ref_index, ref in enumerate(unit['source_refs']):
            try:
                if ref['file_id'] not in files:
                    raise AppError('unknown_source_file', 'Referenced file is outside this case')
                refs.append(locate_source_ref(case.case_id, files[ref['file_id']], ref['quote']).model_dump(mode='json'))
            except AppError as exc:
                refs.append(copy.deepcopy(ref))  # Keep the unlocatable quote; never invent offsets.
                source_errors.append({'index': index, 'reference_index': ref_index, 'code': exc.code, 'message': str(exc)})
        unit['source_refs'] = refs
    result['source_errors'] = source_errors
    result['source_location_valid'] = not source_errors
    result['core_issue_in_issue_set'] = candidate['core_issue'] is None or candidate['core_issue'] in candidate['issue_set']
    result['invalid_stance_targets'] = [{'index': i, 'value': u['stance_target']} for i, u in enumerate(candidate['info_units'])
                                      if u['stance_target'] is not None and u['stance_target'] not in candidate['issue_set']]
    try:
        output = ExtractionOutput.model_validate(candidate)
        checks = validate_extraction(case.case_id, ValidationInput(extraction=output, parsed_files=case.files))
        if not checks.valid:
            raise ValueError(str(checks.errors))
        result['extraction_contract_valid'] = True
    except (ValidationError, ValueError) as exc:
        result['errors'].append({'stage': 'extraction_contract', 'message': str(exc)})
    result['accepted'] = all(result[k] for k in ('json_schema_valid','model_contract_valid','extraction_contract_valid','source_location_valid'))
    result['unit_count'] = len(candidate['info_units'])
    result['source_ref_count'] = sum(len(u['source_refs']) for u in candidate['info_units'])
    result['ids_unique'] = len({u['id'] for u in candidate['info_units']}) == len(candidate['info_units'])
    for original, materialized in zip(raw['info_units'], candidate['info_units']):
        for key in original:
            expected = [{'file_id': r['file_id'], 'quote': r['quote']} for r in materialized[key]] if key == 'source_refs' else materialized[key]
            if original[key] != expected:
                raise ValueError('Materialization changed model-owned semantic content')
    return candidate, result


async def run(output: Path, reference_path: Path):
    output.mkdir(parents=True, exist_ok=False)
    reference, model_input, case, remap = prepare(reference_path, output / 'runtime')
    prompt_path = ROOT.parents[1] / 'backend/prompts/extraction_system_prompt.md'
    prompt = prompt_path.read_text(encoding='utf-8')
    if not prompt.strip():
        raise ValueError('Extraction Prompt is empty')
    config = llm_client.load_config()
    if config.provider != 'azure' or config.model != 'gpt-5-mini':
        raise ValueError('Expected the existing Azure gpt-5-mini deployment')
    if config.public_metadata() != reference['configuration']:
        raise ValueError('Configuration differs from historical first-round control')
    schema = ModelExtraction.model_json_schema()
    if schema != reference['schema']:
        raise ValueError('Schema differs from historical first-round control')
    evaluation_id = 'eval-first-' + uuid4().hex
    report = {'evaluation_id': evaluation_id, 'stage': 'extraction_first_round_only',
              'prompt_version': 'v0.2-extraction-7.2', 'prompt_version_source': 'user-designated evaluation version; actual file hash recorded',
              'configuration': config.public_metadata(), 'endpoint': config.base_url + '/chat/completions',
              'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
              'prompt_file_sha256': hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
              'schema_sha256': digest(schema), 'gold_basis': current_gold_basis(), 'started_at': now_iso(),
              'model_request_attempts': 0, 'observed_http_requests': 0,
              'consolidation_calls': 0, 'production_case_saved': False,
              'input_equal_to_reference': model_input == reference['model_input'], 'reference_input': str(reference_path),
              'schema_equal_to_reference': True, 'configuration_equal_to_reference': True}
    save(output / 'input.json', {'model_input': model_input, 'system_prompt': prompt, 'schema': schema,
                                 'configuration': config.public_metadata(), 'reference_input': str(reference_path)})
    save(output / 'fixture_id_mapping.json', remap)
    # This matches the unmodified call_structured implementation; headers/keys are not saved.
    save(output / 'request_body_constructed.json', {'model': config.model, 'messages': [
        {'role': 'system', 'content': prompt}, {'role': 'user', 'content': json.dumps(model_input, ensure_ascii=False)}],
        'response_format': {'type': 'json_schema', 'json_schema': {'name': 'logicweaver_extraction', 'strict': True, 'schema': schema}},
        'max_completion_tokens': config.max_completion_tokens})
    report.update(status='request_started', model_request_attempts=1)
    save(output / 'run_report.json', report)
    start = time.perf_counter()
    original_client = llm_client.httpx.AsyncClient

    async def observe_request(request):
        report['observed_http_requests'] += 1
        if report['observed_http_requests'] != 1:
            raise RuntimeError('Evaluation permits only one HTTP request')
        actual = json.loads(request.content)
        constructed = json.loads((output / 'request_body_constructed.json').read_text(encoding='utf-8'))
        if actual != constructed:
            raise RuntimeError('Actual request differs from saved input/schema/strict controls')
        save(output / 'actual_http_request.json', {'url': str(request.url), 'method': request.method,
              'header_names_only': sorted(request.headers), 'body': actual,
              'matches_constructed_body': True})

    def observed_client(**kwargs):
        # Pass-through observation of the real client, not a mock transport.
        return original_client(**kwargs, event_hooks={'request': [observe_request]})

    llm_client.httpx.AsyncClient = observed_client
    try:
        reply = await llm_client.call_structured(config, prompt, model_input, schema)
    except llm_client.LLMFailure as exc:
        report.update(status='llm_failed', error_code=exc.code, error_message=str(exc), response=exc.response)
    except Exception as exc:
        report.update(status='evaluation_request_failed', error_type=type(exc).__name__, error_message=str(exc))
    else:
        report.update(response_model=reply.response.get('model'), usage=reply.response.get('usage'))
        save(output / 'provider_response.json', reply.response)
        (output / 'raw_model_content.txt').write_text(reply.content, encoding='utf-8')
        report['llm_elapsed_seconds'] = round(time.perf_counter() - start, 3)
        try:
            candidate, validation = validate_raw(reply.content, case, evaluation_id)
            save(output / 'validation_result.json', validation)
            if candidate is not None:
                save(output / 'raw_model_output.json', json.loads(reply.content))
                save(output / 'candidate_with_backend_metadata.json', candidate)
                scoring = copy.deepcopy(candidate)
                for unit in scoring['info_units']:
                    for ref in unit['source_refs']:
                        if ref['file_id'] in remap:
                            ref['file_id'] = remap[ref['file_id']]
                save(output / 'prediction_for_scoring.json', scoring)
                if validation['accepted']:
                    save(output / 'prediction.json', candidate)
            report.update(status='validated' if validation.get('accepted') else 'contract_rejected',
                          raw_unit_count=validation.get('unit_count'), validation=validation)
        except Exception as exc:
            report.update(status='evaluation_validation_failed', error_type=type(exc).__name__, error_message=str(exc))
    finally:
        llm_client.httpx.AsyncClient = original_client
    report.update(elapsed_seconds=round(time.perf_counter() - start, 3), completed_at=now_iso())
    save(output / 'run_report.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--reference-input', required=True, type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args.output.resolve(), args.reference_input.resolve()))
    print(json.dumps({k: report.get(k) for k in ('evaluation_id','status','raw_unit_count','response_model','usage','elapsed_seconds')}, indent=2))
    raise SystemExit(0 if report['status'] == 'validated' else 1)
