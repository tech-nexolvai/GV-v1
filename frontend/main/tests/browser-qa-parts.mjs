// Synthetic code-crop fixture only. No values or parts copied from a customer drawing.
export const fixtureParts = { drawings: [{
  view_id: '00000000-0000-4000-8000-000000000401', page_index: 0, tag: 'synthetic-shop',
  role: 'shop', can_confirm: true, why_not: null,
  parts: [{
    proposal_id: '00000000-0000-4000-8000-000000000402',
    view_id: '00000000-0000-4000-8000-000000000401', page_index: 0, position: 1,
    suggested_kind: 'cabinet', suggested_code: 'SYNTHETIC-CODE', reason: 'Isolated frontend recovery fixture; not an extracted part.',
    added_by_a_person: false, left_end: { x: '0.25', y: '0.5' }, right_end: { x: '0.5', y: '0.5' },
    has_crop: true, decision: null,
  }],
}] };

export function decisionPartsFixture() {
  const result = structuredClone(fixtureParts);
  result.drawings[0].parts.push({ ...structuredClone(result.drawings[0].parts[0]),
    proposal_id: '00000000-0000-4000-8000-000000000403', position: 2,
    suggested_code: 'SYNTHETIC-SECOND', has_crop: false,
    left_end: { x: '0.5', y: '0.5' }, right_end: { x: '0.75', y: '0.5' },
  });
  return result;
}
