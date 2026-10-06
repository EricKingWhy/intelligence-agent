// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, type ReactElement, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import type { PendingConstraintInputRequest } from '../types';
import { ConstraintResolutionDialog } from './ConstraintResolutionDialog';

function request(overrides: Partial<PendingConstraintInputRequest> = {}): PendingConstraintInputRequest {
  return {
    request_id: 'request-1',
    run_id: 'run-1',
    fact_id: 'fact-1',
    old_value: 'Use Python',
    candidate: 'Use TypeScript',
    question: 'Choose how to resolve this conflict.',
    source_event_id: 'source-1',
    source_event_seq: 1,
    choices: [
      { id: 'replace_persistently', label: 'Replace persistently' },
      { id: 'current_task_only', label: 'This task only' },
      { id: 'keep_existing', label: 'Keep existing' },
      { id: 'custom', label: 'Custom reply' },
    ],
    answer: null,
    ...overrides,
  };
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function render(ui: ReactElement): void {
  if (!container) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }
  act(() => root!.render(ui));
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
});

describe('ConstraintResolutionDialog', () => {
  it('shows the durable conflict and submits a selected choice', () => {
    const onSubmit = vi.fn();
    render(
      <ConstraintResolutionDialog
        request={request()}
        open
        canClose
        resuming={false}
        errorMessage={null}
        onOpenChange={() => {}}
        onSubmit={onSubmit}
      />,
    );

    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain('Use Python');
    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain('Use TypeScript');
    const choice = document.querySelector('input[value="current_task_only"]') as HTMLInputElement;
    act(() => choice.click());
    const submit = document.querySelector('.constraint-input-dialog .project-btn-primary') as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
    act(() => submit.click());
    expect(onSubmit).toHaveBeenCalledWith({
      request_id: 'request-1',
      choice: 'current_task_only',
    });
  });

  it('submits custom text only with the custom choice', () => {
    const onSubmit = vi.fn();
    render(
      <ConstraintResolutionDialog
        request={request()}
        open
        canClose
        resuming={false}
        errorMessage={null}
        onOpenChange={() => {}}
        onSubmit={onSubmit}
      />,
    );
    const custom = document.querySelector('input[value="custom"]') as HTMLInputElement;
    act(() => custom.click());
    const textarea = document.querySelector('textarea') as HTMLTextAreaElement;
    const setter = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      'value',
    )?.set;
    act(() => {
      setter?.call(textarea, 'Keep the existing rule for future tasks.');
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const submit = document.querySelector('.constraint-input-dialog .project-btn-primary') as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
    act(() => submit.click());
    expect(onSubmit).toHaveBeenCalledWith({
      request_id: 'request-1',
      choice: 'custom',
      custom_text: 'Keep the existing rule for future tasks.',
    });
  });

  it('keeps a committed answer read-only and can be reopened after dismissal', () => {
    function Host() {
      const [open, setOpen] = useState(true);
      return (
        <ConstraintResolutionDialog
          request={request({
            answer: { request_id: 'request-1', choice: 'custom', custom_text: 'Use the new rule for this task.' },
          })}
          open={open}
          canClose
          resuming={false}
          errorMessage={null}
          onOpenChange={setOpen}
          onSubmit={() => {}}
        />
      );
    }

    render(<Host />);
    expect(document.querySelector('input[type="radio"]')).toBeNull();
    act(() => {
      document.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
      );
    });
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    const reopen = document.querySelector('.constraint-input-pending button') as HTMLButtonElement;
    act(() => reopen.click());
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain('Use the new rule for this task.');
  });
});
