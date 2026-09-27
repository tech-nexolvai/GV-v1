import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';

import { ElevationDiagram } from '../src/components/output/ElevationDiagram.js';
import { buildElevation, placeSegments, sameQuantity } from '../src/components/output/elevation.js';
import type { FillerDistributionResponse } from '../src/components/measure/fillerDistribution.js';

type Quantity = FillerDistributionResponse['design_width'];

function inches(numerator: number, denominator = 1): Quantity {
  const whole = numerator / denominator;
  return {
    numerator: String(numerator),
    denominator: String(denominator),
    display: `${Number.isInteger(whole) ? whole : whole.toFixed(2)}"`,
    unit: 'in',
  };
}

function cabinet(
  id: string,
  type: 'single_door' | 'double_door' | 'drawer' | 'equipment',
  original: number,
  proposed: number,
) {
  return { id, type, adjustable: type !== 'equipment', original: inches(original), proposed: inches(proposed) };
}

function response(overrides: Partial<FillerDistributionResponse>): FillerDistributionResponse {
  return {
    cabinets: [],
    cabinets_retained: false,
    calculation: '',
    condition: '',
    design_width: inches(0),
    field_dimension: { name: 'field_width', source: 'USER_INPUT', status: 'HUMAN_CONFIRMED', value: null },
    fillers: [],
    message: '',
    operands: [],
    outcome: 'PASS',
    reviewer_action: null,
    site_difference: null,
    summary: '',
    ...overrides,
  } as FillerDistributionResponse;
}

// Raj's deck, scenario 1 (slides 4-6): 90" arch, 82" site -> 2 / 21 / 36 / 21 / 2.
const scenario1 = response({
  design_width: inches(90),
  field_dimension: { name: 'field_width', source: 'USER_INPUT', status: 'HUMAN_CONFIRMED', value: inches(82) },
  site_difference: { ...inches(-8), display: '-8"' },
  fillers: [
    { id: 'left filler', original: inches(3), proposed: inches(2) },
    { id: 'right filler', original: inches(3), proposed: inches(2) },
  ],
  cabinets: [
    cabinet('cabinet-1', 'double_door', 24, 21),
    cabinet('cabinet-2', 'equipment', 36, 36),
    cabinet('cabinet-3', 'double_door', 24, 21),
  ],
});

// Raj's deck, scenario 2 (slides 8-10): 88" arch, 96" site -> 3 / 27 / 36 / 27 / 3.
const scenario2 = response({
  design_width: inches(88),
  field_dimension: { name: 'field_width', source: 'USER_INPUT', status: 'HUMAN_CONFIRMED', value: inches(96) },
  site_difference: { ...inches(8), display: '8"' },
  fillers: [
    { id: 'left filler', original: inches(2), proposed: inches(3) },
    { id: 'right filler', original: inches(2), proposed: inches(3) },
  ],
  cabinets: [
    cabinet('cabinet-1', 'single_door', 24, 27),
    cabinet('cabinet-2', 'equipment', 36, 36),
    cabinet('cabinet-3', 'single_door', 24, 27),
  ],
});

// Scenario 4: nothing fits, RFI to architect. The engine returns no correction.
const scenario4 = response({
  outcome: 'REVIEW_REQUIRED',
  message: 'Fillers, regular cabinets and the equipment cabinet are all at their limits.',
  design_width: inches(90),
  fillers: scenario1.fillers.map((filler) => ({ ...filler, proposed: filler.original })),
  cabinets: scenario1.cabinets.map((item) => ({ ...item, proposed: item.original })),
});

// --- layout ---------------------------------------------------------------------------------

{
  const layout = buildElevation(scenario1);
  assert.deepEqual(
    layout.elements.map((element) => element.id),
    ['left filler', 'cabinet-1', 'cabinet-2', 'cabinet-3', 'right filler'],
    'the run is drawn in wall order: left filler, cabinets, right filler',
  );
  assert.deepEqual(
    layout.elements.map((element) => element.changed),
    [true, true, false, true, true],
    'fillers and regular cabinets changed; the equipment cabinet did not',
  );
  assert.equal(layout.elements[2].kind, 'equipment');
  assert.equal(layout.hasProposal, true);
  assert.equal(layout.archTotal.display, '90"');
  assert.equal(layout.siteTotal?.display, '82"');
  assert.equal(layout.siteTotal?.value, 82, 'the corrected parts sum to the site width');
}

{
  assert.ok(sameQuantity(inches(42, 2), inches(21)), '42/2 and 21/1 are the same width');
  assert.ok(!sameQuantity(inches(21), inches(22)));
}

{
  const layout = buildElevation(scenario1);
  const segments = placeSegments(layout.elements, 'original', 0, 1);
  const end = segments[segments.length - 1].x + segments[segments.length - 1].width;
  assert.equal(end, 90, 'at one unit per pixel the arch strip is exactly 90 long');
  assert.equal(segments[0].width, 3, 'no minimum width distorts a narrow filler');
}

{
  const layout = buildElevation(scenario4);
  assert.equal(layout.hasProposal, false, 'an unresolved run is not drawn as corrected');
  assert.equal(layout.siteTotal, null);
}

{
  const oneFiller = response({
    design_width: inches(40),
    fillers: [{ id: 'filler', original: inches(4), proposed: inches(4) }],
    cabinets: [cabinet('cabinet-1', 'drawer', 36, 36)],
  });
  assert.equal(buildElevation(oneFiller).sideKnown, false, 'a lone filler is not claimed to be on the left');
}

// --- rendering ------------------------------------------------------------------------------

{
  const html = renderToStaticMarkup(<ElevationDiagram result={scenario1} />);
  const crosses = html.match(/✕<\/text>/g)?.length ?? 0;
  const ticks = html.match(/✓<\/text>/g)?.length ?? 0;
  assert.equal(crosses, 4, 'four parts carry the corrected mark');
  assert.equal(ticks, 1, 'the equipment cabinet carries the kept mark');
  assert.ok(html.includes('Corrected for site'));
  assert.ok(html.includes('-8&quot;'), 'the site difference is shown (quote is HTML-escaped)');
  assert.ok(!html.includes('RFI'), 'a resolved run shows no RFI banner');
  assert.ok(!html.includes('<title></title>'), 'every part has a tooltip');
  assert.ok(
    html.includes('Double-door cabinet 1: 24&quot; on the arch drawing, 21&quot; corrected'),
    'a corrected part says what it was and what it became',
  );
}

{
  const html = renderToStaticMarkup(<ElevationDiagram result={scenario4} />);
  assert.ok(html.includes('RFI to architect'), 'an unresolved run says what to do next');
  assert.ok(!html.includes('Corrected for site'), 'and does not draw a correction');
  assert.equal(html.match(/✕<\/text>/g)?.length ?? 0, 0);
}

// Written out so the drawing can be looked at, not only asserted on.
const outDir = new URL('../../elevation-preview/', import.meta.url);
mkdirSync(outDir, { recursive: true });
for (const [name, result] of [
  ['scenario-1', scenario1],
  ['scenario-2', scenario2],
  ['scenario-4', scenario4],
] as const) {
  writeFileSync(new URL(`${name}.html`, outDir), renderToStaticMarkup(<ElevationDiagram result={result} />));
}

console.log('elevation: all assertions passed');
