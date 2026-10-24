// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { CopyButton } from './CopyButton';

describe('CopyButton fallback', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    delete (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT;
    Reflect.deleteProperty(document, 'execCommand');
  });

  it('only exposes copied state when execCommand succeeds', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined });
    const execCommand = vi.fn().mockReturnValue(false);
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true });

    await act(async () => {
      root.render(<CopyButton text="some text" label="Copy text" />);
    });
    const button = container.querySelector('button.copy-btn') as HTMLButtonElement;

    await act(async () => {
      button.click();
    });

    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(button.classList.contains('copied')).toBe(false);
    expect(button.querySelector('.copy-btn-label')).toBeNull();
    expect(button.getAttribute('aria-label')).toBe('Copy text');

    execCommand.mockReturnValue(true);
    await act(async () => {
      button.click();
    });

    expect(button.classList.contains('copied')).toBe(true);
    expect(button.querySelector('.copy-btn-label')).not.toBeNull();

    execCommand.mockReturnValue(false);
    await act(async () => {
      button.click();
    });

    expect(button.classList.contains('copied')).toBe(false);
    expect(button.querySelector('.copy-btn-label')).toBeNull();
    expect(button.getAttribute('aria-label')).toBe('Copy text');
  });
});
