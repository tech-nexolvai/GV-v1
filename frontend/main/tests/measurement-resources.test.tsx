import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { loadMeasurementResources } from '../src/pages/measurementResources.js';
import { ReadingAvailability } from '../src/pages/ReadingAvailability.js';

const fields = { quantities: [{ key: 'SHOP:width' }], confirmed_readings: [{ value: '25 1/4 in' }] };
const before = JSON.stringify(fields);
const refused = await loadMeasurementResources(Promise.resolve(fields), Promise.reject(new Error('413: too many readings')), Promise.resolve(['width']));
assert.equal(refused.fields, fields);
assert.deepEqual(refused.readings, { value: null, error: '413: too many readings' });
assert.deepEqual(refused.vocabulary, { value: ['width'], error: null });
assert.equal(JSON.stringify(fields), before);
const empty = await loadMeasurementResources(Promise.resolve(fields), Promise.resolve({ candidates: [], total: 0 }), Promise.resolve([]));
assert.equal(empty.readings.error, null, 'a genuine empty result is not a failed reading list');
const badTypes = await loadMeasurementResources(Promise.resolve(fields), Promise.resolve({ candidates: ['fixture'] }), Promise.reject(new Error('Vocabulary unavailable')));
assert.equal(badTypes.fields, fields);
assert.equal(badTypes.vocabulary.error, 'Vocabulary unavailable');
assert.deepEqual(badTypes.readings.value, { candidates: ['fixture'] });
await assert.rejects(loadMeasurementResources(Promise.reject(new Error('Rules unavailable')), Promise.resolve([]), Promise.resolve([])), /Rules unavailable/);
await assert.rejects(loadMeasurementResources(Promise.reject(new Error('Rules unavailable')), Promise.reject(new Error('readings')), Promise.reject(new Error('types'))), /Rules unavailable/);
// A subsequent retry can recover the reading list independently of required fields.
const recovered = await loadMeasurementResources(Promise.resolve(fields), Promise.resolve({ candidates: ['recovered'] }), Promise.resolve(['width']));
assert.deepEqual(recovered.readings, { value: { candidates: ['recovered'] }, error: null });
for (const retrying of [false, true]) {
  const html = renderToStaticMarkup(<ReadingAvailability error="413 <refused>" retrying={retrying} onRetry={() => { throw new Error('render must not fetch'); }} />);
  assert.match(html, /role="alert"/);
  assert.match(html, /413 &lt;refused&gt;/);
  assert.match(html, /unavailable list does not mean no dimensions were read/);
  assert.match(html, retrying ? /disabled=""/ : /Retry readings/);
  assert.doesNotMatch(html, /0 readings|Nothing was read/);
}
console.log('measurement-resources: required fields retained, empty vs refused, vocabulary isolation, fatal fields, recovery and accessible warning passed');
