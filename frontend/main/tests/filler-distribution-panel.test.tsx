import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import { FillerDistributionPanel } from '../src/components/measure/FillerDistributionPanel.js';
import {
  buildFillerDistributionRequest,
  distributionFieldWidthKey,
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

const parameters: DistributionParameter[] = [
  { name: 'filler_min', declared_default: '1"', blocked: false },
  { name: 'filler_max', declared_default: '4"', blocked: false },
];

assert.equal(distributionFieldWidthKey(quantities), 'USER_INPUT:field_dimension');

const unselected = buildFillerDistributionRequest({
  cabinetWidths: ['30"', '30"'],
  fillerWidths: ['2"', '2"'],
  fieldWidth: '70"',
  fillerMin: '1"',
  fillerMax: '4"',
  adjustableCabinetId: null,
});
assert.deepEqual(unselected.missing, []);
assert.equal(unselected.request?.adjustable_cabinet_id, null);

const selected = buildFillerDistributionRequest({
  cabinetWidths: ['30"', '30"'],
  fillerWidths: ['2"', '2"'],
  fieldWidth: '70"',
  fillerMin: '1"',
  fillerMax: '4"',
  adjustableCabinetId: 'cabinet-2',
});
assert.equal(selected.request?.adjustable_cabinet_id, 'cabinet-2');

const missing = buildFillerDistributionRequest({
  cabinetWidths: ['30"'],
  fillerWidths: ['2"'],
  fieldWidth: '70"',
  fillerMin: '1"',
  fillerMax: '',
  adjustableCabinetId: 'cabinet-1',
});
assert.deepEqual(missing.missing, ['two architectural filler widths', 'filler maximum']);

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
assert.match(html, /Adjustable cabinet/);
assert.match(html, /<option value="" selected="">No cabinet selected<\/option>/);
assert.match(html, /Cabinet 2/);
assert.doesNotMatch(html, /32&quot;/);

const proposed = renderToStaticMarkup(
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
    initialResult={{
      outcome: 'PASS',
      condition: 'cabinet_adjusted_by_reviewer_selection',
      message: 'The reviewer-selected cabinet absorbs the remaining site difference.',
      design_width: { numerator: '64', denominator: '1', unit: 'in', display: '64"' },
      site_difference: { numerator: '6', denominator: '1', unit: 'in', display: '6"' },
      field_dimension: {
        name: 'field_width',
        source: 'USER_INPUT',
        status: 'HUMAN_CONFIRMED',
        value: { numerator: '70', denominator: '1', unit: 'in', display: '70"' },
      },
      selected_adjustable_cabinet_id: 'cabinet-2',
      fillers: [
        {
          id: 'left filler',
          original: { numerator: '2', denominator: '1', unit: 'in', display: '2"' },
          proposed: { numerator: '4', denominator: '1', unit: 'in', display: '4"' },
        },
        {
          id: 'right filler',
          original: { numerator: '2', denominator: '1', unit: 'in', display: '2"' },
          proposed: { numerator: '4', denominator: '1', unit: 'in', display: '4"' },
        },
      ],
      cabinets: [
        {
          id: 'cabinet-1',
          original: { numerator: '30', denominator: '1', unit: 'in', display: '30"' },
          proposed: { numerator: '30', denominator: '1', unit: 'in', display: '30"' },
          adjustable: false,
        },
        {
          id: 'cabinet-2',
          original: { numerator: '30', denominator: '1', unit: 'in', display: '30"' },
          proposed: { numerator: '32', denominator: '1', unit: 'in', display: '32"' },
          adjustable: true,
        },
      ],
      operands: [],
      calculation: 'backend trace',
    }}
  />,
);

assert.match(proposed, /Proposal returned/);
assert.match(proposed, /cabinet-2 \(adjusted\)/);
assert.match(proposed, /32&quot;/);
assert.match(proposed, /backend trace/);

console.log('filler-distribution panel component test passed');
