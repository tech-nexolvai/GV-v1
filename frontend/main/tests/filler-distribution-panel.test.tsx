import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import { FillerDistributionPanel } from '../src/components/measure/FillerDistributionPanel.js';
import {
  buildFillerDistributionRequest,
  CABINET_BOUND_NAMES,
  distributionFieldWidthKey,
  type CabinetBoundName,
  type DistributionParameter,
  type DistributionQuantity,
} from '../src/components/measure/fillerDistribution.js';

const quantities: DistributionQuantity[] = [
  { key: 'ARCH:cabinet_width', source: 'ARCH', semantic_type: 'cabinet_width', many: true },
  { key: 'ARCH:filler_width', source: 'ARCH', semantic_type: 'filler_width', many: true },
  {
    key: 'USER_INPUT:field_dimension',
    source: 'USER_INPUT',
    semantic_type: 'field_dimension',
    many: false,
  },
];

// The bounds arrive the way filler_min/filler_max already do — from the rulebook, never from a
// default in this panel. CLIENT_FACTS Q21: the real values are still unsettled.
const parameters: DistributionParameter[] = [
  { name: 'filler_min', declared_default: '1"', blocked: false },
  { name: 'filler_max', declared_default: '4"', blocked: false },
  ...CABINET_BOUND_NAMES.map((name) => ({
    name,
    declared_default: name.endsWith('_min') ? '9"' : '48"',
    blocked: false,
  })),
];

const bounds = Object.fromEntries(
  CABINET_BOUND_NAMES.map((name) => [name, name.endsWith('_min') ? '9"' : '48"']),
) as Record<CabinetBoundName, string>;

assert.equal(distributionFieldWidthKey(quantities), 'USER_INPUT:field_dimension');

// A complete draft builds a request, and every cabinet carries the reviewer's classification.
const classified = buildFillerDistributionRequest({
  cabinetWidths: ['30"', '30"'],
  cabinetTypes: ['double_door', 'equipment'],
  fillerWidths: ['2"', '2"'],
  fieldWidth: '70"',
  fillerMin: '1"',
  fillerMax: '4"',
  cabinetBounds: bounds,
});
assert.deepEqual(classified.missing, []);
assert.deepEqual(
  classified.request?.assembly.cabinets.map((cabinet) => cabinet.type),
  ['double_door', 'equipment'],
);

// There is no longer any field through which a caller could nominate one cabinet to absorb
// everything. #678 removed it rather than deprecating it: a path that still produced the wrong
// PASS would be the defect, not a smaller version of it.
assert.equal('adjustable_cabinet_id' in (classified.request ?? {}), false);

// An unclassified cabinet withholds the request. Nothing infers a type from a width.
const unclassified = buildFillerDistributionRequest({
  cabinetWidths: ['30"', '30"'],
  cabinetTypes: ['double_door'],
  fillerWidths: ['2"', '2"'],
  fieldWidth: '70"',
  fillerMin: '1"',
  fillerMax: '4"',
  cabinetBounds: bounds,
});
assert.equal(unclassified.request, null);
assert.deepEqual(unclassified.missing, ['a type for every cabinet']);

// A bound the rulebook has not supplied is a missing input, never a substituted number.
const withoutBound = buildFillerDistributionRequest({
  cabinetWidths: ['30"'],
  cabinetTypes: ['drawer'],
  fillerWidths: ['2"', '2"'],
  fieldWidth: '70"',
  fillerMin: '1"',
  fillerMax: '4"',
  cabinetBounds: { ...bounds, drawer_cab_width_min: '' },
});
assert.equal(withoutBound.request, null);
assert.deepEqual(withoutBound.missing, ['drawer cab width min']);

// One filler is a real layout — slide 12 names a wall on only one side.
const oneFiller = buildFillerDistributionRequest({
  cabinetWidths: ['30"'],
  cabinetTypes: ['drawer'],
  fillerWidths: ['2"'],
  fieldWidth: '70"',
  fillerMin: '1"',
  fillerMax: '4"',
  cabinetBounds: bounds,
});
assert.deepEqual(oneFiller.missing, []);
assert.deepEqual(
  oneFiller.request?.assembly.fillers.map((filler) => filler.id),
  ['filler'],
);

const missing = buildFillerDistributionRequest({
  cabinetWidths: ['30"'],
  cabinetTypes: ['drawer'],
  fillerWidths: ['2"'],
  fieldWidth: '70"',
  fillerMin: '1"',
  fillerMax: '',
  cabinetBounds: bounds,
});
assert.deepEqual(missing.missing, ['filler maximum']);

const html = renderToStaticMarkup(
  <FillerDistributionPanel
    quantities={quantities}
    parameters={parameters}
    singles={{ 'USER_INPUT:field_dimension': '70"' }}
    runs={{ 'ARCH:cabinet_width': ['30"', '30"'], 'ARCH:filler_width': ['2"', '2"'] }}
    fieldWidth={'70"'}
    onFieldWidthChange={() => undefined}
    onCalculate={async () => {
      throw new Error('not called during static render');
    }}
  />,
);

assert.match(html, /Site field width/);
// One classification control per cabinet, not one "which cabinet moves" dropdown.
assert.match(html, /reviewer classification/);
assert.match(html, /distribution-cabinet-type-0/);
assert.match(html, /distribution-cabinet-type-1/);
assert.match(html, /Equipment \(width cannot change\)/);
assert.doesNotMatch(html, /Adjustable cabinet/);
assert.doesNotMatch(html, /No cabinet selected/);
assert.doesNotMatch(html, /32&quot;/);

const proposed = renderToStaticMarkup(
  <FillerDistributionPanel
    quantities={quantities}
    parameters={parameters}
    singles={{ 'USER_INPUT:field_dimension': '82"' }}
    runs={{
      'ARCH:cabinet_width': ['24"', '36"', '24"'],
      'ARCH:filler_width': ['3"', '3"'],
    }}
    fieldWidth={'82"'}
    onFieldWidthChange={() => undefined}
    onCalculate={async () => {
      throw new Error('not called during static render');
    }}
    initialResult={{
      // Raj's first worked example, slide 4: 90" to 82".
      outcome: 'PASS',
      condition: 'cabinets_absorb_remainder',
      // The full explanation Raj asked for on slides 5 and 9, from the exact numbers (#682).
      message:
        'Wall to wall width in the architectural drawing = 90". Wall to wall width as per site ' +
        'dimensions = 82". So 8" needs to be reduced in the shop drawing cabinet elevation.',
      summary: 'The fillers reached their limit, so the rest is divided equally.',
      design_width: { numerator: '90', denominator: '1', unit: 'in', display: '90"' },
      site_difference: { numerator: '-8', denominator: '1', unit: 'in', display: '-8"' },
      field_dimension: {
        name: 'field_width',
        source: 'USER_INPUT',
        status: 'HUMAN_CONFIRMED',
        value: { numerator: '82', denominator: '1', unit: 'in', display: '82"' },
      },
      fillers: [
        {
          id: 'left filler',
          original: { numerator: '3', denominator: '1', unit: 'in', display: '3"' },
          proposed: { numerator: '2', denominator: '1', unit: 'in', display: '2"' },
        },
        {
          id: 'right filler',
          original: { numerator: '3', denominator: '1', unit: 'in', display: '3"' },
          proposed: { numerator: '2', denominator: '1', unit: 'in', display: '2"' },
        },
      ],
      cabinets: [
        {
          id: 'cabinet-1',
          type: 'double_door',
          original: { numerator: '24', denominator: '1', unit: 'in', display: '24"' },
          proposed: { numerator: '21', denominator: '1', unit: 'in', display: '21"' },
          adjustable: true,
        },
        {
          id: 'cabinet-2',
          type: 'equipment',
          original: { numerator: '36', denominator: '1', unit: 'in', display: '36"' },
          proposed: { numerator: '36', denominator: '1', unit: 'in', display: '36"' },
          adjustable: false,
        },
        {
          id: 'cabinet-3',
          type: 'double_door',
          original: { numerator: '24', denominator: '1', unit: 'in', display: '24"' },
          proposed: { numerator: '21', denominator: '1', unit: 'in', display: '21"' },
          adjustable: true,
        },
      ],
      cabinets_retained: false,
      reviewer_action: null,
      operands: [],
      calculation: 'backend trace',
    }}
  />,
);

assert.match(proposed, /Proposal returned/);
// The reviewer is told how the drawing is being corrected, not just that it was.
assert.match(proposed, /Wall to wall width in the architectural drawing/);
// Both regular cabinets moved, and the equipment cabinet is marked as the one that cannot.
assert.match(proposed, /cabinet-2 \(equipment — width fixed\)/);
assert.match(proposed, /21&quot;/);
assert.doesNotMatch(proposed, /Selected cabinet/);
assert.match(proposed, /backend trace/);

const abstained = renderToStaticMarkup(
  <FillerDistributionPanel
    quantities={quantities}
    parameters={parameters}
    singles={{ 'USER_INPUT:field_dimension': '82"' }}
    runs={{ 'ARCH:cabinet_width': ['24"'], 'ARCH:filler_width': ['3"', '3"'] }}
    fieldWidth={'82"'}
    onFieldWidthChange={() => undefined}
    onCalculate={async () => {
      throw new Error('not called during static render');
    }}
    initialResult={{
      outcome: 'REVIEW_REQUIRED',
      condition: 'cannot_be_resolved',
      message:
        'It cannot be absorbed within the stated limits. The program will not force a fix — ' +
        'this needs an RFI to the architect.',
      summary: 'The site difference cannot be absorbed within the stated limits.',
      design_width: { numerator: '90', denominator: '1', unit: 'in', display: '90"' },
      site_difference: { numerator: '-8', denominator: '1', unit: 'in', display: '-8"' },
      field_dimension: {
        name: 'field_width',
        source: 'USER_INPUT',
        status: 'HUMAN_CONFIRMED',
        value: { numerator: '82', denominator: '1', unit: 'in', display: '82"' },
      },
      fillers: [],
      cabinets: [],
      cabinets_retained: false,
      reviewer_action: 'cannot be resolved by distribution; raise an RFI to the architect',
      operands: [],
      calculation: 'backend trace',
    }}
  />,
);

// An abstention has to arrive as an instruction, not as a condition string.
assert.match(abstained, /Could not propose/);
assert.match(abstained, /raise an RFI to the architect/);

console.log('filler-distribution panel component test passed');
