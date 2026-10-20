"""Identify the current evaluation contract without loading archived Gold."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def current_gold_basis():
    rubric = json.loads((ROOT / 'gold/evaluation_rubric.json').read_text(encoding='utf-8'))
    manifest = json.loads((ROOT / 'tests/test_manifest.json').read_text(encoding='utf-8'))
    if manifest['gold_version'] != rubric['version']:
        raise ValueError('Manifest and rubric Gold versions differ')
    units = [json.loads(line) for line in (ROOT / 'gold/gold_info_units.jsonl').read_text(
        encoding='utf-8').splitlines() if line.strip()]
    if len(units) != manifest['counts']['gold_info_units']:
        raise ValueError('Manifest and actual Gold counts differ')
    files = sorted((ROOT / 'gold').glob('*')) + [ROOT / 'fixtures/file_manifest.json']
    files += sorted((ROOT / 'fixtures/parsed').glob('*.txt'))
    basis = {
        'version': rubric['version'],
        'gold_units': len(units),
        'file_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in files if p.is_file()},
    }
    encoded = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {**basis, 'sha256': hashlib.sha256(encoded).hexdigest()}


if __name__ == '__main__':
    print(json.dumps(current_gold_basis(), ensure_ascii=False, indent=2))
