// Invented UI records only. No client drawing, geometry decision or production tolerance.
const id = suffix => `00000000-0000-4000-8000-000000000${suffix}`;
export function countertopRunsFixture() {
  const parts = [1, 2, 3].map(number => ({ item_id: id(600 + number), number,
    kind: number === 2 ? 'filler' : 'cabinet', code: null }));
  const signal = 'Synthetic confirmed part below this synthetic countertop.';
  return { can_suggest: true, why_not: null, drawings: [{
    view_id: id(610), page_index: 0, tag: 'synthetic-shop', can_confirm: true, why_not: null, parts,
    countertops: [4, 5].map(number => ({ countertop_item_id: id(600 + number), number, code: null,
      suggestion: {
        members: parts.slice(0, 2).map((part, index) => ({ ...part, position: index + 1, signal })),
        left_out: [{ ...parts[2], reason: 'Synthetic example: this part sits above the countertop.' }],
        warnings: ['Synthetic example: a gap remains at the right end.'], edge_tolerance: '0.004',
      }, decision: null,
    })),
  }] };
}
