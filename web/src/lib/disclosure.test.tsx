// @vitest-environment jsdom
/** F1（#270）必做 1：hook 返回值的**引用稳定性**（R1）+ 稳定化后不得读到旧值（R2）。
 *
 *  为什么这个文件要 jsdom（而本目录既有 disclosure.test.ts 是 node）：R1/R2 判的都是
 *  「同一个组件实例连续两次渲染」，需要真实客户端渲染器做状态更新。本仓默认车道的
 *  `renderToStaticMarkup` 是**单次**渲染、跑不了状态更新——`memo` 的浅比较在 SSR 里
 *  根本不会执行。所以这里用**文件级** `@vitest-environment` 覆盖，全局配置不动：
 *  其余测试文件继续跑 node。
 *
 *  实现取票面必做 1 的 **B 方案**（整体 useMemo），不取「推荐」的 A 方案（身份永不改变
 *  的 useRef 容器）——理由与红证写在 `disclosure.ts` 顶部注释里。这决定了本文件的断言
 *  口径：**"读最新一次渲染返回的那个对象"**，而不是"首渲染拿到的对象永远有效"。
 *  R1 的准确形式因此是「**依赖未变的相邻两次渲染** `===` 相等」，同时**必须**在
 *  override 变化时换引用（否则点击工具行会静默无效——见下面的 A 方案反证用例）。
 *
 *  重渲染怎么驱动：改**传入的 prop**（`tick`），而不是在渲染体里改模块级变量——
 *  后者会命中 oxlint 的 `react(immutability)` / `react(globals)`（AC6 要求零新增告警）。
 *  父级"确实重渲染了"的证据就是那个 tick 落到了 DOM 上。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import {
  useDisclosure,
  useReasoningDisclosure,
  type Disclosure,
} from './disclosure';
import type { ReasoningDisclosureApi } from '../components/ReasoningBlock';
import type { TraceDensity } from './density';

// ── 最小客户端渲染夹具（不引入 @testing-library：本仓只用 react-dom 自身能力）──

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function mount(ui: ReactElement): void {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root!.render(ui));
}

/** 同一实例的又一次渲染（同一条 root，props 变）。 */
function rerender(ui: ReactElement): void {
  act(() => root!.render(ui));
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  root = null;
  container = null;
});

/** 最近一次渲染拿到的对象——React 消费方拿到的就是这一个。 */
const latest = <T,>(seen: T[]): T => seen[seen.length - 1];

// ── useDisclosure ──

/** 把**每一次渲染**拿到的 api 都记下来：渲染之间就能直接比身份。
 *  `tick` 只用于从外部驱动重渲染并留下落地证据（不参与 hook 逻辑）。 */
function DisclosureHarness({ seen, tick }: { seen: Disclosure[]; tick: number }) {
  const api = useDisclosure('session-1');
  seen.push(api);
  return <span data-tick={tick} />;
}

describe('F1 — useDisclosure 返回值的引用稳定性（R1，AC1）', () => {
  it('依赖未变的连续渲染返回**同一个对象**（R1）', () => {
    const seen: Disclosure[] = [];
    mount(<DisclosureHarness seen={seen} tick={0} />);
    // 挂载只渲染一次：sessionKey 的清空 effect 在挂载期被跳过（初值本就是空 Map），
    // 所以这里**不该**出现第二次渲染。这条断言同时锁住那次无内容变化的状态写入。
    expect(seen.length).toBe(1);
    for (let tick = 1; tick <= 3; tick++) rerender(<DisclosureHarness seen={seen} tick={tick} />);
    // 夹具自检：外部 prop 真的驱动了状态更新，否则下面的身份断言是空断言。
    expect(seen.length).toBe(4);
    expect(container!.querySelector('[data-tick]')!.getAttribute('data-tick')).toBe('3');
    // 全部是同一个对象 ⇒ 作为 prop 传下去时浅比较恒等，memo 才会命中。
    expect(new Set(seen).size).toBe(1);
  });

  it('override 变化时**必须**换引用（否则点击工具行静默无效——A 方案反证）', () => {
    const seen: Disclosure[] = [];
    mount(<DisclosureHarness seen={seen} tick={0} />);
    const before = latest(seen);
    const n = seen.length;
    act(() => before.setLevel('tool:c1', 1));
    expect(seen.length).toBe(n + 1); // setLevel 确实驱动了重渲染
    /* 这一条是「引用稳定」的**边界**：`levelFor` 在 TurnView 自己的渲染体里被调用
     * （Conversation.tsx:711），所以 override 变化后必须让 memo(TurnView) 重新比较出
     * 不等，新档位才流得到 ToolCard。身份永不改变的容器方案会在这里恒等地失败。 */
    expect(latest(seen)).not.toBe(before);
    expect(latest(seen).levelFor('tool:c1', 'compact')).toBe(1);
  });

  it('稳定化后方法仍读**最新** state：setLevel 连发两次都生效（R2，AC2）', () => {
    const seen: Disclosure[] = [];
    mount(<DisclosureHarness seen={seen} tick={0} />);
    act(() => latest(seen).setLevel('tool:c1', 1));
    act(() => latest(seen).setLevel('tool:c1', 2));
    // 读旧 overrides 会得到 density 默认值 0——这就是「点了没反应」的静默 bug
    // （依赖集合漏了 overrides 时，memo 会一直返回首渲染那个对象，这里必红）。
    expect(latest(seen).levelFor('tool:c1', 'compact')).toBe(2);
  });

  it('稳定化后 setLevel 写入的 override 仍优先于 density 默认（语义不变）', () => {
    const seen: Disclosure[] = [];
    mount(<DisclosureHarness seen={seen} tick={0} />);
    act(() => latest(seen).setLevel('tool:c1', 0));
    expect(latest(seen).levelFor('tool:c1', 'raw')).toBe(0); // 手动 L0 压过 raw 默认 L2
  });
});

// ── useReasoningDisclosure ──

/** density 由 prop 驱动，方便在同一实例上「换档」——R2 的另一面：
 *  方法必须读**当次渲染**的 density，不得停在首次渲染那一档。 */
function ReasoningHarness({
  seen,
  density,
  tick,
}: {
  seen: ReasoningDisclosureApi[];
  density: TraceDensity;
  tick: number;
}) {
  const api = useReasoningDisclosure('session-1', density);
  seen.push(api);
  return <span data-tick={tick} />;
}

describe('F1 — useReasoningDisclosure 返回值的引用稳定性（R1，AC1）', () => {
  it('依赖未变的连续渲染返回**同一个对象**（R1）', () => {
    const seen: ReasoningDisclosureApi[] = [];
    mount(<ReasoningHarness seen={seen} density="balanced" tick={0} />);
    expect(seen.length).toBe(1);
    // 先证「依赖没变的重渲染不换引用」。
    rerender(<ReasoningHarness seen={seen} density="balanced" tick={1} />);
    expect(seen.length).toBe(2);
    const stable = latest(seen);
    expect(stable).toBe(seen[0]);
    // 再证「density 是依赖，换了就必须换引用」——两者合起来才是 R1 的完整口径。
    rerender(<ReasoningHarness seen={seen} density="compact" tick={2} />);
    expect(latest(seen)).not.toBe(stable);
  });

  it('稳定化后 isOpen 读**最新** density，不停在首次渲染那一档（R2，AC2）', () => {
    const seen: ReasoningDisclosureApi[] = [];
    mount(<ReasoningHarness seen={seen} density="balanced" tick={0} />);
    // balanced + streaming ⇒ 自动展开（S6）
    expect(latest(seen).isOpen('b1', 'streaming')).toBe(true);
    rerender(<ReasoningHarness seen={seen} density="compact" tick={1} />);
    // density 漏进依赖集合时，闭包会停在 balanced，这条必红。
    expect(latest(seen).isOpen('b1', 'streaming')).toBe(false); // compact 一行实况（PRD §9.1）
  });

  it('稳定化后 toggle 写入的 override 立刻可读，并压过自动规则（S7）', () => {
    const seen: ReasoningDisclosureApi[] = [];
    mount(<ReasoningHarness seen={seen} density="balanced" tick={0} />);
    act(() => latest(seen).toggle('b1', true)); // 手动收起
    expect(latest(seen).isOpen('b1', 'streaming')).toBe(false);
    act(() => latest(seen).toggle('b1', false)); // 手动展开
    expect(latest(seen).isOpen('b1', 'completed')).toBe(true); // 终态也保持手动展开
  });
});

describe('F1 — 夹具自检（防止上面的 R1 用例变成空断言）', () => {
  it('外部 prop 真的驱动了重渲染（渲染次数随 tick 增长）', () => {
    const seen: Disclosure[] = [];
    mount(<DisclosureHarness seen={seen} tick={0} />);
    const before = seen.length;
    rerender(<DisclosureHarness seen={seen} tick={1} />);
    expect(seen.length).toBe(before + 1);
  });
});
