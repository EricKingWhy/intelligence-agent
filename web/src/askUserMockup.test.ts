// @vitest-environment jsdom
// #1218：`docs/plans/ask-user/ask.html` 是独立的交互样例（不是 web 构建产物），
// 这里用 jsdom 直接执行它的内联脚本，锁住「跳过不算已提交」的完成提示。
// jsdom 不自带类型声明，web 也未安装 @types/jsdom；本测试只用到构造器与 window。
// @ts-expect-error jsdom 无内置类型声明
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

// vitest 的 cwd 是 web/（package.json / CI 都在 web 下执行），样例在仓库根的 docs/。
const html = readFileSync('../docs/plans/ask-user/ask.html', 'utf8');

const QUESTIONS_COUNT = 4;

function openMockup() {
  const dom = new JSDOM(html, { runScripts: 'dangerously' });
  const doc = dom.window.document;
  const click = (id: string) => (doc.getElementById(id) as HTMLElement).click();
  const clickFirstOption = () => (doc.querySelector('.opt') as HTMLElement).click();
  const toast = () => doc.getElementById('toast')?.textContent ?? '';
  const after = () => doc.getElementById('afterMsg')?.textContent ?? '';
  return { doc, click, clickFirstOption, toast, after };
}

/** 翻页动画 150ms 后才重绘下一题，等它落定再点选项。 */
const tick = () => new Promise((resolve) => setTimeout(resolve, 220));

describe('ask_user 交互样例的完成提示（#1218）', () => {
  it('部分作答、部分跳过：只把真正作答的题算作已提交', () => {
    const m = openMockup();
    m.clickFirstOption(); // 第 1 题选「今晚低峰期」
    m.click('fwd'); // 第 2 题
    m.click('skip'); // 第 2 题跳过
    m.click('skip'); // 第 3 题跳过
    m.click('skip'); // 第 4 题跳过，触发 finish

    expect(m.toast()).toBe('已提交 1 个决定，跳过 3 个，run 继续');
    expect(m.after()).toContain('今晚低峰期');
    expect(m.after().match(/：跳过/g)).toHaveLength(3);
    expect(m.after()).not.toContain('已提交');
  });

  it('全部跳过：不说成已提交，并说明跳过了全部题目', () => {
    const m = openMockup();
    for (let i = 0; i < QUESTIONS_COUNT; i++) m.click('skip');

    expect(m.toast()).toBe(`已跳过全部 ${QUESTIONS_COUNT} 个问题，run 继续`);
    expect(m.toast()).not.toContain('已提交');
    expect(m.after().match(/：跳过/g)).toHaveLength(QUESTIONS_COUNT);
  });

  it('提前提交：未作答的题按跳过计，不混入已提交数量', () => {
    const m = openMockup();
    m.clickFirstOption(); // 只答第 1 题
    m.click('submit');

    expect(m.toast()).toBe('已提交 1 个决定，跳过 3 个，run 继续');
    expect(m.after().match(/：跳过/g)).toHaveLength(3);
  });

  it('全部作答：四题都算已提交，摘要里没有跳过', async () => {
    const m = openMockup();
    for (let i = 0; i < QUESTIONS_COUNT - 1; i++) {
      m.clickFirstOption();
      m.click('fwd');
      await tick();
    }
    m.clickFirstOption();
    m.click('submit');

    expect(m.toast()).toBe(`已提交 ${QUESTIONS_COUNT} 个决定，run 继续`);
    expect(m.after()).not.toContain('跳过');
  });
});
