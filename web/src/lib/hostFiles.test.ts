/**
 * #826（MM-05）AC1–AC3：宿主路径桥消费侧 + 拖入分流的契约。
 *
 * 这一层的价值在于它**同时**是「桌面行为」与「Web 行为」的判定点：桥缺席（浏览器）时
 * 分流必须把每个文件原样交给上传入口（Web 行为逐字不变），桥在场时才把「有真实路径的
 * 非图片文件」转成 `@path` 引用。所以正反两向都在这里钉住：
 * - 正向：桌面拖入 PDF → 引用、不上传；
 * - 反向（AC3 的负向安全面）：未主动选择的 File 拿不到路径 ⇒ 走上传，绝无"凭空造路径"。
 *
 * 桥本身的行为（谁拿到桥、`pathFor` 转给谁）在 `desktop/test/preload-host-paths.test.ts`
 * 用真 preload 源码 + 假 electron 跑；这里只负责消费侧，注入方式与页面里一致（全局）。
 */

import { afterEach, describe, expect, it } from 'vitest';
import {
  HOST_PATHS_GLOBAL,
  formatFileMention,
  hostPathFor,
  hostPathsBridge,
  routeHostFiles,
  type HostPathsBridge,
} from './hostFiles';

/** 页面里的注入方式：一个全局（`contextBridge` 也是往全局挂）。 */
function installBridge(bridge: unknown): void {
  Reflect.set(globalThis, HOST_PATHS_GLOBAL, bridge);
}

afterEach(() => {
  Reflect.deleteProperty(globalThis, HOST_PATHS_GLOBAL);
});

function file(name: string, type: string): File {
  return new File([new Uint8Array(0)], name, { type });
}

/** 桥只对"来自磁盘"的 File 返回路径——与 Electron `webUtils` 的契约同形。 */
const paths = new WeakMap<File, string>();
function diskFile(name: string, type: string, path: string): File {
  const dropped = file(name, type);
  paths.set(dropped, path);
  return dropped;
}

function testBridge(): HostPathsBridge {
  return { pathFor: (candidate) => paths.get(candidate) ?? '' };
}

describe('#826 hostPathsBridge — 形状判定', () => {
  it('全局缺席（Web 页面）→ undefined', () => {
    expect(hostPathsBridge()).toBeUndefined();
  });

  it('形状不对（非对象 / 缺 pathFor / pathFor 不是函数）→ undefined，不抛', () => {
    for (const bogus of [42, 'x', null, {}, { pathFor: 'nope' }, []]) {
      expect(hostPathsBridge({ [HOST_PATHS_GLOBAL]: bogus })).toBeUndefined();
    }
  });

  it('形状正确 → 可直接调用，且非字符串返回值被压成空串（桥的声明形状不被破坏）', () => {
    const bridge = hostPathsBridge({ [HOST_PATHS_GLOBAL]: { pathFor: () => 7 } });
    expect(bridge).toBeDefined();
    const dropped = file('a.pdf', 'application/pdf');
    expect(bridge?.pathFor(dropped)).toBe('');
  });
});

describe('#826 hostPathFor — 降级纪律', () => {
  it('无桥 → 空串（Web 页面的唯一答案）', () => {
    expect(hostPathFor(file('a.pdf', 'application/pdf'), undefined)).toBe('');
  });

  it('桥抛错 → 空串：拖入动作不能因为一个坏桥把界面炸掉', () => {
    const boom: HostPathsBridge = {
      pathFor: () => {
        throw new Error('bridge exploded');
      },
    };
    expect(hostPathFor(file('a.pdf', 'application/pdf'), boom)).toBe('');
  });

  it('正常桥 → 真实路径原样透传（不做归一化，改不动用户的路径）', () => {
    const dropped = diskFile('notes.pdf', 'application/pdf', 'C:\\Users\\u\\My Documents\\notes.pdf');
    expect(hostPathFor(dropped, testBridge())).toBe('C:\\Users\\u\\My Documents\\notes.pdf');
  });
});

describe('#826 formatFileMention — 引用语法', () => {
  it('无空白 → 裸写 @path', () => {
    expect(formatFileMention('/repo/src/a.ts')).toBe('@/repo/src/a.ts');
  });

  it('含空白 → 引号形态（否则引用在空格处断成两截）', () => {
    expect(formatFileMention('C:\\My Docs\\a.pdf')).toBe('@"C:\\My Docs\\a.pdf"');
  });

  it('控制字符或双引号 → null：调用方必须回退到上传，不许静默吞掉', () => {
    expect(formatFileMention('/repo/a\nb.ts')).toBeNull();
    expect(formatFileMention('/repo/a\u007fb.ts')).toBeNull();
    expect(formatFileMention('/repo/"quoted".ts')).toBeNull();
  });
});

describe('#826 routeHostFiles — AC2 分流', () => {
  it('无桥（Web）：每个文件都走上传，引用为空（Web 行为逐字不变）', () => {
    const pdf = file('notes.pdf', 'application/pdf');
    const png = file('shot.png', 'image/png');
    expect(routeHostFiles([pdf, png])).toEqual({ uploads: [pdf, png], references: [] });
  });

  it('有桥：非图片 → 引用（不上传）；图片 → 上传', () => {
    installBridge(testBridge());
    const pdf = diskFile('notes.pdf', 'application/pdf', '/repo/notes.pdf');
    const png = diskFile('shot.png', 'image/png', '/repo/shot.png');
    expect(routeHostFiles([pdf, png])).toEqual({ uploads: [png], references: ['@/repo/notes.pdf'] });
  });

  it('有桥：无真实路径的文件（剪贴板字节 / 页面里构造的 File）仍走上传', () => {
    installBridge(testBridge());
    const pastedBytes = file('pasted.png', 'image/png');
    const constructed = file('built.pdf', 'application/pdf');
    expect(routeHostFiles([pastedBytes, constructed])).toEqual({
      uploads: [pastedBytes, constructed],
      references: [],
    });
  });

  it('有桥 + 目录：强制走上传（本票不做目录引用，顶层按既有行为丢弃）', () => {
    installBridge(testBridge());
    const dir = diskFile('My Docs', '', '/repo/My Docs');
    expect(routeHostFiles([dir], { directories: new Set([dir]) })).toEqual({ uploads: [dir], references: [] });
  });

  it('有桥但路径不可表示（控制字符）：回退上传——交给既有入口给出可见的拒绝原因', () => {
    installBridge(testBridge());
    const weird = diskFile('a.pdf', 'application/pdf', '/repo/a\nb.pdf');
    expect(routeHostFiles([weird])).toEqual({ uploads: [weird], references: [] });
  });

  it('AC3 负向：桥对未主动选择的文件只给空串时，绝不产生引用（也没有任何路径被凭空造出）', () => {
    installBridge({ pathFor: (candidate: File) => (candidate.name === 'selected.pdf' ? '/repo/selected.pdf' : '') });
    const selected = file('selected.pdf', 'application/pdf');
    const unselected = file('secret.txt', 'text/plain');
    expect(routeHostFiles([unselected, selected])).toEqual({
      uploads: [unselected],
      references: ['@/repo/selected.pdf'],
    });
  });

  it('引用按用户拖入顺序排列（多条一次插入时顺序稳定）', () => {
    installBridge(testBridge());
    const first = diskFile('a.txt', 'text/plain', '/repo/a.txt');
    const second = diskFile('b.txt', 'text/plain', '/repo/b.txt');
    expect(routeHostFiles([first, second]).references).toEqual(['@/repo/a.txt', '@/repo/b.txt']);
  });
});
