export function resolveFocusReturnTarget(active: Element | null): HTMLElement | null {
  if (!(active instanceof HTMLElement)) return null;
  const menu = active.closest('[role="menu"]');
  if (!menu) return active;
  const triggerId = menu.getAttribute('aria-labelledby')?.split(' ')[0];
  const trigger = triggerId ? document.getElementById(triggerId) : null;
  return trigger instanceof HTMLElement ? trigger : null;
}

export function restoreFocusTarget(target: HTMLElement | null): boolean {
  const toolbarTrigger = document.querySelector<HTMLElement>('.appbar-more-trigger');
  for (const candidate of [target, toolbarTrigger]) {
    if (
      !candidate
      || candidate === document.body
      || !candidate.isConnected
      || candidate.matches(':disabled')
    ) {
      continue;
    }
    candidate.focus();
    if (document.activeElement === candidate) return true;
  }
  return false;
}
