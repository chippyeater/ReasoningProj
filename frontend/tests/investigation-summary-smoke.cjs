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
    await page.goto('http://127.0.0.1:5178');
    await page.getByRole('tab', { name: before.issue_set[1], exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('.workspace-save-state')?.textContent);
    const workBefore = await (await page.request.get(stateUrl)).json();
    await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
    const panel = page.getByRole('region', { name: 'Agent 调查结果' });
    const input = page.getByPlaceholder('提出调查问题，或检查假设与证据缺口…');
    async function ask(goal) {
      await input.fill(goal);
      const response = page.waitForResponse(r => r.url().endsWith('/agent/run') && r.request().method() === 'POST');
      await page.getByRole('button', { name: '发送调查问题' }).click();
      const result = await (await response).json();
      await panel.waitFor();
      return result;
    }
    const result = await ask('房东是否存在实际空置损失');
    const summary = panel.locator(':scope > .investigation-answer');
    await summary.getByText('尚未证实', { exact: true }).waitFor();
    assert.equal(await summary.locator('p').count(), 2);
    assert.equal(await summary.locator('strong').innerText(), '尚未证实');
    assert.ok((await summary.innerText()).length < 100);
    assert.match(await summary.innerText(), /两条假设和两项证据缺口/);
    assert.match(await summary.innerText(), /采纳或忽略/);
    assert.equal(result.proposals.filter(p => p.kind === 'hypothesis').length, 2);
    assert.equal(result.proposals.filter(p => p.kind === 'evidence_gap').length, 2);
    assert.equal(await panel.locator('blockquote:visible').count(), 0);
    assert.equal(await page.locator('.reasoning-candidate').count(), 4);
    await page.locator('.reasoning-suggestions').getByText('查看待确认建议（4）', { exact: true }).click();
    assert.equal(await page.locator('.reasoning-suggestion:visible').count(), 4);
    assert.equal(await page.getByRole('button', { name: '忽略建议', exact: true }).count(), 4);
    assert.equal(await page.getByRole('button', { name: '采纳建议', exact: true }).count(), 4);
    for (const proposal of result.proposals) assert.ok(!(await summary.innerText()).includes(proposal.content));
    assert.deepEqual((await (await page.request.get(stateUrl)).json()).workspaces[before.issue_set[1]].reasoning,
      workBefore.workspaces[before.issue_set[1]].reasoning); // No implicit adoption.
    await page.screenshot({ path: 'tmp/investigation-summary-markdown.png' });

    await ask('替租人什么时候搬进去的');
    await summary.getByText(/据张某陈述/).waitFor();
    assert.equal(await page.locator('.reasoning-suggestions').count(), 0);
    assert.doesNotMatch(await summary.innerText(), /已提出|采纳/);
    await panel.getByText('查看调查依据', { exact: true }).click();
    await panel.getByRole('button', { name: '张某笔录.docx', exact: true }).first().click();
    assert.equal(await page.locator('.canvas-node.node-selected').count(), 1);

    const partial = await ask('房东是否存在实际空置损失，超长总结');
    await panel.getByText('本轮总结未完成', { exact: true }).waitFor();
    assert.equal(partial.summary_status, 'failed');
    assert.equal(partial.answer, null);
    assert.equal(partial.proposals.length, 4);
    assert.equal(await summary.count(), 0);
    assert.match(await panel.getByRole('alert').innerText(), /建议尚未采纳/);
    await page.locator('.reasoning-suggestions').getByText('查看待确认建议（4）', { exact: true }).waitFor();

    await ask('安全格式检查');
    await summary.getByText('尚未证实', { exact: true }).waitFor();
    assert.equal(await summary.locator('img,script,a,iframe').count(), 0);
    assert.match(await summary.innerText(), /<img src=x/);
    assert.equal(await page.evaluate(() => window.summaryInjected), undefined);
    assert.deepEqual(errors, []);
    const after = (await (await page.request.get(`${api}/api/case`)).json()).case;
    for (const field of ['info_units', 'relations']) assert.deepEqual(after[field], before[field]);
    console.log('PASS: concise Markdown summary, exact pending counts/action entry, no implicit adoption, fact/source location, rejected long summary preserves cards, inert HTML/links.');
  } catch (error) {
    if (page) await page.screenshot({ path: 'tmp/investigation-summary-failure.png' }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
