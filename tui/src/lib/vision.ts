/**
 * 模型视觉能力判定（AC8）。
 *
 * **本仓自有实现**（无上游复制）：只用一条外部可观察契约 -- `GET /api/models` 的条目
 * 与 `run/started.data.model`。
 *
 * 判定口径与 MM-03 服务端**同一单点**（`src/agent_harness/model/config.py:299
 * model_supports_vision`）：catalog 条目显式声明 > provider preset > **False（未知不猜）**。
 * `GET /api/models` 的条目只在能力位已知时才带 `supports_vision` 键
 * （`web/catalog.py:_render_model_option` 的 `if cap_key in capabilities`），因此
 * 「键缺席」= 服务端也会判 False => 客户端同样失败关闭，与服务端 422 门禁不会互相打脸。
 *
 * 只做客户端**预检**（把"发出去才 422"提前成"提交前明确拒绝"）；服务端 422 仍是权威。
 */

export interface ModelOptionView {
  id: string;
  /** 上游模型名（`GET /api/models` 的 `model` 字段，与 `run/started.data.model` 同源）。 */
  model?: string;
  is_default?: boolean;
  supports_vision?: boolean;
}

/**
 * 本次请求的模型是否支持视觉。
 *
 * `modelName` 来自会话最新一条 `run/started.data.model`（= 装配层真正发往 provider 的
 * `model` 字段）。会话还没有 run（全新会话）时传 `null` => 用目录里的 `is_default` 条目
 * （不传 model 参数时服务端用的就是默认链）。解析不到任何条目 => `false`（不猜）。
 */
export function resolveVisionSupport(models: ModelOptionView[], modelName: string | null): boolean {
  const entry =
    modelName === null
      ? models.find((m) => m.is_default === true)
      : models.find((m) => m.model === modelName || m.id === modelName);
  return entry?.supports_vision === true;
}
