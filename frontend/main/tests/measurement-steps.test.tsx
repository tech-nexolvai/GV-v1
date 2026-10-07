import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { MeasurementSectionNav } from '../src/pages/MeasurementSectionNav.js';
import { proposalCoversEveryPosition, proposalValuesAtPositions } from '../src/lib/measurement-proposals.js';

const markup = renderToStaticMarkup(<MeasurementSectionNav />);
for (const destination of ['measure-drawings', 'measure-runs', 'measure-values', 'measure-settings']) {
  assert.ok(markup.includes(`aria-controls="${destination}"`), `${destination} must remain reachable`);
}
assert.ok(!markup.includes('href="#measure-'), 'step navigation must not replace the hash-based package route');
assert.ok(markup.includes('Confirm runs and widths'));
assert.ok(markup.includes('Review measurements'));
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
