import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { PartPicture, PartsLoadState, type PartImageState } from '../src/components/measure/PartRecovery.js';

const noop = () => { throw new Error('render must not fetch or decide'); };
const list = (error: string | null, loading: boolean, hasDrawings = false) => renderToStaticMarkup(
  <PartsLoadState error={error} loading={loading} hasDrawings={hasDrawings} onRetry={noop} />,
);
assert.equal(list(null, false), '');
assert.match(list(null, true), /role="status"/);
assert.match(list('503 <refused>', false), /503 &lt;refused&gt;/);
assert.match(list('failed', false), /Retry parts list/);
assert.doesNotMatch(list('failed', false), /last loaded list/);
assert.match(list('failed', true, true), /last loaded list remains/);
assert.match(list('failed', true), /disabled=""/);
const image = (state: PartImageState) => renderToStaticMarkup(
  <PartPicture state={state} position={2} page={3} onRetry={noop} onError={noop} />,
);
assert.match(image(null), /role="status"/);
assert.doesNotMatch(image(null), /<img/);
const failed = image({ error: true });
assert.match(failed, /role="alert"/);
assert.match(failed, /part 2 on page 3/);
assert.match(failed, /Retry part 2 crop/);
assert.doesNotMatch(failed, /<img|confirmed|<input|Not a part/);
assert.match(image({ url: 'blob:synthetic' }), /Where the code is printed on part 2, page 3/);
assert.doesNotMatch(image({ url: 'blob:synthetic' }), /Retry|role="alert"/);
console.log('part-recovery: list/crop errors, loading, stale-list disclosure, exact page and read-only recovery states passed');
