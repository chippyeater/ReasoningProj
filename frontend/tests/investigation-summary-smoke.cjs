// Headed UI -> real Case A API/loop; only model responses are mocked by serve_agent_smoke.py.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const assert = require('node:assert/strict');
const api = 'http://127.0.0.1:8018';

(async () => {
  const browser = await chromium.launch({ headless: false });
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.request.post(`${api}/api/cases/case-4325561f/select`);
    const before = (await (await page.request.get(`${api}/api/case`)).json()).case;
    const stateUrl = `${api}/api/cases/${before.case_id}/issue-workspaces`;
    await page.goto('http://127.0.0.1:5178', { waitUntil: 'domcontentloaded' });
    await page.getByRole('tab', { name: before.issue_set[1], exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('.workspace-save-state')?.textContent);
    await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
    const panel = page.getByRole('region', { name: 'Agent 调查结果' });
    const input = page.getByPlaceholder('提出调查问题，或检查假设与证据缺口…');
    async function ask(goal) {
      await input.fill(goal);
      // Bounded finish recovery adds audited turns; keep UI action timeouts unchanged.
      const response = page.waitForResponse(r => r.url().endsWith('/agent/run') && r.request().method() === 'POST', { timeout: 30000 });
      await page.getByRole('button', { name: '发送调查问题' }).click();
      const result = await (await response).json();
      await panel.waitFor();
      return result;
    }
    const result = await ask('房东是否存在实际空置损失');
    const summary = panel.locator(':scope > .investigation-answer');
    await summary.waitFor();
    assert.ok((await summary.innerText()).length < 150);
    assert.equal(result.workspace_commit.status, 'applied', JSON.stringify(result));
    assert.equal(result.proposals.length, 3);
    const history = page.getByRole('region', { name: '调查变更记录' });
    await history.getByRole('button', { name: '撤销本轮变更', exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: /采纳|忽略建议/ }).count(), 0);
    for (const proposal of result.proposals) assert.ok(!(await summary.innerText()).includes(proposal.content));
    await history.getByRole('button', { name: '撤销本轮变更', exact: true }).click();
    await history.getByRole('button', { name: '恢复本轮变更', exact: true }).waitFor();
    const workBeforeFailure = (await (await page.request.get(stateUrl)).json()).workspaces[before.issue_set[1]].reasoning;
    const partial = await ask('房东是否存在实际空置损失，超长总结');
    await panel.getByText('本轮总结未完成', { exact: true }).waitFor();
    assert.equal(partial.summary_status, 'failed');
    assert.equal(partial.answer, null);
    assert.equal(partial.proposals.length, 3);
    assert.match(await panel.getByRole('alert').innerText(), /本轮未写入推理工作区/);
    await history.getByText('本轮未写入（3 项暂存提案）', { exact: true }).waitFor();
    assert.deepEqual((await (await page.request.get(stateUrl)).json()).workspaces[before.issue_set[1]].reasoning, workBeforeFailure);
    assert.deepEqual(errors, []);
    const after = (await (await page.request.get(`${api}/api/case`)).json()).case;
    for (const field of ['info_units', 'relations']) assert.deepEqual(after[field], before[field]);
    console.log('PASS: concise summary with automatic commit, rejected long summary retains staged artifacts without workspace writes.');
  } catch (error) {
    if (page) await page.screenshot({ path: 'tmp/investigation-summary-failure.png' }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
