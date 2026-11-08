"""Offline, isolated HTML artifact tests against the saved case/workspace path."""
from html import escape, unescape
from html.parser import HTMLParser
import json
from unittest.mock import patch

from support import IsolatedCaseTest
from app import case_store
from app.case_materials import render_case_materials, SECTIONS
from app.case_service import create_empty_case
from app.issue_workspaces import get_issue_workspaces, persist_issue_workspaces
from app.reasoning_models import EvidenceGap, EvidenceLink, Hypothesis, ReasoningState
from app.schemas import InfoUnit, ReasoningView, SourceRef, TimelineEvent, TimelineView


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.sections, self.tags, self.stack = {}, [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == 'section':
            self.stack.append(attrs['id'])
            self.sections[self.stack[-1]] = ''

    def handle_endtag(self, tag):
        if tag == 'section':
            self.stack.pop()

    def handle_data(self, data):
        if self.stack:
            self.sections[self.stack[-1]] += data


class TestCaseMaterials(IsolatedCaseTest):
    def fixture(self):
        raw = '甲称已付款。乙要求返还押金。银行到账一百元。双方核实已经交付。'
        uploaded = self.upload(raw=raw.encode(), source_kind='communication')
        self.case = case_store.load_case(uploaded['case_id'])
        self.case.case_title = '租赁争议材料包'
        self.case.core_issue = '租金是否支付'
        self.case.issue_set = ['租金是否支付', '押金是否返还']
        self.a, self.b = self.case.issue_set
        self.file_id = self.case.files[0].file_id
        self.now = self.case.updated_at
        ref = SourceRef(file_id=self.file_id, source_kind='communication', quote=raw, start=0, end=len(raw))
        values = dict(source_refs=[ref], side=None, actor=None, target=None, created_at=self.now)
        self.case.info_units = [
            InfoUnit(**values, id='info-report', type='event', subtype='reported', title='付款陈述',
                     content='甲称已经付款，仍待核实。', speaker='甲', related_issue=self.a),
            InfoUnit(**values, id='info-request', type='position', subtype=None, title='返还诉求',
                     content='乙要求返还全部押金。', speaker='乙', related_issue=self.b),
            InfoUnit(**values, id='info-record', type='event', subtype='documented', title='到账记录',
                     content='材料记载银行到账一百元。', speaker=None, related_issue=None),
            InfoUnit(**values, id='info-verified', type='event', subtype='verified', title='交付核实',
                     content='双方核实已经完成交付。', speaker=None, related_issue=None),
        ]
        self.case.reasoning_views = [ReasoningView(id='view-timeline', question='核对事件时间', view_type='timeline',
            selection_reason='已保存时间线', created_at=self.now, payload=TimelineView(view_type='timeline', title='付款过程',
                events=[TimelineEvent(id='time-one', title='付款时间', time_label='2026年10月1日',
                    sort_date='2026-10-01', time_precision='day', summary='付款陈述中的时间。',
                    related_info_unit_ids=['info-report'])]))]
        case_store.save_case(self.case)
        self.state = get_issue_workspaces(self.case)
        self.state.active_issue = self.a
        for issue, judgment in [(self.a, 'unverified'), (self.b, 'verified')]:
            workspace = self.state.workspaces[issue]
            # IDs may coincide across Issue workspaces: association stays local.
            workspace.reasoning = ReasoningState(hypotheses=[Hypothesis(id='hyp-local', content=f'{issue}对应的命题',
                judgment=judgment, updated_at=self.now)])
        ra, rb = [self.state.workspaces[i].reasoning for i in [self.a, self.b]]
        ra.links = [EvidenceLink(id='link-a', hypothesis_id='hyp-local', info_unit_id='info-report',
                                meaning='support', rationale='甲方陈述支持此命题，尚待核实。')]
        rb.links = [EvidenceLink(id='link-b', hypothesis_id='hyp-local', info_unit_id='info-report',
                                meaning='oppose', rationale='甲方陈述与该命题相反。'),
                    EvidenceLink(id='link-c', hypothesis_id='hyp-local', info_unit_id='info-record',
                                meaning='related', rationale='记录相关，但不足以直接支持。')]
        rb.gaps = [EvidenceGap(id='gap-open', content='仍需取得押金转账凭证。', hypothesis_ids=['hyp-local'],
            basis_info_unit_ids=['info-request'], addressing_info_unit_ids=['info-record'], updated_at=self.now),
            EvidenceGap(id='gap-resolved', content='已经解决的交付问题。', hypothesis_ids=['hyp-local'],
                        status='resolved', updated_at=self.now)]
        self.state.workspaces[self.b].status = 'investigating'
        # Canvas visibility cannot filter semantic material out of the artifact.
        for workspace in self.state.workspaces.values():
            workspace.canvas.visible_node_ids = []
        persist_issue_workspaces(self.case, self.state)
        return f'/api/cases/{self.case.case_id}/materials'

    def snapshot(self):
        return {str(p.relative_to(self.data_dir)): p.read_bytes() for p in self.data_dir.rglob('*') if p.is_file()}

    def test_all_six_chapters_and_all_issues_from_saved_state(self):
        url = self.fixture()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('text/html', response.headers['content-type'])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        doc = Document(response.text)
        self.assertEqual(set(doc.sections), {key for key, _ in SECTIONS})
        self.assertIn('押金是否返还对应的命题', doc.sections['issues'])
        self.assertIn('租金是否支付对应的命题', doc.sections['issues'])
        for token in ['已证实命题', '未证实命题', '支持', '反对', '相关', 'link-c', 'info-record', '调查中']:
            self.assertIn(token, doc.sections['issues'])
        self.assertIn('陈述者：乙', doc.sections['overview'])
        self.assertIn('未标定用户身份', doc.sections['overview'])

    def test_real_file_ids_deduplicate_across_issues_and_preserve_unlinked_uploads(self):
        url = self.fixture()
        # Same filename, new original ID: never deduplicate by filename.
        response = self.client.post(f'/api/cases/{self.case.case_id}/files', data={
            'source_kinds': json.dumps({'material.txt': 'document'})},
            files={'files': ('material.txt', b'another file')})
        self.assertEqual(response.status_code, 200, response.text)
        second_id = response.json()['case']['files'][1]['file_id']
        doc = Document(self.client.get(url).text)
        rows = [attrs['data-file-id'] for tag, attrs in doc.tags if 'data-file-id' in attrs]
        self.assertEqual(rows, [self.file_id, second_id])
        self.assertNotIn('info-report', rows)
        self.assertIn(self.a, doc.sections['files'])
        self.assertIn(self.b, doc.sections['files'])
        self.assertIn('尚无关联 InfoUnit', doc.sections['files'])

    def test_statements_never_promoted_by_verified_hypothesis_or_support_link(self):
        url = self.fixture()
        doc = Document(self.client.get(url).text)
        self.assertIn('当事人 / 来源陈述 · 未证实', doc.sections['timeline'])
        self.assertIn('有材料记录 · 仍待核实', doc.sections['timeline'])
        confirmed = doc.sections['preparation'].split('当前已证实的命题')[0]
        self.assertIn('双方核实已经完成交付', confirmed)
        self.assertNotIn('甲称已经付款', confirmed)
        self.assertIn('communication', doc.sections['preparation'])

    def test_event_time_reuses_saved_timeline_never_created_at(self):
        doc = Document(self.client.get(self.fixture()).text)
        self.assertIn('2026年10月1日', doc.sections['timeline'])
        self.assertIn('事件时间未记录', doc.sections['timeline'])
        self.assertNotIn(self.now, doc.sections['timeline'])

    def test_open_gaps_only_with_local_hypothesis_basis_and_addressing(self):
        doc = Document(self.client.get(self.fixture()).text)
        content = doc.sections['gaps']
        for token in ['gap-open', self.b, 'hyp-local', '返还诉求', '到账记录', '未解决 · open', '已解决 1 项']:
            self.assertIn(token, content)
        self.assertNotIn('已经解决的交付问题', content)
        self.assertNotIn('gap-resolved', content)
        self.assertIn('仍需取得押金转账凭证', doc.sections['preparation'])

    def test_regenerate_reads_changes_without_any_writes_or_llm_calls(self):
        url = self.fixture()
        before = self.snapshot()
        with patch('app.llm_client.call_structured', side_effect=AssertionError('No LLM')), \
             patch('app.llm_client.call_tools', side_effect=AssertionError('No LLM')), \
             patch.object(case_store, 'read_parsed_document', side_effect=AssertionError('No parsing')):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.snapshot(), before)
        # Persist an edit through the existing Workspace API.
        response = self.client.patch(f'/api/cases/{self.case.case_id}/issue-reasoning', json={
            'issue': self.b, 'hypothesis': {'id': 'hyp-local', 'content': '最新保存的押金命题', 'judgment': 'refuted'}})
        self.assertEqual(response.status_code, 200, response.text)
        before = self.snapshot()
        doc = Document(self.client.get(url).text)
        self.assertIn('最新保存的押金命题', doc.sections['issues'])
        self.assertIn('已否定命题', doc.sections['issues'])
        self.assertEqual(self.snapshot(), before)

    def test_empty_case_no_workspace_created_and_case_isolation(self):
        self.fixture()
        case = create_empty_case('空白案件')
        case_store.save_case(case, set_current=False)
        before = self.snapshot()
        response = self.client.get(f'/api/cases/{case.case_id}/materials')
        self.assertEqual(response.status_code, 200)
        doc = Document(response.text)
        self.assertEqual(len(doc.sections), 6)
        self.assertIn('暂无可整理的时间线', doc.sections['timeline'])
        self.assertIn('尚无原始上传文件', doc.sections['files'])
        self.assertNotIn('押金', ''.join(doc.sections.values()))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.client.get('/api/cases/nonexistent/materials').status_code, 404)

    def test_unassociated_source_is_explicit_never_substitutes_info_unit_id(self):
        self.fixture()
        # The normal schema prevents broken references. Test the renderer's
        # defensive boundary without weakening or migrating that schema.
        self.case.info_units[0].source_refs = []
        html = render_case_materials(self.case, self.state)
        self.assertIn('来源未关联', html)
        self.assertNotIn('data-file-id="info-report"', html)
        self.case.info_units[0].source_refs = [SourceRef(file_id='foreign-file', source_kind='document',
            quote='文本', start=0, end=2)]
        self.assertIn('引用的文件不在当前 Case 文件目录中', render_case_materials(self.case, self.state))

    def test_untrusted_content_is_text_and_script_is_hash_restricted(self):
        self.fixture()
        attack = '</script><img src=x onerror="window.pwned=1">'
        self.case.case_title = attack
        self.case.files[0].filename = attack
        self.case.files[0].file_id = attack
        self.case.info_units[0].content = attack
        self.case.info_units[0].source_refs[0].file_id = attack
        self.case.info_units[0].id = attack
        self.case.reasoning_views[0].payload.events[0].related_info_unit_ids = [attack]
        for workspace in self.state.workspaces.values():
            for link in workspace.reasoning.links:
                if link.info_unit_id == 'info-report':
                    link.info_unit_id = attack
        html = render_case_materials(self.case, self.state)
        self.assertIn(escape(attack, quote=True), html)
        doc = Document(html)
        self.assertEqual(len([tag for tag, _ in doc.tags if tag == 'script']), 1)
        self.assertFalse(any(tag == 'img' for tag, _ in doc.tags))
        csp = next(attrs['content'] for tag, attrs in doc.tags if attrs.get('http-equiv') == 'Content-Security-Policy')
        self.assertIn("script-src 'sha256-", unescape(csp))
        self.assertNotIn('src="', html)

    def test_corrupt_workspace_is_explicit_failure_and_not_overwritten(self):
        url = self.fixture()
        path = case_store.case_dir(self.case.case_id) / 'issue_workspaces.json'
        path.write_text('broken', encoding='utf-8')
        before = self.snapshot()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'invalid_issue_workspaces')
        self.assertEqual(self.snapshot(), before)
