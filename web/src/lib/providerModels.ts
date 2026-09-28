/** 供应商表单的模型列表规整（ADR-0032 §8.1）。
 *
 *  纯函数，放在 lib 而不是组件里：这是「哪些行发上去」的**契约**（空行丢弃、
 *  按 model_id 去重、label 随行保留），组件只负责渲染与收集输入。放组件内就
 *  只能靠 SSR 快照间接断言，而这里能直接钉住数据丢失类回归——整表替换式 PUT
 *  下漏掉 label 就等于静默删掉用户已有的显示名。
 */

export interface ProviderModelRow {
  model_id: string;
  label: string;
}

/** 表单行 → 请求体 models：trim、丢空行、按 model_id 去重（保序、首见优先）、
 *  label 为空则不发送该键（后端把缺省 label 当"无显示名"，与空串同义）。 */
export function normalizeProviderModels(
  rows: ProviderModelRow[],
): { model_id: string; label?: string }[] {
  const seen = new Set<string>();
  const out: { model_id: string; label?: string }[] = [];
  for (const row of rows) {
    const model_id = row.model_id.trim();
    if (!model_id || seen.has(model_id)) continue;
    seen.add(model_id);
    const label = row.label.trim();
    out.push(label ? { model_id, label } : { model_id });
  }
  return out;
}
