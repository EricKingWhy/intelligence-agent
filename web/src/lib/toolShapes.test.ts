/** toolShapes 纯函数契约——后端 df4f7d8 工具结果标记的解析真值。
 * 标记格式以后端 HANDOFF_FRONTEND_SYNC.md §1.3 为准，零伪造：不匹配即 null。 */

import { describe, expect, it } from 'vitest';
import {
  GREP_TRUNCATED_SUFFIX,
  hasGrepTruncatedSuffix,
  parseReadShape,
  stripGrepTruncatedSuffix,
} from './toolShapes';

describe('parseReadShape — read 续读标记', () => {
  it('完整续读标记解析并从正文剥离', () => {
    const s = parseReadShape({
      content: 'line1\nline2\nline3\n[Showing lines 1-3 of 5000. Use offset=4 to continue.]',
      total_lines: 5000,
    });
    expect(s).not.toBeNull();
    expect(s!.content).toBe('line1\nline2\nline3');
    expect(s!.continuation).toEqual({ shownFrom: 1, shownTo: 3, totalLines: 5000, nextOffset: 4 });
    expect(s!.totalLines).toBe(5000);
    expect(s!.lineTruncated).toBeNull();
  });

  it('中间页标记：offset 与 shownTo+1 一致契约', () => {
    const s = parseReadShape({
      content: 'a\n[Showing lines 2001-4000 of 5000. Use offset=4001 to continue.]',
      total_lines: 5000,
    });
    expect(s!.continuation).toEqual({ shownFrom: 2001, shownTo: 4000, totalLines: 5000, nextOffset: 4001 });
  });

  it('无标记的普通内容原样透传', () => {
    const s = parseReadShape({ content: 'just a file\nwith lines', total_lines: 2 });
    expect(s!.content).toBe('just a file\nwith lines');
    expect(s!.continuation).toBeNull();
    expect(s!.totalLines).toBe(2);
  });

  it('total_lines 缺失 → null（不伪造）', () => {
    const s = parseReadShape({ content: 'x' });
    expect(s!.totalLines).toBeNull();
  });

  it('空文件 content:"" + total_lines:0 → 正常成功形状', () => {
    const s = parseReadShape({ content: '', total_lines: 0 });
    expect(s).not.toBeNull();
    expect(s!.content).toBe('');
    expect(s!.totalLines).toBe(0);
    expect(s!.continuation).toBeNull();
  });

  it('非对象 / 缺 content → null（回退 GenericBlock）', () => {
    expect(parseReadShape(null)).toBeNull();
    expect(parseReadShape('string result')).toBeNull();
    expect(parseReadShape({ total_lines: 3 })).toBeNull();
  });
});

describe('parseReadShape — 单行超长截断标记（不可续读）', () => {
  it('解析并从正文剥离', () => {
    const s = parseReadShape({
      content: "data\n[Line 2 truncated at 51200 bytes. Use bash with 'sed -n ...']",
      total_lines: 2,
    });
    expect(s!.lineTruncated).toEqual({ line: 2, bytes: 51200 });
    expect(s!.content).toBe('data');
    expect(s!.continuation).toBeNull();
  });

  it('截断字节计数进 bytes（格式化归展示层）', () => {
    const s = parseReadShape({ content: '[Line 1 truncated at 51200 bytes. Use bash with \'sed -n "51,100p"\']' });
    expect(s!.lineTruncated!.bytes).toBe(51200);
  });
});

describe('grep 截断尾巴', () => {
  it('行尾 ... [truncated] 检测', () => {
    expect(hasGrepTruncatedSuffix(`some matching line${GREP_TRUNCATED_SUFFIX}`)).toBe(true);
    expect(hasGrepTruncatedSuffix('normal line')).toBe(false);
    expect(hasGrepTruncatedSuffix('... [truncated] not at end')).toBe(false);
  });

  it('stripGrepTruncatedSuffix：带尾巴的行剥离尾巴、普通行原样返回', () => {
    expect(stripGrepTruncatedSuffix(`some matching line${GREP_TRUNCATED_SUFFIX}`)).toBe('some matching line');
    expect(stripGrepTruncatedSuffix('normal line')).toBe('normal line');
    expect(stripGrepTruncatedSuffix('')).toBe('');
  });
});
