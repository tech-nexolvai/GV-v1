import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { ReadingConfirmationFeedback, type ReadingConfirmationState } from '../src/components/measure/ReadingConfirmationFeedback.js';

const render = (state?: ReadingConfirmationState) => renderToStaticMarkup(
  <ReadingConfirmationFeedback id="field-feedback" state={state} />,
);
assert.equal(render(), '');
assert.match(render({ kind: 'saving' }), /role="status"/);
assert.match(render({ kind: 'saving' }), /after the server accepts it/);
const failed = render({ kind: 'error', message: '409: crop <unavailable>' });
assert.match(failed, /id="field-feedback".*role="alert"/);
assert.match(failed, /409: crop &lt;unavailable&gt;/);
assert.match(failed, /field was not changed/);
assert.doesNotMatch(failed, /Reading confirmed/);
assert.match(render({ kind: 'confirmed', preserved: false, multiple: false }), /confirmed and added/);
assert.match(render({ kind: 'confirmed', preserved: true, multiple: false }), /edited value has been kept/);
assert.match(render({ kind: 'confirmed', preserved: false, multiple: true }), /more than one confirmed/);
assert.doesNotMatch(render({ kind: 'confirmed', preserved: true, multiple: true }), /enter the value/);
console.log('reading-confirmation-feedback: pending, exact escaped errors, confirmed, edited and ambiguous states passed');
