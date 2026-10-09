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
// #1125: the page's title is its one <h1> (the top bar has none on this page), in the shadcn style.
assert.match(html, new RegExp(`<h1 id="${label}"[^>]*>Rulebook</h1>`));
assert.equal((html.match(/<h1[ >]/g) ?? []).length, 1, 'exactly one h1');
assert.doesNotMatch(html, /page-frame__/, 'no legacy PageFrame classes are left');
assert.ok(html.includes(snapshot), 'the complete API snapshot must remain available');
assert.match(html, /<dd>0<\/dd>/, 'zero is a real value, not an empty-state trigger');
assert.match(html, /<button type="button">Refresh<\/button>/);

const error = renderToStaticMarkup(
  <PageLoadError title="Usage unavailable" message="Could not contact the server" onRetry={() => undefined} />,
);
assert.match(error, /role="alert"/);
assert.match(error, /Could not contact the server/);
// #1125: the retry is the shadcn Button, its icon before the words.
assert.match(error, /<button data-slot="button"[^>]*type="button"[^>]*>.*Try again<\/button>/);
assert.doesNotMatch(error, /No findings|0 findings/);

// A stale category after a refreshed list is a filter miss, not evidence of an empty rulebook.
assert.equal(rulebookEmptyState(0).title, 'No rules are published');
assert.equal(rulebookEmptyState(2).title, 'No rules match this category');
assert.match(rulebookEmptyState(2).message, /Select All/);
assert.doesNotMatch(rulebookEmptyState(2).message, /no rule snapshots/i);

console.log('page-frame: accessible content, errors, and rulebook filter honesty ok');
