/** Synthetic combined-sheet labels; never a claim about a client drawing. */
export function roleViewsFixture() {
  return ['arch', 'shop'].map((role, index) => ({
    view_id: `00000000-0000-4000-8000-00000000050${index + 1}`,
    page_index: 0, tag: `synthetic-panel-${index + 1}`, role: null, upload_side: null,
    suggested_role: role,
    suggested_from: role === 'arch' ? 'SYNTHETIC ID SET ELEVATION' : 'SYNTHETIC SHOP ELEVATION',
    reason: 'Synthetic printed heading; awaiting a person’s confirmation',
  }));
}
