/**
 * AC3 状态机：#827 MM-06 编辑器内 `[Image #N]` 标记 + 待发图片数组，删标记即撤销。
 *
 * 语义 = PORT DESIGN（oh-my-pi `packages/tui/src/prompt/composer-attachments.ts:273`
 * `compactImageMarkers` @ `579da1d6`）：标记是**位置式**的（`[Image #N]` 对应
 * pendingImages[N-1]），提交时把仍被文本引用的图稠密重编号为 1..K，未被引用的
 * 图从待发数组里丢弃；引用不到任何保留图的**悬空标记**（手打 `[Image #9]`）删掉
 * （本仓在移植语义上的加强，独立审查 P4）。测试只断言外部可观察行为（文本 + 保留下标），
 * 不碰内部实现。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  compactDraftImages,
  imageMarker,
  parseImageMarkers,
  stripImageMarkers,
} from "../src/lib/pending-images.ts";

test("imageMarker：生成 [Image #N]，N 从 1 起", () => {
  assert.equal(imageMarker(1), "[Image #1]");
  assert.equal(imageMarker(3), "[Image #3]");
});

test("parseImageMarkers：按出现顺序给出序号（含重复）", () => {
  assert.deepEqual(parseImageMarkers("看这个 [Image #2] 和 [Image #1]"), [2, 1]);
  assert.deepEqual(parseImageMarkers("[Image #1][Image #1]"), [1, 1]);
  assert.deepEqual(parseImageMarkers("没有标记"), []);
});

test("全部标记都在：无需压缩（返回 null，调用方保持原样）", () => {
  assert.equal(compactDraftImages("[Image #1] 看看 [Image #2]", 2), null);
});

test("无待发图：不压缩（返回 null，文本里手打的 [Image #1] 原样保留）", () => {
  assert.equal(compactDraftImages("[Image #1]", 0), null);
});

test("删掉第 1 个标记：keep 只剩原数组下标 1，文本重编号为 [Image #1]", () => {
  const result = compactDraftImages("只剩 [Image #2] 了", 2);
  assert.deepEqual(result, { text: "只剩 [Image #1] 了", keep: [1] });
});

test("删光全部标记：keep 为空（提交内容不含任何图）", () => {
  const result = compactDraftImages("我把标记都删了", 2);
  assert.deepEqual(result, { text: "我把标记都删了", keep: [] });
});

test("悬空标记（手打 [Image #9]）在提交时删掉，被引用的合法标记照常重编号", () => {
  // 1 张待发图 + 正文手打 `[Image #9]`：9 号没有对应图片 => 删掉；1 号仍被引用且稠密，
  // 故只有悬空标记这一处改写（独立审查 P4：悬空标记不许原样发给模型）。
  assert.deepEqual(compactDraftImages("[Image #1] [Image #9]", 1), {
    text: "[Image #1]",
    keep: [0],
  });
});

test("悬空标记与它前面的行内空白一起删；整行只剩悬空标记时连空行一起删", () => {
  assert.deepEqual(compactDraftImages("看这个 [Image #7] 吧", 1), {
    text: "看这个 吧",
    keep: [],
  });
  assert.deepEqual(compactDraftImages("第一行\n[Image #7]\n第三行", 1), {
    text: "第一行\n第三行",
    keep: [],
  });
  // 用户自己写的空行原样保留（只删"因悬空标记而变空"的那一行）。
  assert.deepEqual(compactDraftImages("第一行\n\n第二行 [Image #7]", 1), {
    text: "第一行\n\n第二行",
    keep: [],
  });
});

test("stripImageMarkers：抹掉全部标记（含悬空/重复），其余文本与空白原样", () => {
  assert.equal(stripImageMarkers("[Image #1] 看图 [Image #2]"), " 看图 ");
  assert.equal(stripImageMarkers("[Image #99][Image #99]"), "");
  assert.equal(stripImageMarkers("没有标记"), "没有标记");
});

test("保留下标升序：文本里倒序引用不影响重编号结果", () => {
  // 3 张待发图，用户删掉了第 2 张的标记 ⇒ 保留原下标 0 与 2，文本重编号为 1/2。
  const result = compactDraftImages("[Image #3] 先说第三张 [Image #1]", 3);
  assert.deepEqual(result, { text: "[Image #2] 先说第三张 [Image #1]", keep: [0, 2] });
});
