export type MeasurementSection = { key: string; label: string };

export function measurementSections(sources: MeasurementSection[], layout: boolean, stored: boolean): MeasurementSection[] {
  return [
    { key: 'overview', label: 'Reading overview' },
    ...sources.map((source) => ({ key: `source:${source.key}`, label: source.label })),
    { key: 'settings', label: 'Settings' },
    { key: 'distribution', label: 'Filler distribution' },
    ...(layout ? [{ key: 'layout', label: 'Layout' }] : []),
    ...(stored ? [{ key: 'stored', label: 'Stored values' }] : []),
    { key: 'actions', label: 'Save / run controls' },
  ];
}

/** Scroll and focus only, scoped to this form. No hash routing, field edits or requests. */
export function jumpToMeasurementSection(container: HTMLElement | null, key: string): void {
  if (!container) return;
  const target = Array.from(container.querySelectorAll<HTMLElement>('[data-measure-section]'))
    .find((element) => element.dataset.measureSection === key);
  if (!target) return;
  const navigation = container.querySelector<HTMLElement>('.measurement-nav');
  const view = container.ownerDocument?.defaultView;
  const padding = Number.parseFloat(view?.getComputedStyle(container).paddingTop ?? '') || 0;
  const gap = navigation ? Number.parseFloat(view?.getComputedStyle(navigation).marginBottom ?? '') || 0 : 0;
  const top = container.scrollTop + target.getBoundingClientRect().top
    - container.getBoundingClientRect().top - (navigation?.offsetHeight ?? 0) - padding - gap;
  target.focus({ preventScroll: true });
  container.scrollTo({ top: Math.max(0, top), behavior: 'instant' });
}
