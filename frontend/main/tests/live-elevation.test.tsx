/**
 * The drawing, rendered from a **live API response** rather than a fixture.
 *
 * `elevation.test.tsx` builds its own response object, which is the right way to test the drawing's
 * logic and cannot catch the one thing this does: that the shape the running server actually sends
 * is the shape the component reads. The PR that added the diagram said so itself — "tested with
 * static renders and headless screenshots, not in the running app against a live API".
 *
 * `liveDistribution.ts` holds that response, captured from `POST /filler-distribution` on a server
 * running against a real database, for Raj's slide-6 example: 90" wall, 82" site, 3" fillers and a
 * 36" equipment cabinet between two 24" regulars.
 */
import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import { ElevationDiagram } from '../src/components/output/ElevationDiagram.js';
import { LIVE_DISTRIBUTION as live } from './liveDistribution.js';

// The server's own answer, before anything draws it. If these drift, the drawing below is being
// checked against the wrong arithmetic and the assertions past this point mean nothing.
assert.equal(live.outcome, 'PASS');
assert.equal(live.condition, 'cabinets_absorb_remainder');
assert.deepEqual(
  live.fillers.map((filler) => filler.proposed.display),
  ['2"', '2"'],
);
assert.deepEqual(
  live.cabinets.map((cabinet) => cabinet.proposed.display),
  ['21"', '36"', '21"'],
);

// Rendered markup escapes the inch mark, so `21"` is `21&quot;` in the string. Compared on the
// decoded text rather than by searching the raw HTML: searching it silently found nothing and read
// as "the drawing is missing 21 inches", which is a false alarm that would have been reported.
const html = renderToStaticMarkup(<ElevationDiagram result={live} />);
const shown = html.replace(/&quot;/g, '"');

// Slide 6: the corrected run reads 2 / 21 / 36 / 21 / 2, and the drawing has to show the new widths.
for (const width of ['2"', '21"', '36"']) {
  assert.ok(shown.includes(width), `the drawing does not show ${width}`);
}

// The old widths survive beside the new ones — a reviewer has to see what changed, not just what it
// became.
for (const width of ['3"', '24"', '90"', '82"']) {
  assert.ok(shown.includes(width), `the drawing does not show ${width}`);
}

// Both runs are drawn, and the marks that say which parts moved.
for (const label of ['Arch drawing', 'Corrected for site', '✕', '✓', 'Equipment, width fixed']) {
  assert.ok(shown.includes(label), `the drawing is missing ${label}`);
}

// It rendered a drawing, not an empty frame.
assert.ok(html.includes('<svg'), 'no svg in the output');
assert.ok(html.length > 1000, `the drawing is suspiciously small: ${html.length} bytes`);

console.log('live elevation: rendered slide 6 from a real API response');
