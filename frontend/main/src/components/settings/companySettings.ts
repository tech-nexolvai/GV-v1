import type { components } from '../../api/schema';

/** One of GV's standard numbers, as `GET /company-settings` lists it (#812). */
export type CompanySetting = components['schemas']['CompanySettingOut'];

/** `cabinet_side_thickness` → `Cabinet side thickness`. The code name is shown beside it. */
export function settingLabel(name: string): string {
  const words = name.replace(/_/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A date with the month as a word ("9 Oct 2026", or "Oct 9, 2026" in US English), never 09/10/2026. */
export function settingDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

/** Where the number a check starts from comes from, said the way an admin reads it. */
export function inUseSentence(setting: CompanySetting): string {
  if (setting.in_use === null || setting.in_use === undefined) {
    return 'Not set — checks that need it say "not found" until it is.';
  }
  if (setting.in_use_from === 'company') {
    const who = setting.company_set_by ? ` by ${setting.company_set_by}` : '';
    const when = setting.company_set_at
      ? ` on ${settingDate(setting.company_set_at)}`
      : '';
    return `${setting.in_use} — GV's standard, set${who}${when}.`;
  }
  return `${setting.in_use} — the rulebook's default, until GV sets its own.`;
}

/** For the meter (#1072): how many GV has set, how many use the rulebook's default, how many have a value. */
export function settingCounts(settings: readonly CompanySetting[]): { total: number; inUse: number; byCompany: number; byRulebook: number } {
  return {
    total: settings.length,
    inUse: settings.filter((setting) => setting.in_use != null).length,
    byCompany: settings.filter((setting) => setting.in_use != null && setting.in_use_from === 'company').length,
    byRulebook: settings.filter((setting) => setting.in_use != null && setting.in_use_from === 'rulebook').length,
  };
}

/**
 * The values to send: only what was typed, trimmed, and only where it is non-empty.
 *
 * A blank box means "leave it as it is" — the server carries every value not sent forward — so a
 * save never removes a standard somebody set before.
 */
export function changedValues(drafts: Record<string, string>): { name: string; value: string }[] {
  return Object.entries(drafts)
    .map(([name, value]) => ({ name, value: value.trim() }))
    .filter((entry) => entry.value !== '')
    .sort((left, right) => left.name.localeCompare(right.name));
}

/**
 * A plain-words hint for a typed value the server would refuse for want of a unit (#1369), or null.
 *
 * Only the two obvious cases: no number at all, and a number with no unit (`2`, `1 1/2`). Anything
 * else goes to the server, which stays the authority and says what is wrong in its own words.
 */
export function unitHint(typed: string): string | null {
  const value = typed.trim();
  if (value === '') return null;
  if (!/\d/.test(value)) return 'Type a number with its unit, for example 1 1/2" or 38 mm.';
  if (/^-?(\d+(\.\d+)?([\s-]+\d+\/\d+)?|\d+\/\d+)$/.test(value)) return 'Add the unit, for example 1 1/2" or 38 mm.';
  return null;
}

/** The row a "Not checked" link points at stays marked this long after it is focused (#1369). */
export const HIGHLIGHT_MS = 2500;

/**
 * Open Company settings on one setting's box (#1369): a project's box first (the named project's, or
 * the first project listing it), else GV's standard. Opens a closed list around it, scrolls it into
 * view, focuses it and marks its row for a moment. Returns false when no box on the page is for it.
 */
export function focusSettingField(root: ParentNode, name: string, project?: string): boolean {
  // Inside a quoted attribute value only `"` and `\` need escaping (and jsdom has no CSS.escape).
  const quoted = (text: string) => text.replace(/["\\]/g, '\\$&');
  const field = `[data-setting-field="${quoted(name)}"]`;
  const box =
    (project ? root.querySelector<HTMLElement>(`[data-project="${quoted(project)}"] ${field}`) : null) ??
    root.querySelector<HTMLElement>(`[data-project] ${field}`) ??
    root.querySelector<HTMLElement>(`[data-part="company-standards"] ${field}`);
  if (box === null) return false;
  const list = box.closest('details');
  if (list !== null) list.open = true;
  const reduced = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  box.scrollIntoView?.({ block: 'center', behavior: reduced ? 'auto' : 'smooth' });
  box.focus({ preventScroll: true });
  const row = box.closest('tr');
  if (row !== null) {
    row.setAttribute('data-highlight', 'true');
    window.setTimeout(() => row.removeAttribute('data-highlight'), HIGHLIGHT_MS);
  }
  return true;
}
