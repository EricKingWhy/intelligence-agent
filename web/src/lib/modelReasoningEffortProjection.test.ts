import { describe, expect, it } from 'vitest';
import type { ModelCatalogEntry } from './api';
import { projectActiveFallbackModel } from './modelReasoningEffortProjection';

const fallback: ModelCatalogEntry = {
  name: 'backup',
  provider: 'p',
  model: 'backup-id',
  default: false,
};

const active = {
  run_status: 'running' as const,
  run_id: 'run-1',
  model_run_id: 'run-1',
  model_fallback: { from_model: 'primary-id', to_model: 'backup-id', reason: 'TimeoutError' },
};

describe('projectActiveFallbackModel', () => {
  it('resolves the event model id only when the active run owns the fallback event', () => {
    expect(projectActiveFallbackModel(active, [fallback])).toEqual({
      modelName: 'backup-id',
      model: fallback,
    });
    expect(projectActiveFallbackModel({ ...active, model_run_id: 'old-run' }, [fallback])).toBeNull();
    expect(projectActiveFallbackModel({ ...active, run_status: 'completed' }, [fallback])).toBeNull();
  });

  it('keeps an active projection but does not guess when the catalog match is absent or ambiguous', () => {
    expect(projectActiveFallbackModel(active, [])).toEqual({ modelName: 'backup-id', model: null });
    expect(projectActiveFallbackModel(active, [fallback, { ...fallback, name: 'other' }])).toEqual({
      modelName: 'backup-id',
      model: null,
    });
  });

  it('matches an exact catalog name before checking provider model ids', () => {
    const named = { ...fallback, name: 'backup-id', model: 'different-id' };
    expect(projectActiveFallbackModel(active, [named])).toEqual({
      modelName: 'backup-id',
      model: named,
    });
  });
});
