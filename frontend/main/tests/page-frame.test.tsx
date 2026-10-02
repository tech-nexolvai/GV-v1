import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { PageFrame, PageLoadError } from '../src/components/ui/PageFrame.js';
import { rulebookEmptyState } from '../src/pages/rulebookState.js';

// The frame must preserve page-owned content and actions, including long identifiers and zeroes.
const snapshot = 'a'.repeat(64);
const html = renderToStaticMarkup(
  <PageFrame title="Rulebook" description="Published checks" actions={<button type="button">Refresh</button>}>
    <dl><dt>Snapshot</dt><dd>{snapshot}</dd><dt>Published versions</dt><dd>0</dd></dl>
  </PageFrame>,
);
const label = html.match(/aria-labelledby="([^"]+)"/)?.[1];
assert.ok(label, 'the page region must have an accessible heading');
assert.ok(html.includes(`<h1 id="${label}">Rulebook</h1>`));
assert.ok(html.includes(snapshot), 'the complete API snapshot must remain available');
assert.match(html, /<dd>0<\/dd>/, 'zero is a real value, not an empty-state trigger');
assert.match(html, /<button type="button">Refresh<\/button>/);

const error = renderToStaticMarkup(
  <PageLoadError title="Usage unavailable" message="Could not contact the server" onRetry={() => undefined} />,
);
assert.match(error, /role="alert"/);
assert.match(error, /Could not contact the server/);
assert.match(error, /<button type="button"[^>]*>Try again<\/button>/);
assert.doesNotMatch(error, /No findings|0 findings/);

// A stale category after a refreshed list is a filter miss, not evidence of an empty rulebook.
assert.equal(rulebookEmptyState(0).title, 'No rules are published');
assert.equal(rulebookEmptyState(2).title, 'No rules match this category');
assert.match(rulebookEmptyState(2).message, /Select All/);
assert.doesNotMatch(rulebookEmptyState(2).message, /no rule snapshots/i);

console.log('page-frame: accessible content, errors, and rulebook filter honesty ok');
