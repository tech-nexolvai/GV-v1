import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { MeasurementSectionNav } from '../src/pages/MeasurementSectionNav.js';
import { proposalCoversEveryPosition, proposalValuesAtPositions } from '../src/lib/measurement-proposals.js';

const markup = renderToStaticMarkup(<MeasurementSectionNav />);
for (const destination of ['measure-drawings', 'measure-runs', 'measure-values', 'measure-settings']) {
  assert.ok(markup.includes(`aria-controls="${destination}"`), `${destination} must remain reachable`);
}
assert.ok(!markup.includes('href="#measure-'), 'step navigation must not replace the hash-based package route');
// The wizard's step names (#1061); each step is a button that shows it, never a link.
for (const label of ['Drawings &amp; parts', 'Countertops', 'Values', 'Settings &amp; run checks']) {
  assert.ok(markup.includes(label), `${label} is a step`);
}
const counted = renderToStaticMarkup(
  <MeasurementSectionNav current="runs" counts={{ drawings: { done: 3, total: 3 }, runs: { done: 1, total: 4 }, values: { done: 0, total: 0 }, settings: null }} />,
);
assert.ok(counted.includes('aria-current="step" data-step="runs"'), 'the shown step is marked');
// #1124: counts in words, never a bare "1/4".
assert.ok(counted.includes('All 3 done'), 'a finished step says so');
assert.ok(counted.includes('1 of 4 done'), 'a step in progress says how many of how many');
assert.ok(!/\d\/\d/.test(counted), 'no bare done/total');
assert.ok(counted.includes('Nothing to do'), 'an empty step is not shown as done');
const sparseProposal = [
  { value: '1 in', position: 0 },
  { value: '3 in', position: 2 },
];
assert.deepEqual(proposalValuesAtPositions(sparseProposal, 3), ['1 in', '', '3 in']);
assert.equal(proposalCoversEveryPosition(sparseProposal, 3), false);
assert.equal(
  proposalCoversEveryPosition(
    [
      { value: '1 in', position: 0 },
      { value: '2 in', position: 1 },
      { value: '3 in', position: 2 },
    ],
    3,
  ),
  true,
);
console.log('measurement steps retain all four destinations');
