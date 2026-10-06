/** A filter miss must never claim that the rulebook itself is empty. */
export function rulebookEmptyState(total: number) {
  return total === 0
    ? { title: 'No rules are published', message: 'No published rules were returned. There are no rule snapshots to display.' }
    : { title: 'No rules match this category', message: 'Published rules are available in other categories. Select All to see them.' };
}
