import type { NotCheckedItem, NotCheckedSetting } from '@/api/review-items';

/** One setting a check's run lacked that is held now, and by what (#1403). */
export interface HeldSetting {
  setting: NotCheckedSetting;
  value: string;
  from: 'gv_standard' | 'project';
}

/**
 * The not-checked group in a few words (#1403): how many places wait, for which settings, and which
 * of those settings are held now. Built only from the settings the server names for each check
 * (`settings`, `set_now`, `value_now`, `value_from`): the screen never works out which setting a
 * check needs, and never a value.
 */
export interface NotCheckedSummary {
  /**
   * Countertops (a live countertop, else a countertop row) still waiting for a setting nobody holds;
   * checks instead when one of them names neither.
   */
  places: number;
  placeWord: 'countertop' | 'check';
  /** The settings nobody holds yet, each once, in the order the list first names them. */
  waiting: NotCheckedSetting[];
  /** The settings held since the checks ran: GV's standard or this job's own value. */
  heldSince: HeldSetting[];
  /** True when any setting named here is held by a GV standard now. */
  anyGvStandard: boolean;
}

/** The summary of the open not-checked checks, or null when none names a setting. */
export function notCheckedSummary(items: readonly NotCheckedItem[]): NotCheckedSummary | null {
  const waiting = new Map<string, NotCheckedSetting>();
  const heldSince = new Map<string, HeldSetting>();
  const places = new Set<string>();
  let everyHasCountertop = true;
  for (const item of items) {
    if (item.status !== 'open') continue;
    const settings = item.settings ?? [];
    let waits = false;
    for (const setting of settings) {
      if (!setting.set_now) {
        waits = true;
        if (!waiting.has(setting.key)) waiting.set(setting.key, setting);
      } else if (setting.value_now && setting.value_from && !heldSince.has(setting.key)) {
        heldSince.set(setting.key, { setting, value: setting.value_now, from: setting.value_from });
      }
    }
    if (!waits) continue;
    // One place per countertop: its live item, else its countertop row, else the check alone.
    if (item.countertop) places.add(`countertop:${item.countertop.item_id}`);
    else if (item.row_label) places.add(`row:${item.page ?? ''}:${item.row_label}`);
    else {
      everyHasCountertop = false;
      places.add(`check:${item.key}`);
    }
  }
  if (waiting.size === 0 && heldSince.size === 0) return null;
  const held = [...heldSince.values()];
  return {
    places: places.size,
    placeWord: everyHasCountertop ? 'countertop' : 'check',
    waiting: [...waiting.values()],
    heldSince: held,
    anyGvStandard: held.some((entry) => entry.from === 'gv_standard'),
  };
}

/** "A", "A and B", "A, B and C". */
export function andList(words: readonly string[]): string {
  return words.length < 2 ? words.join('') : `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`;
}
