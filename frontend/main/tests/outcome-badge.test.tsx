import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { OutcomeBadge } from '../src/components/ui/outcome-badge.js';

// Proves the node harness resolves the `@/` alias (outcome-badge imports `@/lib/utils`, `@/data/…`
// and `@/components/ui/OutcomeIcon`), so tested components can move onto shadcn/ui primitives.
const fail = renderToStaticMarkup(<OutcomeBadge outcome="FAIL" />);
assert.match(fail, /data-slot="outcome-badge"/);
assert.match(fail, /data-outcome="FAIL"/);
assert.match(fail, /Needs correction/);
assert.match(fail, /data-outcome-icon="FAIL"/, 'colour never stands alone: the glyph is always there');

const missing = renderToStaticMarkup(<OutcomeBadge outcome="NOT_FOUND" />);
assert.match(missing, /Waiting on a value/);
assert.match(missing, /border-dashed/, 'waiting on a value keeps its dashed edge');

console.log('outcome badge: agreed word, glyph and edge for each outcome; @/ alias resolves in node');
