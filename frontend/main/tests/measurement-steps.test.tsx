import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { MeasurementSectionNav } from '../src/pages/MeasurementSectionNav.js';

const markup = renderToStaticMarkup(<MeasurementSectionNav />);
for (const destination of ['measure-drawings', 'measure-runs', 'measure-values', 'measure-settings']) {
  assert.ok(markup.includes(`aria-controls="${destination}"`), `${destination} must remain reachable`);
}
assert.ok(!markup.includes('href="#measure-'), 'step navigation must not replace the hash-based package route');
assert.ok(markup.includes('Confirm runs and widths'));
assert.ok(markup.includes('Review measurements'));
console.log('measurement steps retain all four destinations');
