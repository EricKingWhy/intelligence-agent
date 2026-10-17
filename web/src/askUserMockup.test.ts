// #1218：`docs/plans/ask-user/ask.html` 是独立的交互样例（不是 web 构建产物），
// 这里用 jsdom 直接执行它的内联脚本，锁住「跳过不算已提交」的完成提示。
// jsdom 无内置类型声明，web 也未安装 @types/jsdom。
// @ts-expect-error jsdom 无内置类型声明
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it } from 'vitest';

const openWindows: Array<{ window: { close: () => void } }> = [];
afterEach(() => {
  for (const dom of openWindows.splice(0)) dom.window.close();
});

const html = readFileSync(new URL('../../docs/plans/ask-user/ask.html', import.meta.url), 'utf8');

const QUESTIONS_COUNT = 4;

function openMockup() {
  const dom = new JSDOM(html, { runScripts: 'dangerously' });
  openWindows.push(dom);
  const doc = dom.window.document;
  const click = (id: string) => (doc.getElementById(id) as HTMLElement).click();
  const clickFirstOption = () => (doc.querySelector('.opt') as HTMLElement).click();
  const toast = () => doc.getElementById('toast')?.textContent ?? '';
  const after = () => doc.getElementById('afterMsg')?.textContent ?? '';
  return { doc, click, clickFirstOption, toast, after };
}

/** 翻页动画 150ms 后才重绘下一题，等它落定再点选项。 */
async function waitForQuestion(doc: Document, questionIndex: number) {
  const expectedPage = `${questionIndex + 1} / ${QUESTIONS_COUNT}`;
  const deadline = Date.now() + 1000;
  while (doc.getElementById('page')?.textContent !== expectedPage) {
    if (Date.now() >= deadline) throw new Error(`Timed out waiting for page: ${expectedPage}`);
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

describe('ask_user 交互样例的完成提示（#1218）', () => {
  it('部分作答、部分跳过：只把真正作答的题算作已提交', () => {
    const m = openMockup();
    m.clickFirstOption(); // 第 1 题选「今晚低峰期」
    m.click('fwd'); // 第 2 题
    m.click('skip'); // 第 2 题跳过
    m.click('skip'); // 第 3 题跳过
    m.click('skip'); // 第 4 题跳过，触发 finish

    expect(m.toast()).toBe('已提交 1 个决定，跳过 3 个问题，run 继续');
    expect(m.after()).toContain('今晚低峰期');
    expect(m.after().match(/：跳过/g)).toHaveLength(3);
    expect(m.after()).toContain('收到，按你的决定继续发布…');
  });

  it('全部跳过：不说成已提交，并说明跳过了全部题目', () => {
    const m = openMockup();
    for (let i = 0; i < QUESTIONS_COUNT; i++) m.click('skip');

    expect(m.toast()).toBe(`已跳过全部 ${QUESTIONS_COUNT} 个问题，run 继续`);
    expect(m.after()).toContain('未作答问题均已跳过');
    expect(m.after().match(/：跳过/g)).toHaveLength(QUESTIONS_COUNT);
  });

  it('提前提交：未作答的题按跳过计，不混入已提交数量', () => {
    const m = openMockup();
    m.clickFirstOption(); // 只答第 1 题
    m.click('submit');

    expect(m.toast()).toBe('已提交 1 个决定，跳过 3 个问题，run 继续');
    expect(m.after()).toContain('今晚低峰期');
    expect(m.after().match(/：跳过/g)).toHaveLength(3);
  });

  it('自定义答案计为已提交、摘要按题隔离且按文本展示；空白自定义禁用提交', async () => {
    const m = openMockup();
    m.click('customRow');
    const input = m.doc.getElementById('customInput') as HTMLTextAreaElement;
    input.value = '<img src=x onerror="window.__injected=true"> 下周一凌晨';
    input.dispatchEvent(new m.doc.defaultView!.Event('input', { bubbles: true }));
    m.click('fwd');
    await waitForQuestion(m.doc, 1);
    expect((m.doc.getElementById('customInput') as HTMLTextAreaElement).value).toBe('');
    m.click('customRow');
    expect((m.doc.getElementById('customInput') as HTMLTextAreaElement).value).toBe('');
    m.click('skip');
    m.click('skip');
    m.click('skip');

    expect(m.toast()).toBe('已提交 1 个决定，跳过 3 个问题，run 继续');
    expect(m.after()).toContain('新版本什么时候发布：<img src=x onerror="window.__injected=true"> 下周一凌晨（自定义）');
    expect(m.after()).toContain('这次发版走哪套部署流程：跳过');
    expect(m.doc.querySelector('#afterMsg img')).toBeNull();
    expect(m.doc.defaultView?.__injected).toBeUndefined();
    expect(m.after().match(/：跳过/g)).toHaveLength(3);

    const blank = openMockup();
    blank.click('customRow');
    const blankInput = blank.doc.getElementById('customInput') as HTMLTextAreaElement;
    blankInput.value = '   ';
    blankInput.dispatchEvent(new blank.doc.defaultView!.Event('input', { bubbles: true }));

    expect((blank.doc.getElementById('submit') as HTMLButtonElement).disabled).toBe(true);
    expect(blank.toast()).toBe('');
  });

  it('全部作答：四题都算已提交，摘要列出四个选择', async () => {
    const m = openMockup();
    for (let i = 0; i < QUESTIONS_COUNT - 1; i++) {
      m.clickFirstOption();
      m.click('fwd');
      await waitForQuestion(m.doc, i + 1);
    }
    m.clickFirstOption();
    m.click('submit');

    expect(m.toast()).toBe(`已提交 ${QUESTIONS_COUNT} 个决定，run 继续`);
    expect(m.after()).toContain('收到，按你的决定继续发布…');
    expect(m.after()).toContain('新版本什么时候发布：今晚低峰期');
    expect(m.after()).toContain('这次发版走哪套部署流程：蓝绿部署');
    expect(m.after()).toContain('发布后出现异常怎么回滚：自动回滚');
    expect(m.after()).toContain('发版后通知哪些人：仅通知我');
    expect(m.after()).not.toContain('：跳过');
  });
});
