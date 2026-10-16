import type { ArchitectCompareCounts, ArchitectCompareEntry, ArchitectCompareResult, ItemAction } from '@/api/review-items';

// The "Compared with the architect" section's words and groups: what the server sent, never worked out.

/** The four groups, in the order they are shown. "Need you" holds unpaired and unread numbers. */
export type CompareGroup = 'different' | 'need_you' | 'rule' | 'same';

export const GROUP_OF: Record<ArchitectCompareResult, CompareGroup> = {
  different: 'different',
  unpaired: 'need_you',
  unread: 'need_you',
  rule: 'rule',
  same: 'same',
};

export const GROUP_TITLE: Record<CompareGroup, string> = {
  different: 'Different',
  need_you: 'Need you',
  rule: 'Checked by a rule',
  same: 'Same',
};

/** The counts the summary line prints: the server's own, else counted from the list (never silent). */
export function compareCounts(entries: readonly ArchitectCompareEntry[], sent: ArchitectCompareCounts | null): ArchitectCompareCounts {
  if (sent) return sent;
  const counts: ArchitectCompareCounts = { same: 0, different: 0, rule: 0, unpaired: 0, unread: 0 };
  for (const entry of entries) counts[entry.result] += 1;
  return counts;
}

/** "94 numbers compared: 80 same · 3 different · 6 checked by a rule · 5 need you". */
export function compareSummary(counts: ArchitectCompareCounts): string {
  const needYou = counts.unpaired + counts.unread;
  const total = counts.same + counts.different + counts.rule + needYou;
  const parts = [`${counts.same} same`, `${counts.different} different`, `${counts.rule} checked by a rule`, `${needYou} need${needYou === 1 ? 's' : ''} you`];
  return `${total} ${total === 1 ? 'number' : 'numbers'} compared: ${parts.join(' · ')}`;
}

/** What one entry is about, in words: the line the side-by-side viewer labels its rings with. */
export function compareWhat(entry: ArchitectCompareEntry): string {
  if (entry.vendor_text) return `Vendor ${entry.vendor_text} vs architect ${entry.architect_text}`;
  return `Architect ${entry.architect_text}`;
}


/** The one-click answers an entry may offer, in the reviewer's words. Others the server lists are ignored. */
export const ANSWER_WORDS: Partial<Record<ItemAction, string>> = {
  agree: 'Agree: the vendor is wrong',
  not_this: 'Not a difference',
  checked_right: 'Checked by hand: right',
  checked_wrong: 'Checked by hand: wrong',
};

/** The keys "Clear all as not compared" names: every open entry with no match or not read. */
export function notComparedKeys(entries: readonly ArchitectCompareEntry[]): string[] {
  return entries.filter((entry) => GROUP_OF[entry.result] === 'need_you' && entry.status === 'open').map((entry) => entry.key);
}
