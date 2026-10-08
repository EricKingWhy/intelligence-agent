import type { ModelCatalogEntry } from './api';
import type { ConversationState } from '../types';

export interface ActiveFallbackModelProjection {
  modelName: string;
  model: ModelCatalogEntry | null;
}

type FallbackProjectionState = Pick<
  ConversationState,
  'run_status' | 'run_id' | 'model_run_id' | 'model_fallback'
>;

export function projectActiveFallbackModel(
  conversation: FallbackProjectionState | null | undefined,
  models: readonly ModelCatalogEntry[],
): ActiveFallbackModelProjection | null {
  if (
    !conversation ||
    conversation.run_status !== 'running' ||
    !conversation.run_id ||
    conversation.model_run_id !== conversation.run_id ||
    !conversation.model_fallback
  ) {
    return null;
  }

  const modelName = conversation.model_fallback.to_model;
  const nameMatches = models.filter((model) => model.name === modelName);
  if (nameMatches.length === 1) return { modelName, model: nameMatches[0] };
  if (nameMatches.length > 1) return { modelName, model: null };

  const modelMatches = models.filter((model) => model.model === modelName);
  return {
    modelName,
    model: modelMatches.length === 1 ? modelMatches[0] : null,
  };
}
