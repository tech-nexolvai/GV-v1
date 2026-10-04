import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import type { components } from '../src/api/schema.js';
import { ChangedValuesPanel } from '../src/components/output/ChangedValuesPanel.js';

type ChangedValues = components['schemas']['ChangedValuesOut'];

const revision = '00000000-0000-4000-8000-000000000101';
const base: ChangedValues = {
  revision_id: revision,
  status: 'available',
  message: null,
  company_standards_displaced: ['synthetic standard changed by reviewer'],
  outstanding: ['synthetic_required_value'],
};
const render = (value: ChangedValues) => renderToStaticMarkup(
  <ChangedValuesPanel state="ready" value={value} currentRevisionId={revision} />,
);

const available = render(base);
assert.match(available, /synthetic standard changed by reviewer/);
assert.match(available, /synthetic_required_value/);

const unavailable = render({
  ...base,
  status: 'unavailable',
  message: 'Not available for this check run; re-run the checks to see it',
  company_standards_displaced: [],
  outstanding: [],
});
assert.match(unavailable, /Not available for this check run/);
assert.doesNotMatch(unavailable, /synthetic standard changed/);

assert.match(renderToStaticMarkup(
  <ChangedValuesPanel state="loading" value={null} currentRevisionId={revision} />,
), /Loading recorded/);
assert.match(renderToStaticMarkup(
  <ChangedValuesPanel state="error" value={null} currentRevisionId={revision} />,
), /could not be loaded/);
assert.match(renderToStaticMarkup(
  <ChangedValuesPanel state="ready" value={base} currentRevisionId="another revision" />,
), /Refresh this review/);

console.log('changed values panel states verified');
