// Entirely synthetic, fixed calculator responses. No actual rules, client values or backend calls.
import { needed } from './browser-qa-fixtures.mjs';

export const distributionNeeded = {
  ...needed,
  quantities: [
    { key: 'ARCH:cabinet_width', source: 'ARCH', semantic_type: 'cabinet_width', many: true, consumers: [], categories: [] },
    { key: 'ARCH:filler_width', source: 'ARCH', semantic_type: 'filler_width', many: true, consumers: [], categories: [] },
    { key: 'USER_INPUT:field_dimension', source: 'USER_INPUT', semantic_type: 'field_dimension', many: false, consumers: [], categories: [] },
  ],
  confirmed_readings: [
    { key: 'ARCH:cabinet_width', source: 'ARCH', semantic_type: 'cabinet_width', value: '30 in', qualification: 'reviewer_confirmed' },
    { key: 'ARCH:filler_width', source: 'ARCH', semantic_type: 'filler_width', value: '2 in', qualification: 'reviewer_confirmed' },
  ],
  parameters: [
    { name: 'filler_min', declared_default: '1 in' },
    { name: 'filler_max', declared_default: '4 in' },
    ...['single_door', 'double_door', 'drawer'].flatMap(kind => [
      { name: `${kind}_cab_width_min`, declared_default: '9 in' },
      { name: `${kind}_cab_width_max`, declared_default: '48 in' },
    ]),
  ].map(item => ({ ...item, blocked: false, rule_ids: [], scope: 'project', sources: [] })),
};

const quantity = value => ({ numerator: String(value), denominator: '1', unit: 'in', display: `${value} in` });
export function distributionFixture(request) {
  const expected = {
    assembly: { cabinets: [{ id: 'cabinet-1', width: '30 in', type: 'double_door' }],
      fillers: [{ id: 'filler', width: '2 in' }] },
    field_width: request.field_width,
    ...Object.fromEntries(distributionNeeded.parameters.map(item => [item.name, item.declared_default])),
  };
  if (!['32 in', '33 in'].includes(request.field_width) || JSON.stringify(request) !== JSON.stringify(expected)) return null;
  const changed = request.field_width === '33 in';
  return {
    outcome: 'PASS', condition: changed ? 'fillers_absorb' : 'no_change_required',
    message: changed ? 'Synthetic response: the filler becomes 3 in.' : 'Synthetic response: no width changes.',
    summary: 'Synthetic frontend calculator fixture; not a real review.',
    design_width: quantity(32), site_difference: quantity(changed ? 1 : 0),
    field_dimension: { name: 'field_width', source: 'USER_INPUT', status: 'HUMAN_CONFIRMED', value: quantity(changed ? 33 : 32) },
    fillers: [{ id: 'filler', original: quantity(2), proposed: quantity(changed ? 3 : 2) }],
    cabinets: [{ id: 'cabinet-1', type: 'double_door', original: quantity(30), proposed: quantity(30), adjustable: true }],
    cabinets_retained: true, reviewer_action: null, operands: [], calculation: 'Synthetic trace only.',
  };
}
