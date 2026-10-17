// #1218：`docs/plans/ask-user/ask.html` 是独立的交互样例（不是 web 构建产物），
// 这里用 jsdom 直接执行它的内联脚本，锁住「跳过不算已提交」的完成提示。
// jsdom 无内置类型声明，web 也未安装 @types/jsdom。
// @ts-expect-error jsdom 无内置类型声明
import { JSDOM } from 'jsdom';
// @ts-expect-error node 内置模块没有声明（web 的 tsconfig 只带 vite/client）
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
  const clickOption = (i: number) => (doc.querySelectorAll('.opt')[i] as HTMLElement).click();
  const clickFirstOption = () => clickOption(0);
  const setCustom = (text: string) => {
    const input = doc.getElementById('customInput') as HTMLTextAreaElement;
    input.value = text;
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  };
  const toast = () => doc.getElementById('toast')?.textContent ?? '';
  const after = () => doc.getElementById('afterMsg')?.textContent ?? '';
  return { doc, click, clickOption, clickFirstOption, setCustom, toast, after };
}

/**
 * 翻页动画 150ms 后才重绘下一题，且动画期间样例丢弃输入（见「翻页动画期间」用例），
 * 所以每次翻页后都要等它落定，再点下一题的按钮。
 */
async function waitForQuestion(doc: Document, questionIndex: number) {
  const expectedPage = `${questionIndex + 1} / ${QUESTIONS_COUNT}`;
  const deadline = Date.now() + 1000;
  while (doc.getElementById('page')?.textContent !== expectedPage) {
    if (Date.now() >= deadline) throw new Error(`Timed out waiting for page: ${expectedPage}`);
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

describe('ask_user 交互样例的完成提示（#1218）', () => {
  it('部分作答、部分跳过：只把真正作答的题算作已提交', async () => {
    const m = openMockup();
    m.clickFirstOption(); // 第 1 题选「今晚低峰期」
    m.click('fwd'); // 第 2 题
    await waitForQuestion(m.doc, 1);
    m.click('skip'); // 第 2 题跳过
    await waitForQuestion(m.doc, 2);
    m.click('skip'); // 第 3 题跳过
    await waitForQuestion(m.doc, 3);
    m.click('skip'); // 第 4 题跳过，触发 finish

    expect(m.toast()).toBe('已提交 1 个决定，跳过 3 个问题，run 继续');
    expect(m.after()).toContain('今晚低峰期');
    expect(m.after().match(/：跳过/g)).toHaveLength(3);
    expect(m.after()).toContain('收到，按你的决定继续发布…');
  });

  it('全部跳过：不说成已提交，并说明跳过了全部题目', async () => {
    const m = openMockup();
    for (let i = 0; i < QUESTIONS_COUNT; i++) {
      m.click('skip');
      if (i < QUESTIONS_COUNT - 1) await waitForQuestion(m.doc, i + 1);
    }

    expect(m.toast()).toBe(`已跳过全部 ${QUESTIONS_COUNT} 个问题，run 继续`);
    expect(m.after()).toContain('未作答问题均已跳过');
    expect(m.after().match(/：跳过/g)).toHaveLength(QUESTIONS_COUNT);
  });

  it('提前提交：未作答的题按跳过计；提交后完成提示不被改写且卡片显式置为不可用', () => {
    const m = openMockup();
    m.clickFirstOption(); // 只答第 1 题
    m.click('submit');

    expect(m.toast()).toBe('已提交 1 个决定，跳过 3 个问题，run 继续');
    expect(m.after()).toContain('今晚低峰期');
    expect(m.after().match(/：跳过/g)).toHaveLength(3);

    // 再改选项、再点提交都不会改写已经发出的完成提示。
    m.clickOption(1); // 「明早 9 点」
    m.click('submit');
    expect(m.after()).toContain('今晚低峰期');
    expect(m.after()).not.toContain('明早 9 点');

    // 冻结要看得见：此刻已经无效的控件被显式置为不可用。
    expect(m.doc.getElementById('qcard')?.classList.contains('done')).toBe(true);
    expect((m.doc.getElementById('submit') as HTMLButtonElement).disabled).toBe(true);
    expect((m.doc.getElementById('skip') as HTMLButtonElement).disabled).toBe(true);
    expect((m.doc.getElementById('customInput') as HTMLTextAreaElement).readOnly).toBe(true);
  });

  it('提交后点关闭：提示不再自称 run 仍暂停', () => {
    const m = openMockup();
    m.clickFirstOption();
    m.click('submit');

    m.click('close');
    expect(m.toast()).toBe('已关闭卡片，run 已按你的决定继续');
    expect(m.toast()).not.toContain('保持暂停');
  });

  it('翻页动画期间的输入被丢弃：不写进新题，也不解锁新题的提交', async () => {
    const m = openMockup();
    m.click('skip'); // 第 1 题跳过 → 动画切到第 2 题
    m.clickOption(0); // 动画期间：选选项（写进新题就会把没人作答的第 2 题算成已提交）
    m.click('customRow'); // 动画期间：切自定义
    m.setCustom('乱写'); // 动画期间：输入文本
    m.click('fwd'); // 动画期间：往后翻
    m.click('prev'); // 动画期间：回退
    await waitForQuestion(m.doc, 1);

    expect(m.doc.getElementById('page')?.textContent).toBe(`2 / ${QUESTIONS_COUNT}`);
    expect((m.doc.getElementById('customInput') as HTMLTextAreaElement).value).toBe('');
    expect((m.doc.getElementById('submit') as HTMLButtonElement).disabled).toBe(true);
    expect(m.doc.querySelector('.opt.selected')).toBeNull();

    m.click('skip'); // 仍然停在第 2 题：跳过它
    await waitForQuestion(m.doc, 2);
    m.click('skip'); // 第 3 题跳过
    await waitForQuestion(m.doc, 3);
    m.click('skip'); // 第 4 题跳过，触发 finish

    expect(m.toast()).toBe(`已跳过全部 ${QUESTIONS_COUNT} 个问题，run 继续`);
    expect(m.after().match(/：跳过/g)).toHaveLength(QUESTIONS_COUNT);
  });

  it('自定义答案计为已提交、摘要按题隔离且按文本展示；空白自定义既禁用提交也不计入已提交', async () => {
    const m = openMockup();
    m.click('customRow');
    m.setCustom('<img src=x onerror="window.__injected=true"> 下周一凌晨');
    m.click('fwd');
    await waitForQuestion(m.doc, 1);
    expect((m.doc.getElementById('customInput') as HTMLTextAreaElement).value).toBe('');
    m.click('customRow');
    expect((m.doc.getElementById('customInput') as HTMLTextAreaElement).value).toBe('');
    m.click('skip');
    await waitForQuestion(m.doc, 2);
    m.click('skip');
    await waitForQuestion(m.doc, 3);
    m.click('skip');

    expect(m.toast()).toBe('已提交 1 个决定，跳过 3 个问题，run 继续');
    expect(m.after()).toContain(
      '新版本什么时候发布：<img src=x onerror="window.__injected=true"> 下周一凌晨（自定义）',
    );
    expect(m.after()).toContain('这次发版走哪套部署流程：跳过');
    expect(m.doc.querySelector('#afterMsg img')).toBeNull();
    expect(m.doc.defaultView?.__injected).toBeUndefined();
    expect(m.after().match(/：跳过/g)).toHaveLength(3);

    // 空白自定义：当前题禁用提交；落到别的题上提交时，这一题也只算「跳过」。
    const blank = openMockup();
    blank.click('customRow');
    blank.setCustom('   ');
    blank.click('fwd');
    await waitForQuestion(blank.doc, 1);
    blank.click('customRow');
    expect((blank.doc.getElementById('customInput') as HTMLTextAreaElement).value).toBe('');
    expect((blank.doc.getElementById('submit') as HTMLButtonElement).disabled).toBe(true);
    expect(blank.toast()).toBe('');

    blank.clickFirstOption(); // 第 2 题选一项后提交
    blank.click('submit');
    expect(blank.toast()).toBe('已提交 1 个决定，跳过 3 个问题，run 继续');
    expect(blank.after()).toContain('新版本什么时候发布：跳过');
    expect(blank.after()).not.toContain('（自定义）');
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
