/**
 * AC8：模型不支持视觉时提交前拒绝（复用 MM-03 的 `supports_vision` 口径）。
 *
 * 口径来源（`src/agent_harness/model/config.py:299 model_supports_vision`）：
 * catalog 条目显式声明 > provider preset > **False（未知不猜）**。`GET /api/models`
 * 的条目只在能力位**已知**时才带 `supports_vision` 键（`web/catalog.py:_render_model_option`
 * 的 `if cap_key in capabilities`），所以「键缺席」= 服务端也会判 False ⇒ 客户端同样
 * 失败关闭。本条与 `web/` 侧 #825 的「模型不支持视觉时禁止附图」是同一判定。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { resolveVisionSupport } from "../src/lib/vision.ts";

const MODELS = [
  { id: "vision-a", model: "vision-a", is_default: true, supports_vision: true },
  { id: "text-b", model: "text-b", is_default: false, supports_vision: false },
  { id: "unknown-c", model: "unknown-c", is_default: false },
];

test("显式声明支持视觉 ⇒ true", () => {
  assert.equal(resolveVisionSupport(MODELS, "vision-a"), true);
});

test("显式声明不支持视觉 ⇒ false（提交前拒绝）", () => {
  assert.equal(resolveVisionSupport(MODELS, "text-b"), false);
});

test("能力位缺席（未知）⇒ false（不猜，与服务端同一失败关闭口径）", () => {
  assert.equal(resolveVisionSupport(MODELS, "unknown-c"), false);
});

test("模型不在目录里 ⇒ false（不猜）", () => {
  assert.equal(resolveVisionSupport(MODELS, "nope"), false);
});

test("会话模型未知（尚无 run/started）⇒ 按 is_default 条目判定", () => {
  assert.equal(resolveVisionSupport(MODELS, null), true);
  assert.equal(
    resolveVisionSupport([{ id: "d", model: "d", is_default: true, supports_vision: false }], null),
    false,
  );
});

test("按 id 也能命中（会话模型可能是 id 形态，如自定义供应商 `prov:model`）", () => {
  assert.equal(
    resolveVisionSupport([{ id: "prov:x", model: "x", supports_vision: true }], "prov:x"),
    true,
  );
});

test("id 未命中时按 model（上游模型名）命中单一条目", () => {
  const models = [{ id: "catalog-name", model: "upstream-model", supports_vision: true }];
  assert.equal(resolveVisionSupport(models, "upstream-model"), true);
  assert.equal(
    resolveVisionSupport([{ id: "catalog-name", model: "upstream-model", supports_vision: false }], "upstream-model"),
    false,
  );
});

test("id 精确命中优先于数组更靠前的 model 命中（与服务端 name 优先同序，避免假拒）", () => {
  const models = [
    { id: "other", model: "target", supports_vision: false },
    { id: "target", model: "upstream", supports_vision: true },
  ];
  assert.equal(resolveVisionSupport(models, "target"), true, "id 命中的那份才是权威条目");
});

test("多条同名 model（客户端无 provider 消歧）⇒ 乐观放行，把判定交给服务端 422", () => {
  const models = [
    { id: "prov-a", model: "same", supports_vision: false },
    { id: "prov-b", model: "same", supports_vision: true },
  ];
  assert.equal(resolveVisionSupport(models, "same"), true, "歧义不许假拒");
});
