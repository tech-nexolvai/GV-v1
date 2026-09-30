import assert from 'node:assert/strict';

import {
  categoryLabel,
  classificationEntries,
  isCategorical,
  type ClassifiableQuantity,
} from '../src/pages/classificationFields.js';

const cabinetType: ClassifiableQuantity = {
  key: 'USER_INPUT:cabinet_category',
  many: true,
  consumers: [{ rule_id: 'CAB-FILLER-001', input_name: 'cabinet_type' }],
  categories: ['single_door', 'double_door', 'drawer', 'equipment'],
};

const cabinetWidths: ClassifiableQuantity = {
  key: 'ARCH:cabinet_width',
  many: true,
  consumers: [{ rule_id: 'CAB-FILLER-001', input_name: 'architectural_cabinets' }],
};

// Only the server saying so makes an input a choice. A dimension must never grow a dropdown.
assert.equal(isCategorical(cabinetType), true);
assert.equal(isCategorical(cabinetWidths), false);
assert.equal(isCategorical({ ...cabinetType, categories: [] }), false);

assert.equal(categoryLabel('double_door'), 'Double door');
assert.equal(categoryLabel('equipment'), 'Equipment');

// A complete run is sent, in the order chosen — which cabinet is the equipment cabinet is the
// whole question, so the order is the answer.
assert.deepEqual(
  classificationEntries([cabinetType, cabinetWidths], {
    'USER_INPUT:cabinet_category': ['double_door', 'equipment', 'double_door'],
    'ARCH:cabinet_width': ['24"', '36"', '24"'],
  }),
  [
    {
      rule_id: 'CAB-FILLER-001',
      name: 'cabinet_type',
      categories: ['double_door', 'equipment', 'double_door'],
    },
  ],
);

// A partial run is not sent. Storing two classifications for three cabinets makes the check abstain
// on the length — true, but the slow way, when the form already knows the reviewer is not finished.
assert.deepEqual(
  classificationEntries([cabinetType], {
    'USER_INPUT:cabinet_category': ['double_door', '', 'double_door'],
  }),
  [],
);

// An untouched run is left out entirely rather than sent as blanks.
assert.deepEqual(classificationEntries([cabinetType], {}), []);
assert.deepEqual(classificationEntries([cabinetType], { 'USER_INPUT:cabinet_category': [] }), []);

// One chosen run fans out to every input it feeds, as a measurement does. The mapping is the
// server's, from `consumers`.
assert.deepEqual(
  classificationEntries(
    [
      {
        ...cabinetType,
        consumers: [
          { rule_id: 'CAB-FILLER-001', input_name: 'cabinet_type' },
          { rule_id: 'SOME-OTHER-RULE', input_name: 'cabinet_type' },
        ],
      },
    ],
    { 'USER_INPUT:cabinet_category': ['equipment'] },
  ),
  [
    { rule_id: 'CAB-FILLER-001', name: 'cabinet_type', categories: ['equipment'] },
    { rule_id: 'SOME-OTHER-RULE', name: 'cabinet_type', categories: ['equipment'] },
  ],
);

console.log('classification fields test passed');
