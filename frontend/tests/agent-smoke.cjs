// Run against backend/tests/serve_agent_smoke.py and Vite with VITE_API_BASE=:8018.
// Uses an existing Playwright installation; does not add an app dependency.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({ headless: false });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.request.post('http://127.0.0.1:8018/api/cases/case-4325561f/select');
    await page.goto('http://127.0.0.1:5178');
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some(b => b.textContent === 'Agent 调查' && !b.disabled));
    const before = await (await page.request.get('http://127.0.0.1:8018/api/case')).json();
    await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
    const input = page.getByPlaceholder('提出调查问题（当前仅支持只读调查）…');
    const result = page.getByRole('region', { name: 'Agent 调查结果' });
    async function ask(goal) {
      await input.fill(goal);
      await page.getByRole('button', { name: '发送调查问题' }).click();
      await page.getByText('调查中…正在按需检索和核对依据。', { exact: true }).waitFor();
    }
    await ask('核对损失说法');
    await result.getByText('本轮调查已结束', { exact: true }).waitFor();
    assert.match(await result.innerText(), /张某笔录.docx/);
    assert.match(await result.innerText(), /笔录陈述/);
    assert.match(await result.innerText(), /这个房子其实也没有空置多久/);
    await result.getByRole('button', { name: '张某笔录.docx', exact: true }).first().click();
    assert.equal(await page.locator('.canvas-node.node-selected').count(), 1);
    await page.screenshot({ path: 'tmp/unified-agent-result.png' });
    await ask('核对入住说法');
    await result.getByText('本轮调查已结束', { exact: true }).waitFor();
    assert.match(await result.innerText(), /关键原文/);
    assert.match(await result.innerText(), /他 3 月 8 号就直接搬进去住了/);
    await ask('删除这个节点');
    await result.getByText('当前不支持此操作', { exact: true }).waitFor();
    assert.match(await result.innerText(), /当前仅支持只读调查/);
    await ask('模拟失败');
    await page.getByRole('alert').filter({ hasText: '离线模拟调用超时' }).waitFor();
    assert.equal(await result.count(), 0);
    const after = await (await page.request.get('http://127.0.0.1:8018/api/case')).json();
    for (const field of ['info_units', 'relations', 'workspace_state']) assert.deepEqual(after.case[field], before.case[field]);
    // Existing manual editing now lives in the context menu.
    async function command(name) {
      await page.locator('.canvas-node.node-selected').click({ button: 'right' });
      await page.getByRole('menuitem', { name, exact: true }).click();
    }
    await command('编辑选中节点');
    const editor = page.getByRole('dialog', { name: '编辑信息节点' });
    await editor.waitFor();
    const title = await editor.getByLabel('Title', { exact: true }).inputValue();
    await editor.getByLabel('Title', { exact: true }).fill('离线测试标题');
    await editor.getByRole('button', { name: '保存', exact: true }).click();
    await editor.waitFor({ state: 'hidden' });
    await command('编辑选中节点');
    await editor.getByLabel('Title', { exact: true }).fill(title);
    await editor.getByRole('button', { name: '保存', exact: true }).click();
    await editor.waitFor({ state: 'hidden' });
    await command('新建关系');
    await page.getByRole('dialog', { name: '新建关系' }).waitFor();
    await page.getByRole('button', { name: '关闭关系编辑器' }).click();
    await command('适应全部节点');
    // A delayed response from the previous case must not become the new case's result.
    await page.route('**/agent/run', async route => {
      const response = await route.fetch();
      await new Promise(resolve => setTimeout(resolve, 800));
      await route.fulfill({ response });
    });
    await ask('核对损失说法');
    await page.locator('.case-selector > .case-selector-trigger').click();
    await page.getByRole('button', { name: '空白测试案件', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.case-selector > .case-selector-trigger')?.textContent === '空白测试案件');
    await page.waitForTimeout(1800);
    assert.equal(await result.count(), 0);
    await page.unroute('**/agent/run');
    assert.equal(await page.getByRole('button', { name: '生成 InfoUnit 提案' }).count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS: UI → real API → dynamic loop; evidence, running/error/unsupported states, no case mutation, manual edit/save, relation editor, canvas fit.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
