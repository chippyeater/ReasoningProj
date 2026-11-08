import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import { mkdir, mkdtemp, readFile } from 'node:fs/promises';
import { createServer } from 'vite';

const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fileURLToPath(new URL('../..', import.meta.url));
// Reuse the isolated backend fixture and real HTML endpoint. No live case data,
// backend server, model transport, or additional test dependencies are needed.
const fixture = spawnSync(`${root}/backend/.venv/Scripts/python.exe`, ['-c', `
import base64, json, sys
from io import BytesIO
from pypdf import PdfWriter
sys.path[:0] = ['backend', 'backend/tests']
from test_case_materials_export import TestCaseMaterialsExport
from support import docx_bytes
from app.case_service import create_empty_case
from app.case_store import save_case
from app.issue_workspaces import persist_issue_workspaces
t = TestCaseMaterialsExport()
t.setUp()
try:
    url = t.fixture()
    t.append('合同 #100%.docx', docx_bytes('原始合同内容，不改变原件字节。'))
    t.append('原始图像.png', base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aotUAAAAASUVORK5CYII='))
    pdf = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.write(pdf)
    t.append('原始扫描.pdf', pdf.getvalue())
    t.case.files[0].filename = '<img src=x onerror="window.injected=1">.txt'
    t.case.info_units[0].content = '甲称已付款。<img src=x onerror="window.injected=1">'
    save_case(t.case)
    first = t.client.get(url).text
    t.state.workspaces[t.b].reasoning.hypotheses[0].content = '重新生成后的押金命题'
    t.state.workspaces[t.b].reasoning.hypotheses[0].judgment = 'refuted'
    t.state.workspaces[t.b].reasoning.gaps[0].status = 'resolved'
    persist_issue_workspaces(t.case, t.state)
    second = t.client.get(url).text
    archive = t.client.get(f'/api/cases/{t.case.case_id}/materials.zip').content
    originals = {}
    for file in t.case.files:
        response = t.client.get(t.original_url(file.file_id))
        originals[file.file_id] = dict(raw=base64.b64encode(response.content).decode(),
            headers={key: response.headers[key] for key in ('content-type', 'content-disposition', 'content-security-policy', 'x-content-type-options')})
    blank = create_empty_case('空白案件')
    save_case(blank, set_current=False)
    blank_html = t.client.get(f'/api/cases/{blank.case_id}/materials').text
    print(json.dumps(dict(case=t.case.model_dump(mode='json'), state=t.state.model_dump(mode='json'),
        first=first, second=second, blank=blank.model_dump(mode='json'), blank_html=blank_html,
        originals=originals, archive=base64.b64encode(archive).decode())))
finally:
    t.doCleanups()
`], { cwd: root, encoding: 'utf8' });
assert.equal(fixture.status, 0, fixture.stderr);
const data = JSON.parse(fixture.stdout);

test('Case materials: navigation, saved snapshots, sources, failure recovery and mobile reader', { timeout: 120000 }, async t => {
  const server = await createServer({ root: `${root}/frontend`, server: { host: '127.0.0.1', port: 0 } });
  await server.listen();
  let browser;
  try {
    browser = await chromium.launch({ headless: false, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(10000);
    const errors = [], mutations = [], forbidden = [];
    page.on('pageerror', error => errors.push(error.message));
    let caseData = data.case, revision = 'first', fail = false, failZip = false;
    // Context routing also intercepts the first request of a new source tab.
    await page.context().route('**/api/**', async route => {
      const request = route.request(), url = new URL(request.url()), method = request.method();
      if (method === 'OPTIONS') return route.fulfill({ status: 204 });
      if (method !== 'GET') mutations.push([method, url.pathname]);
      const reply = json => route.fulfill({ json });
      if (url.pathname === '/api/case') return reply({ case: caseData });
      if (url.pathname === '/api/cases') return reply({ cases: [data.case, data.blank], current_case_id: caseData.case_id });
      const suffix = url.pathname.replace(/^\/api\/cases\/[^/]+/, '');
      if (suffix === '/agent/latest') return reply({ result: null });
      if (suffix === '/select') { caseData = url.pathname.includes(data.blank.case_id) ? data.blank : data.case; return reply({ case: caseData }); }
      if (suffix === '/issue-workspaces') return reply(caseData === data.blank ? { active_issue: null, workspaces: {} } : data.state);
      if (suffix === '/materials') {
        if (fail) return route.fulfill({ status: 500, json: { detail: '模拟读取失败' } });
        return route.fulfill({ contentType: 'text/html; charset=utf-8', body: caseData === data.blank ? data.blank_html : data[revision] });
      }
      if (suffix === '/materials.zip') {
        if (failZip) return route.fulfill({ status: 409, json: { detail: { code: 'original_file_missing', message: '原始文件缺失：file-missing' } } });
        return route.fulfill({ contentType: 'application/zip', body: Buffer.from(data.archive, 'base64') });
      }
      const original = suffix.match(/^\/files\/([^/]+)\/original$/);
      if (original && data.originals[original[1]]) {
        const file = data.originals[original[1]];
        return route.fulfill({ headers: file.headers, body: Buffer.from(file.raw, 'base64') });
      }
      if (!suffix) return reply({ case: caseData });
      forbidden.push([method, url.pathname]);
      return route.fulfill({ status: 500, json: { detail: 'Unexpected request' } });
    });
    await page.goto(server.resolvedUrls.local[0], { waitUntil: 'domcontentloaded', timeout: 30000 });
    const entry = page.getByRole('button', { name: '案件材料 全部争议问题 ↗' });
    await entry.waitFor();
    await page.waitForFunction(() => !document.querySelector('.workspace-save-state')?.textContent);
    // Rendering or navigation must not generate mutations/agent requests.
    const mutationCount = mutations.length;
    const reader = page.frameLocator('iframe[title="HTML 案件档案"]');
    const chapter = name => reader.getByRole('navigation', { name: '材料包目录' }).getByRole('link', { name, exact: true });
    const open = async () => {
      await entry.click();
      await reader.getByRole('heading', { name: '案件概况', exact: true }).waitFor();
      await page.getByRole('button', { name: '重新生成', exact: true }).waitFor();
    };

    await t.test('opens the actual HTML artifact with all issues, real file IDs and safe content', async () => {
      await open();
      const sandbox = await page.locator('iframe').getAttribute('sandbox');
      assert.match(sandbox, /allow-popups-to-escape-sandbox/);
      assert.doesNotMatch(sandbox, /allow-same-origin/);
      assert.equal(await reader.locator('img').count(), 0);
      assert.match(await reader.locator('.snapshot').innerText(), /覆盖全部 2 个 Issue/);
      assert.equal(await reader.locator('.contents > a').count(), 6);
      assert.equal(await reader.locator('.chapter:visible').count(), 1);
      await chapter('争议问题与证据对应表').click();
      assert.equal(await reader.locator('.issue-group').count(), 2);
      const second = reader.locator('#issue-1');
      assert.match(await second.innerText(), /押金是否返还对应的命题/);
      await second.getByText('证据关联 · 2 项', { exact: true }).click();
      assert.match(await second.innerText(), /反对/);
      assert.match(await second.innerText(), /相关/);
      await chapter('事实时间线').click();
      assert.match(await reader.locator('#timeline').innerText(), /2026年10月1日/);
      assert.match(await reader.locator('#timeline').innerText(), /事件时间未记录/);
      assert.match(await reader.locator('#timeline').innerText(), /当事人 \/ 来源陈述 · 未证实/);
      const event = reader.locator('.timeline-event').filter({ hasText: '付款陈述' });
      await event.getByRole('button', { name: '查看来源与关联 ↗' }).click();
      const sources = reader.getByRole('complementary', { name: '相关证据及文件来源' });
      await sources.waitFor();
      assert.match(await sources.innerText(), new RegExp(data.case.files[0].file_id));
      assert.match(await sources.innerText(), /info-report/);
      assert.match(await sources.innerText(), /租金是否支付/);
      assert.match(await sources.innerText(), /押金是否返还/);
      await sources.locator('.source-panel:visible a[href^="#file-"]').click();
      assert.equal(await reader.locator('#files').isVisible(), true);
      assert.equal(await reader.locator('[data-file-id]').count(), data.case.files.length);
      assert.equal(await reader.locator('[data-file-id]').first().getAttribute('data-file-id'), data.case.files[0].file_id);
      assert.equal(await reader.locator('.file-record').first().getAttribute('open'), '');
      await sources.getByRole('button', { name: '收起证据与来源' }).click();
      await chapter('待补充事项').click();
      assert.match(await reader.locator('#gaps').innerText(), /仍需取得押金转账凭证/);
      assert.doesNotMatch(await reader.locator('#gaps').innerText(), /已经解决的交付问题/);
      await chapter('协商准备摘要').click();
      assert.match(await reader.locator('#preparation').innerText(), /后续沟通仍需确认/);
      assert.equal(mutations.length, mutationCount);
    });

    await t.test('source sidebar and directory open originals by ID and download unsupported formats', async () => {
      await chapter('事实时间线').click();
      await reader.locator('.timeline-event').filter({ hasText: '付款陈述' }).getByRole('button', { name: '查看来源与关联 ↗' }).click();
      const sources = reader.getByRole('complementary', { name: '相关证据及文件来源' });
      const popupPromise = page.waitForEvent('popup');
      await sources.getByRole('link', { name: '打开原始文件 ↗' }).click();
      const popup = await popupPromise;
      await popup.waitForLoadState('domcontentloaded');
      assert.match(popup.url(), new RegExp(`/files/${data.case.files[0].file_id}/original$`));
      assert.equal(await popup.locator('body').innerText(), Buffer.from(data.originals[data.case.files[0].file_id].raw, 'base64').toString());
      assert.equal(await popup.evaluate(() => window.opener), null);
      await popup.close();
      await sources.getByRole('button', { name: '收起证据与来源' }).click();
      await chapter('证据目录').click();
      const office = reader.locator(`[data-file-id="${data.case.files[1].file_id}"]`);
      await office.locator('summary').first().click();
      const downloadPromise = page.waitForEvent('download');
      await office.getByRole('link', { name: '打开原始文件 ↗' }).click();
      const download = await downloadPromise;
      assert.equal(download.suggestedFilename(), '合同 #100%.docx');
      assert.deepEqual(await readFile(await download.path()), Buffer.from(data.originals[data.case.files[1].file_id].raw, 'base64'));
      const image = reader.locator(`[data-file-id="${data.case.files[2].file_id}"]`);
      await image.locator('summary').first().click();
      const imagePromise = page.waitForEvent('popup');
      await image.getByRole('link', { name: '打开原始文件 ↗' }).click();
      const imagePage = await imagePromise;
      await imagePage.waitForLoadState('load');
      assert.equal(await imagePage.locator('img').evaluate(img => img.naturalWidth), 1);
      await imagePage.close();
      const pdf = reader.locator(`[data-file-id="${data.case.files[3].file_id}"]`);
      await pdf.locator('summary').first().click();
      const pdfPromise = page.waitForEvent('popup');
      await pdf.getByRole('link', { name: '打开原始文件 ↗' }).click();
      const pdfPage = await pdfPromise;
      await pdfPage.waitForLoadState('domcontentloaded');
      await mkdir(`${root}/tmp`, { recursive: true });
      // Chromium renders PDFs in its built-in extension frame, outside the
      // original response document's DOM. Verify the actual native plugin.
      const viewer = pdfPage.frames().find(frame => frame.url().startsWith('chrome-extension://'))
        ?? await pdfPage.waitForEvent('framenavigated', { predicate: frame => frame.url().startsWith('chrome-extension://') });
      await viewer.locator('embed[type="application/x-google-chrome-pdf"]').waitFor({ state: 'attached' });
      await pdfPage.screenshot({ path: `${root}/tmp/case-materials-original-pdf.png` });
      await pdfPage.close();
    });

    await t.test('download button handles missing originals and produces an archive that runs offline without network', async () => {
      const downloadButton = page.getByRole('button', { name: '下载案件材料包', exact: true });
      failZip = true;
      await downloadButton.click();
      await page.getByRole('alert').waitFor();
      assert.match(await page.getByRole('alert').innerText(), /下载失败.*original_file_missing/);
      assert.equal(await reader.locator('#files').isVisible(), true);
      failZip = false;
      const downloadPromise = page.waitForEvent('download');
      await downloadButton.click();
      const download = await downloadPromise;
      assert.equal(download.suggestedFilename(), 'case-materials.zip');
      const archivePath = await download.path();
      assert.deepEqual(await readFile(archivePath), Buffer.from(data.archive, 'base64'));
      await mkdir(`${root}/tmp`, { recursive: true });
      const folder = await mkdtemp(`${root}/tmp/case-materials-offline-`);
      const extracted = spawnSync(`${root}/backend/.venv/Scripts/python.exe`, ['-c', `
import sys
from zipfile import ZipFile
with ZipFile(sys.argv[1]) as archive:
    archive.extractall(sys.argv[2])
`, archivePath, folder], { encoding: 'utf8' });
      assert.equal(extracted.status, 0, extracted.stderr);
      const offline = await browser.newPage();
      const network = [];
      await offline.context().route(/^https?:\/\//, route => { network.push(route.request().url()); return route.abort(); });
      await offline.goto(pathToFileURL(`${folder}/index.html`).href);
      assert.equal(await offline.locator('.chapter:visible').count(), 1);
      await offline.getByRole('navigation', { name: '材料包目录' }).getByRole('link', { name: '争议问题与证据对应表', exact: true }).click();
      assert.match(await offline.locator('#issues').innerText(), /重新生成后的押金命题/);
      await offline.locator('.contents > a[href="#files"]').click();
      await offline.locator('.file-record').first().locator('summary').first().click();
      const popupPromise = offline.waitForEvent('popup');
      await offline.locator('.file-record').first().getByRole('link', { name: '打开原始文件 ↗' }).click();
      const source = await popupPromise;
      await source.waitForLoadState('domcontentloaded');
      assert.equal(new URL(source.url()).protocol, 'file:');
      assert.match(await source.locator('body').innerText(), /甲称已付款/);
      await source.close();
      assert.deepEqual(network, []);
      await offline.close();
    });

    await t.test('regeneration refreshes saved judgments/gaps; failure retains the previous snapshot and retry works', async () => {
      await chapter('协商准备摘要').click();
      fail = true;
      await page.getByRole('button', { name: '重新生成', exact: true }).click();
      await page.getByRole('alert').waitFor();
      assert.match(await page.getByRole('alert').innerText(), /仍显示上次材料包/);
      assert.equal(await reader.locator('#preparation').isVisible(), true);
      fail = false; revision = 'second';
      await page.getByRole('button', { name: '重试生成' }).click();
      await reader.getByRole('heading', { name: '案件概况', exact: true }).waitFor();
      await chapter('争议问题与证据对应表').click();
      assert.match(await reader.locator('#issue-1').innerText(), /重新生成后的押金命题/);
      assert.match(await reader.locator('#issue-1').innerText(), /已否定命题/);
      await chapter('待补充事项').click();
      assert.doesNotMatch(await reader.locator('#gaps').innerText(), /仍需取得押金转账凭证/);
      await page.getByRole('button', { name: '返回工作台', exact: false }).click();
      assert.equal(await page.locator('dialog.materials-page').count(), 0);
      fail = true;
      await entry.click();
      await page.getByRole('alert').waitFor();
      assert.equal(await page.locator('iframe').count(), 0);
      fail = false;
      await page.getByRole('button', { name: '重试生成' }).click();
      await reader.getByRole('heading', { name: '案件概况', exact: true }).waitFor();
    });

    await t.test('mobile navigation and source drawer fit; switching to an empty Case cannot retain old material', async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      await chapter('事实时间线').focus();
      await page.keyboard.press('Enter');
      assert.equal(await reader.locator('#timeline').isVisible(), true);
      await reader.locator('.timeline-event').first().getByRole('button', { name: '查看来源与关联 ↗' }).click();
      await reader.getByRole('button', { name: '收起证据与来源' }).click();
      assert.equal(await reader.locator('html').evaluate(el => el.scrollWidth <= el.clientWidth), true);
      await mkdir(`${root}/tmp`, { recursive: true });
      await page.screenshot({ path: `${root}/tmp/case-materials-mobile.png` });
      await page.setViewportSize({ width: 1440, height: 1000 });
      await chapter('争议问题与证据对应表').click();
      await page.screenshot({ path: `${root}/tmp/case-materials-desktop.png` });
      await page.getByRole('button', { name: '返回工作台', exact: false }).click();
      await page.locator('.case-selector-trigger').click();
      await page.getByRole('button', { name: '空白案件', exact: true }).click();
      await open();
      assert.match(await reader.locator('.case-name').innerText(), /空白案件/);
      await chapter('证据目录').click();
      assert.match(await reader.locator('#files').innerText(), /尚无原始上传文件/);
      assert.equal(await reader.locator('[data-file-id]').count(), 0);
      assert.equal(await reader.locator('body').evaluate(() => window.injected), undefined);
    });
    assert.deepEqual(errors, []);
    assert.deepEqual(forbidden, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
