import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { SummaryMarkdown } from '../src/components/SummaryMarkdown.ts';

const render = text => renderToStaticMarkup(createElement(SummaryMarkdown, { text }));

test('summary preserves paragraphs, line breaks and emphasis', () => {
  assert.equal(render('实际损失**尚未证实**。\r\n\r\n优先核对*计租日*。\n可采纳或忽略。'),
    '<div class="investigation-answer"><p>实际损失<strong>尚未证实</strong>。</p><p>优先核对<em>计租日</em>。<br/>可采纳或忽略。</p></div>');
});

test('HTML and unsafe Markdown never create active elements', () => {
  const markup = render('<img src=x onerror="alert(1)"><script>alert(1)</script>\n\n[查看](javascript:alert(1)) ![图片](https://untrusted.invalid/pixel)');
  assert.ok(markup.includes('&lt;img'));
  assert.ok(markup.includes('&lt;script&gt;'));
  assert.doesNotMatch(markup, /<(?:img|script|a|iframe)\b/);
  assert.ok(markup.includes('[查看](javascript:alert(1))'));
});

test('plain facts, unfinished emphasis and literal punctuation are preserved', () => {
  assert.equal(render('据张某陈述，3 月 8 日搬入，尚未独立核实。'),
    '<div class="investigation-answer"><p>据张某陈述，3 月 8 日搬入，尚未独立核实。</p></div>');
  assert.ok(render('未闭合**强调与 A < B').includes('未闭合**强调与 A &lt; B'));
});
