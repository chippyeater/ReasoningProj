"""Original file and offline ZIP contracts, using isolated real upload bytes."""
from contextlib import contextmanager
from io import BytesIO
import json
from pathlib import PurePosixPath
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

from support import IsolatedCaseTest
import test_case_materials as fixtures
from app import case_store, main
from app.case_materials_export import safe_original_filename
from app.case_service import create_empty_case
from app.issue_workspaces import get_issue_workspaces, persist_issue_workspaces, workspace_lock


class TestCaseMaterialsExport(IsolatedCaseTest):
    fixture = fixtures.TestCaseMaterials.fixture
    snapshot = fixtures.TestCaseMaterials.snapshot

    def append(self, filename, raw):
        response = self.client.post(f'/api/cases/{self.case.case_id}/files', data={
            'source_kinds': json.dumps({filename: 'document'})}, files={'files': (filename, raw)})
        self.assertEqual(response.status_code, 200, response.text)
        self.case = case_store.load_case(self.case.case_id)
        return self.case.files[-1]

    def export(self):
        return self.client.get(f'/api/cases/{self.case.case_id}/materials.zip')

    def original_url(self, file_id):
        return f'/api/cases/{self.case.case_id}/files/{file_id}/original'

    def test_opens_only_the_original_selected_by_case_and_file_id(self):
        self.fixture()
        expected = case_store.artifact_path(self.case.case_id, self.case.files[0].original_path).read_bytes()
        # Frontend-supplied paths have no role in resolving the file.
        response = self.client.get(self.original_url(self.file_id), params={'path': 'case.json'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, expected)
        self.assertTrue(response.headers['content-type'].startswith('text/plain'))
        self.assertTrue(response.headers['content-disposition'].startswith('inline;'))
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertIn('sandbox', response.headers['content-security-policy'])
        other = create_empty_case('另一个案件')
        case_store.save_case(other, set_current=False)
        self.assertEqual(self.client.get(f'/api/cases/{other.case_id}/files/{self.file_id}/original').status_code, 404)
        self.assertEqual(self.client.get(self.original_url('info-report')).status_code, 404)
        self.assertEqual(self.client.get(self.original_url('unknown-file')).status_code, 404)

    def test_supported_files_inline_and_active_or_unsupported_types_download(self):
        self.fixture()
        for filename, raw, mime, disposition in [
            ('image.png', b'PNG bytes', 'image/png', 'inline'),
            ('scan.pdf', b'not a parsed PDF', 'application/pdf', 'inline'),
            ('evidence.docx', b'original office bytes', 'application/octet-stream', 'attachment'),
            ('untrusted.html', b'<script>window.bad=1</script>', 'application/octet-stream', 'attachment'),
            ('untrusted.svg', b'<svg onload="window.bad=1"/>', 'application/octet-stream', 'attachment'),
        ]:
            with self.subTest(filename=filename):
                file = self.append(filename, raw)
                response = self.client.get(self.original_url(file.file_id))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, raw)
                self.assertEqual(response.headers['content-type'].split(';')[0], mime)
                self.assertTrue(response.headers['content-disposition'].startswith(disposition + ';'))

    def test_zip_contains_every_original_exactly_once_and_a_path_free_manifest(self):
        self.fixture()
        self.append('material.txt', b'same name, different ID')
        failed = self.append('broken.docx', b'office-original-unchanged')
        unsupported = self.append('binary.bin', b'\x00\xff\x13raw')
        self.append('中文 空格 #100% +.txt', '特殊文件名'.encode())
        self.assertEqual(failed.parse_status, 'failed')
        self.assertEqual(unsupported.parse_status, 'unsupported')
        before = self.snapshot()
        with patch('app.llm_client.call_structured', side_effect=AssertionError('No LLM')), \
             patch('app.llm_client.call_tools', side_effect=AssertionError('No LLM')):
            response = self.export()
        self.assertEqual(response.status_code, 200, response.text[:200] if response.status_code != 200 else '')
        self.assertEqual(response.headers['content-type'], 'application/zip')
        self.assertIn('case-materials.zip', response.headers['content-disposition'])
        self.assertEqual(self.snapshot(), before)
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['case_id'], self.case.case_id)
            self.assertEqual(manifest['case_title'], self.case.case_title)
            self.assertEqual(len(manifest['files']), len(self.case.files))
            names = ['index.html', 'manifest.json'] + [item['zip_path'] for item in manifest['files']]
            self.assertEqual(archive.namelist(), names)
            self.assertEqual(len(names), len(set(names)))
            for item, file in zip(manifest['files'], self.case.files):
                self.assertEqual(item, dict(file_id=file.file_id, original_filename=file.filename,
                    zip_path=f'sources/{file.file_id}/{safe_original_filename(file.filename)}',
                    file_type=file.file_type.value, source_kind=file.source_kind.value, parse_status=file.parse_status.value))
                self.assertEqual(archive.read(item['zip_path']),
                    case_store.artifact_path(self.case.case_id, file.original_path).read_bytes())
            self.assertNotIn(str(self.data_dir), archive.read('manifest.json').decode())
            self.assertNotIn('original_path', archive.read('manifest.json').decode())
            self.assertFalse(any(name.startswith(('parsed/', 'runs/')) or name in ('case.json', 'issue_workspaces.json') for name in names))

    def test_preview_and_offline_html_share_six_chapters_with_correct_source_links(self):
        url = self.fixture()
        file = self.append('中文 #100% +.txt', b'special name')
        preview = fixtures.Document(self.client.get(url).text)
        with ZipFile(BytesIO(self.export().content)) as archive:
            html = archive.read('index.html').decode()
            offline = fixtures.Document(html)
            manifest = json.loads(archive.read('manifest.json'))
        self.assertEqual(offline.sections, preview.sections)
        preview_links = [a for tag, a in preview.tags if a.get('class') == 'original-link']
        offline_links = [a for tag, a in offline.tags if a.get('class') == 'original-link']
        self.assertEqual(len(preview_links), len(offline_links))
        self.assertGreater(len(offline_links), len(self.case.files))  # directory + source sidebar
        self.assertEqual({urlsplit(a['href']).path for a in preview_links},
            {self.original_url(f.file_id) for f in self.case.files})
        self.assertEqual({unquote(a['href']) for a in offline_links}, {f['zip_path'] for f in manifest['files']})
        self.assertTrue(any('%23' in a['href'] and '%25' in a['href'] for a in offline_links))
        self.assertFalse(any(urlsplit(a['href']).scheme or a['href'].startswith('/')
                             for a in offline_links))
        self.assertNotIn('/api/', html)
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)
        self.assertEqual(len([tag for tag, _ in offline.tags if tag == 'script']), 1)
        self.assertFalse(any(tag in {'link', 'iframe', 'img'} or (tag == 'script' and 'src' in a)
                             for tag, a in offline.tags))
        self.assertTrue(all(a['target'] == '_blank' and a['rel'] == 'noopener noreferrer' for a in offline_links))
        self.assertIn(file.file_id, offline.sections['files'])

    def test_missing_original_fails_entire_export_and_file_read_without_absolute_path_leaks(self):
        self.fixture()
        unlinked = self.append('unlinked.bin', b'not optional')
        case_store.artifact_path(self.case.case_id, unlinked.original_path).unlink()
        before = self.snapshot()
        for url in [self.original_url(unlinked.file_id), f'/api/cases/{self.case.case_id}/materials.zip']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['detail']['code'], 'original_file_missing')
            self.assertIn(unlinked.file_id, response.text)
            self.assertNotIn(str(self.data_dir), response.text)
        self.assertEqual(self.snapshot(), before)

    def test_export_ignores_parsed_caches_and_includes_empty_case(self):
        self.fixture()
        for file in self.case.files:
            case_store.artifact_path(self.case.case_id, file.parsed_text_path).unlink()
        with patch.object(case_store, 'read_parsed_document', side_effect=AssertionError('No cache reads')):
            self.assertEqual(self.export().status_code, 200)
        blank = create_empty_case('空案件')
        case_store.save_case(blank, set_current=False)
        response = self.client.get(f'/api/cases/{blank.case_id}/materials.zip')
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ['index.html', 'manifest.json'])
            self.assertEqual(json.loads(archive.read('manifest.json'))['files'], [])

    def test_unsafe_names_are_single_portable_components_and_keep_original_manifest_names(self):
        self.fixture()
        names = ['../../outside.txt', r'C:\secret\file.txt', 'CON.txt', 'NUL', '..',
                 '<script>#%?.txt', '尾部. ', '换行\r\n.txt', '字' * 300 + '.txt']
        for name in names:
            with self.subTest(name=name):
                file = self.case.files[0]
                file.filename = name
                case_store.save_case(self.case)
                with ZipFile(BytesIO(self.export().content)) as archive:
                    item = json.loads(archive.read('manifest.json'))['files'][0]
                    self.assertEqual(item['original_filename'], name)
                    path = PurePosixPath(item['zip_path'])
                    self.assertEqual(path.parts[:2], ('sources', file.file_id))
                    self.assertEqual(len(path.parts), 3)
                    self.assertNotIn('..', path.parts)
                    self.assertFalse(path.is_absolute())
                    self.assertLessEqual(len(path.name.encode('utf-8')), 200)
                    self.assertFalse(any(char in path.name for char in '\\<>:"|?*\r\n'))
                    self.assertFalse(path.name.endswith(('.', ' ')))

    def test_invalid_file_ids_and_escaping_original_paths_are_rejected(self):
        self.fixture()
        for file_id in ['bad:id', 'CON', 'file.id', 'bad id', 'x' * 129]:
            with self.subTest(file_id=file_id):
                self.assertEqual(self.client.get(self.original_url(file_id)).status_code, 422)
        # Test a stored invalid ID without weakening the business schema.
        payload = self.case.model_dump(mode='json')
        bad_id = '../escape'
        payload['files'][0]['file_id'] = bad_id
        for unit in payload['info_units']:
            for ref in unit['source_refs']:
                ref['file_id'] = bad_id
        path = case_store.case_dir(self.case.case_id) / 'case.json'
        case_store.write_json(path, payload)
        response = self.export()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['detail']['code'], 'invalid_file_id')
        payload['files'][0]['original_path'] = '../secret.txt'
        case_store.write_json(path, payload)
        self.assertEqual(self.export().status_code, 409)

    def test_case_insensitive_file_id_directory_collision_is_explicit(self):
        self.fixture()
        self.case.files.append(self.case.files[0].model_copy(update={'file_id': self.file_id.upper()}))
        case_store.save_case(self.case)
        response = self.export()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'unsafe_zip_path')

    def test_snapshot_and_original_reads_remain_inside_existing_workspace_lock(self):
        self.fixture()
        stale = self.case.model_copy(deep=True)
        inside = False

        @contextmanager
        def locked(case):
            nonlocal inside
            with workspace_lock(case):
                # A change completed before acquiring the export lock must be
                # read afresh, instead of packaging the initial unlocked read.
                latest = case.model_copy(deep=True)
                latest.case_title = '锁内最新案件名称'
                case_store.save_case(latest)
                state = get_issue_workspaces(latest)
                state.workspaces[self.b].reasoning.hypotheses[0].content = '锁内最新命题'
                persist_issue_workspaces(latest, state)
                inside = True
                try:
                    yield
                finally:
                    inside = False

        from app.case_materials_export import build_case_materials_zip
        def checked(case, state):
            self.assertTrue(inside)
            return build_case_materials_zip(case, state)

        with patch.object(main, 'workspace_lock', side_effect=locked), \
             patch.object(main, 'build_case_materials_zip', side_effect=checked):
            response = self.export()
        self.assertEqual(response.status_code, 200)
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertEqual(json.loads(archive.read('manifest.json'))['case_title'], '锁内最新案件名称')
            self.assertIn('锁内最新命题', archive.read('index.html').decode())
        self.assertEqual(stale.case_title, '租赁争议材料包')
