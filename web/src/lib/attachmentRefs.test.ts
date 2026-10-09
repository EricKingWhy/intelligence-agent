/** `parseImageRefs` 的**坏形状容忍**契约（#825 / MM-04 / AC5）。
 *
 *  为什么单独钉：这是投影/恢复必经节点上的解析器，一行坏数据**不许** brick 整张会话
 *  视图（`docs/agents/implementation-discipline.md` 的边界纪律）。组件测试只喂合法形状，
 *  坏数据的行为没有任何别的车道覆盖——删掉"逐条跳过"改成"整批丢弃"时，只有这里会红。
 */

import { describe, expect, it } from 'vitest';
import { parseImageRefs } from './attachmentRefs';

describe('parseImageRefs', () => {
  const good = {
    kind: 'image',
    attachment_id: 'sha256:abc',
    media_type: 'image/png',
    bytes: 12,
    width: 8,
    height: 8,
  };

  it('合法条目原样保留，name 缺席落成 null（显示名回落由调用方决定）', () => {
    expect(parseImageRefs([good])).toEqual([{ ...good, name: null }]);
  });

  it('name 为串时保留；非串（数字/null）一律落 null，不原样外传', () => {
    expect(parseImageRefs([{ ...good, name: 'a.png' }])[0].name).toBe('a.png');
    expect(parseImageRefs([{ ...good, name: 7 }])[0].name).toBeNull();
    expect(parseImageRefs([{ ...good, name: null }])[0].name).toBeNull();
  });

  it('坏条目**逐条**跳过，好条目照样出来（不整批丢）', () => {
    const refs = parseImageRefs([
      null,
      'x',
      { ...good, kind: 'file' },
      { ...good, attachment_id: '' },
      { ...good, media_type: 3 },
      { ...good, bytes: Number.NaN },
      { ...good, width: '8' },
      { ...good, height: Number.POSITIVE_INFINITY },
      good,
    ]);
    expect(refs).toHaveLength(1);
    expect(refs[0].attachment_id).toBe('sha256:abc');
  });

  it('非数组（旧事件 / 字段缺席）→ 空数组，不抛', () => {
    expect(parseImageRefs(undefined)).toEqual([]);
    expect(parseImageRefs(null)).toEqual([]);
    expect(parseImageRefs({ kind: 'image' })).toEqual([]);
  });
});
