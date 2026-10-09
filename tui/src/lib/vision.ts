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
 *
 * **两阶段查找，与服务端 `find_catalog_entry` 同序**（独立审查 P3：客户端把 `id` 与
 * `model` 混在一个 `find` 里时，数组顺序可能让与服务端不同的条目胜出 => 客户端假拒）：
 * 1. 先按 `id`（= 服务端 catalog 条目的 `name`）精确匹配，命中即采用：服务端第一阶段
 *    就是 `(provider, name)` 精确匹配，`name` 即 `/api/models` 条目的 `id`；
 * 2. 无 `id` 命中时再按 `model`（上游模型名）匹配：恰好一条 => 采用；**多条同名 => 歧义**，
 *    客户端没有 provider 信息、无法像服务端那样消歧 => **乐观返回 `true`**（不预检，
 *    把判定交给服务端 422），宁可放过也不假拒。
 */
export function resolveVisionSupport(models: ModelOptionView[], modelName: string | null): boolean {
  if (modelName === null) return models.find((m) => m.is_default === true)?.supports_vision === true;
  const byId = models.find((m) => m.id === modelName);
  if (byId !== undefined) return byId.supports_vision === true;
  const byModel = models.filter((m) => m.model === modelName);
  if (byModel.length > 1) return true;
  return byModel[0]?.supports_vision === true;
}
