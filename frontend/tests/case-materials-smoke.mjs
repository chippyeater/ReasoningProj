import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import { createServer } from 'vite';

const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fileURLToPath(new URL('../..', import.meta.url));
// Reuse the isolated backend fixture and real HTML endpoint. No live case data,
// backend server, model transport, or additional test dependencies are needed.
const fixture = spawnSync(`${root}/backend/.venv/Scripts/python.exe`, ['-c', `
import json, sys
sys.path[:0] = ['backend', 'backend/tests']
from test_case_materials import TestCaseMaterials
from app.case_service import create_empty_case
from app.case_store import save_case
from app.issue_workspaces import persist_issue_workspaces
t = TestCaseMaterials()
t.setUp()
try:
    url = t.fixture()
    t.case.files[0].filename = '<img src=x onerror="window.injected=1">.txt'
    t.case.info_units[0].content = '甲称已付款。<img src=x onerror="window.injected=1">'
    save_case(t.case)
    first = t.client.get(url).text
    t.state.workspaces[t.b].reasoning.hypotheses[0].content = '重新生成后的押金命题'
    t.state.workspaces[t.b].reasoning.hypotheses[0].judgment = 'refuted'
    t.state.workspaces[t.b].reasoning.gaps[0].status = 'resolved'
    persist_issue_workspaces(t.case, t.state)
    second = t.client.get(url).text
    blank = create_empty_case('空白案件')
    save_case(blank, set_current=False)
    blank_html = t.client.get(f'/api/cases/{blank.case_id}/materials').text
    print(json.dumps(dict(case=t.case.model_dump(mode='json'), state=t.state.model_dump(mode='json'),
        first=first, second=second, blank=blank.model_dump(mode='json'), blank_html=blank_html)))
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
    let caseData = data.case, revision = 'first', fail = false;
    await page.route('**/api/**', async route => {
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
      assert.equal(await page.locator('iframe').getAttribute('sandbox'), 'allow-scripts');
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
      await sources.getByRole('link').click();
      assert.equal(await reader.locator('#files').isVisible(), true);
      assert.equal(await reader.locator('[data-file-id]').count(), 1);
      assert.equal(await reader.locator('[data-file-id]').getAttribute('data-file-id'), data.case.files[0].file_id);
      assert.equal(await reader.locator('.file-record').getAttribute('open'), '');
      await sources.getByRole('button', { name: '收起证据与来源' }).click();
      await chapter('待补充事项').click();
      assert.match(await reader.locator('#gaps').innerText(), /仍需取得押金转账凭证/);
      assert.doesNotMatch(await reader.locator('#gaps').innerText(), /已经解决的交付问题/);
      await chapter('协商准备摘要').click();
      assert.match(await reader.locator('#preparation').innerText(), /后续沟通仍需确认/);
      assert.equal(mutations.length, mutationCount);
    });

    await t.test('regeneration refreshes saved judgments/gaps; failure retains the previous snapshot and retry works', async () => {
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
