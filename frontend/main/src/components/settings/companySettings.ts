import type { components } from '../../api/schema';

/** One of GV's standard numbers, as `GET /company-settings` lists it (#812). */
export type CompanySetting = components['schemas']['CompanySettingOut'];

/** `cabinet_side_thickness` → `Cabinet side thickness`. The code name is shown beside it. */
export function settingLabel(name: string): string {
  const words = name.replace(/_/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Where the number a check starts from comes from, said the way an admin reads it. */
export function inUseSentence(setting: CompanySetting): string {
  if (setting.in_use === null || setting.in_use === undefined) {
    return 'Not set — checks that need it say "not found" until it is.';
  }
  if (setting.in_use_from === 'company') {
    const who = setting.company_set_by ? ` by ${setting.company_set_by}` : '';
    const when = setting.company_set_at
      ? ` on ${new Date(setting.company_set_at).toLocaleDateString()}`
      : '';
    return `${setting.in_use} — GV's standard, set${who}${when}.`;
  }
  return `${setting.in_use} — the rulebook's default, until GV sets its own.`;
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
