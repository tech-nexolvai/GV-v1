/**
 * What the Measurements form already holds on the server, and where each value came from (#1183).
 *
 * `GET .../required-inputs` returns the values a reviewer saved (`saved_measurements`,
 * `saved_parameters`, #1074), but the form never read them: after a reload every box was empty
 * although the checks would still use the saved numbers. These are the pure rules the form uses to
 * put them back, to say who typed them, and to leave an unchanged value out of the next save.
 *
 * **Nothing here saves or chooses anything.** A saved value is shown because the server holds it.
 * A company setting or a rulebook default is shown as what a blank box uses, never put in the box.
 */
import type { components } from '@/api/schema';
import { settingDate, type CompanySetting } from '@/components/settings/companySettings';
import { mixedFractionsIn, mixedQuantity } from './fractions';

export type SavedValue = components['schemas']['SavedValueOut'];
export type SavedQuantity = components['schemas']['SavedQuantityOut'];
export type SavedParameter = components['schemas']['SavedParameterOut'];

/** A saved number as the form's box takes it: the server's own text, which reads back exactly. */
export function savedText(value: SavedValue): string {
  return value.text ?? mixedQuantity(value);
}

/** "typed by ui reviewer, 11 Oct 2026": who saved a value and when, in the reviewer's words. */
export function typedBy(setBy: string, setAt: string): string {
  return `typed by ${setBy}, ${settingDate(setAt)}`;
}

export function savedQuantitiesByKey(saved: readonly SavedQuantity[] | undefined): Record<string, SavedQuantity> {
  return Object.fromEntries((saved ?? []).map((entry) => [entry.key, entry]));
}

export function savedParametersByName(saved: readonly SavedParameter[] | undefined): Record<string, SavedParameter> {
  return Object.fromEntries((saved ?? []).map((entry) => [entry.name, entry]));
}

/** The saved texts of a quantity, in layout order for a run. */
export function savedTexts(saved: SavedQuantity): string[] {
  return saved.values.map(savedText);
}

/** Whether what a box shows is exactly the saved value (the same text the server wrote back). */
export function showsSaved(current: string | readonly string[], saved: readonly string[] | null | undefined): boolean {
  if (!saved || saved.length === 0) return false;
  const shown = (typeof current === 'string' ? [current] : [...current]).map((value) => value.trim()).filter(Boolean);
  return shown.length === saved.length && shown.every((value, index) => value === saved[index].trim());
}

/**
 * Whether a measurement may be left out of a save because the server already holds it, unchanged,
 * for every check it feeds. The server carries every value a save does not send (#799, #801), so
 * leaving it out keeps who typed it and when; sending it again would restamp it as typed now.
 * A value held for only some of its checks (`complete: false`) is sent, so all of them get it.
 */
export function measurementUnchanged(current: string | readonly string[], saved: SavedQuantity | undefined): boolean {
  return saved !== undefined && saved.complete && showsSaved(current, savedTexts(saved));
}

/** One setting as `settingEntry` builds it for the wire. */
export interface SettingWire {
  value: string;
  scope: 'project' | 'run';
  source?: string;
  reference?: string;
  citation?: string;
}

/** Whether a setting may be left out of a save: same value, layer, source, reference and passage. */
export function settingUnchanged(entry: SettingWire, saved: SavedParameter | undefined): boolean {
  if (!saved || saved.layer !== entry.scope) return false;
  if (!showsSaved(entry.value, [savedText(saved.value)])) return false;
  if ((entry.citation ?? null) !== (saved.citation ?? null)) return false;
  if (entry.citation) return true;
  // No source sent means the server records its own default, as it did when this was saved.
  if (entry.source !== undefined && entry.source !== saved.source) return false;
  return (entry.reference ?? '').trim() === (saved.reference ?? '').trim();
}

/** What a blank setting uses when the checks run, and where that number comes from. */
export type BlankUses =
  | { kind: 'company'; value: string; setBy: string | null; setAt: string | null }
  | { kind: 'rulebook'; value: string }
  /** The rulebook has a default, but whether GV recorded its own could not be read. */
  | { kind: 'unknown'; value: string };

/**
 * A blank setting falls through to GV's company setting, then to the rulebook's default
 * (`workflow/stages._layered`). `GET /company-settings` says which is in use; `declared_default` alone
 * is the rulebook's, and ignores the company layer, so it is used only where the company list has no
 * entry for this name, or could not be read (said as `unknown`).
 */
export function blankUses(
  parameter: { name: string; declared_default: string | null },
  company: readonly CompanySetting[] | null,
): BlankUses | null {
  const listed = company?.find((setting) => setting.name === parameter.name);
  if (listed && listed.in_use != null) {
    const value = mixedFractionsIn(listed.in_use);
    return listed.in_use_from === 'company'
      ? { kind: 'company', value, setBy: listed.company_set_by ?? null, setAt: listed.company_set_at ?? null }
      : { kind: 'rulebook', value };
  }
  if (!parameter.declared_default) return null;
  const value = mixedFractionsIn(parameter.declared_default);
  return company === null ? { kind: 'unknown', value } : { kind: 'rulebook', value };
}

/** The source of a company setting in use, in words: "GV standard, set by … on …" or "rulebook default". */
export function companySourceWords(setting: CompanySetting): string {
  if (setting.in_use_from === 'company') {
    const who = setting.company_set_by ? ` by ${setting.company_set_by}` : '';
    const when = setting.company_set_at ? ` on ${settingDate(setting.company_set_at)}` : '';
    return `GV company setting, set${who}${when}`;
  }
  return 'rulebook default';
}

/** What the server holds for a form, as `required-inputs` sends it. */
export interface SavedSnapshot {
  saved_measurements?: readonly SavedQuantity[];
  saved_parameters?: readonly SavedParameter[];
}

/** One saved value that changed on the server after the form was loaded: someone else saved it. */
export interface SavedChange {
  /** `q:<quantity key>` or `p:<setting name>`, the id the form keeps a reviewer's choice under. */
  id: string;
  kind: 'quantity' | 'setting';
  key: string;
  text: string;
  setBy: string;
  setAt: string;
}

function quantityStamp(saved: SavedQuantity | undefined): string {
  return saved ? JSON.stringify([savedTexts(saved), saved.set_by, saved.set_at, saved.complete]) : '';
}

function settingStamp(saved: SavedParameter | undefined): string {
  return saved
    ? JSON.stringify([saved.layer, savedText(saved.value), saved.value.set_by, saved.value.set_at, saved.source, saved.reference ?? null, saved.citation ?? null])
    : '';
}

/**
 * Every saved value that differs between the form's snapshot and a fresh read (#1183): a value
 * someone saved, or changed, after this form was opened. The form must not skip or overwrite one
 * of these without the reviewer seeing it.
 */
export function savedChangesSince(before: SavedSnapshot, after: SavedSnapshot): SavedChange[] {
  const changes: SavedChange[] = [];
  const oldQuantities = savedQuantitiesByKey(before.saved_measurements);
  for (const saved of after.saved_measurements ?? []) {
    if (quantityStamp(oldQuantities[saved.key]) === quantityStamp(saved)) continue;
    changes.push({ id: `q:${saved.key}`, kind: 'quantity', key: saved.key, text: savedTexts(saved).join(', '), setBy: saved.set_by, setAt: saved.set_at });
  }
  const oldSettings = savedParametersByName(before.saved_parameters);
  for (const saved of after.saved_parameters ?? []) {
    if (settingStamp(oldSettings[saved.name]) === settingStamp(saved)) continue;
    changes.push({ id: `p:${saved.name}`, kind: 'setting', key: saved.name, text: savedText(saved.value), setBy: saved.value.set_by, setAt: saved.value.set_at });
  }
  return changes;
}
