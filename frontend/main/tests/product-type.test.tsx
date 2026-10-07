import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import { ProductTypeField } from '../src/components/upload/ProductTypeField.js';
import {
  DEFAULT_PRODUCT_TYPE,
  defaultProductType,
  packageCreateBody,
  type ProductChoice,
} from '../src/api/uploadState.js';

// #994: the upload screen asks what the drawing set is for. The choices are the API's, never a list
// in the frontend; Countertop is preselected when offered; the create request carries the value.
const choices: ProductChoice[] = [
  { value: 'countertop', label: 'Countertop', published_checks: 7 },
  { value: 'cabinet', label: 'Cabinets', published_checks: 2 },
];

assert.equal(DEFAULT_PRODUCT_TYPE, 'countertop');
assert.equal(defaultProductType(choices), 'countertop');
assert.equal(defaultProductType([...choices].reverse()), 'countertop', 'order does not matter');
assert.equal(defaultProductType([choices[1]]), 'cabinet', 'the first offered when no countertop');
assert.equal(defaultProductType([]), null, 'nothing published: nothing is chosen for you');
console.log('Countertop is the default product when the rulebook offers it');

const ready = renderToStaticMarkup(
  <ProductTypeField
    state={{ status: 'ready', choices }}
    value={defaultProductType(choices)}
    onChange={() => undefined}
  />,
);
assert.match(ready, /What is this drawing set for\?/);
assert.match(ready, /<option value="countertop" selected="">Countertop<\/option>/);
assert.match(ready, /<option value="cabinet">Cabinets<\/option>/);
assert.equal((ready.match(/<option /g) ?? []).length, 2, 'only the API choices are offered');
assert.match(ready, /Only the checks for this product are run\./);
assert.doesNotMatch(ready, /disabled/);
console.log('the dropdown renders the API choices with Countertop selected');

const empty = renderToStaticMarkup(
  <ProductTypeField state={{ status: 'ready', choices: [] }} value={null} onChange={() => undefined} />,
);
assert.match(empty, /No checks are published yet/);
assert.match(empty, /disabled/);
console.log('with nothing published the dropdown says so and cannot be used');

const loading = renderToStaticMarkup(
  <ProductTypeField state={{ status: 'loading' }} value={null} onChange={() => undefined} />,
);
assert.match(loading, /Loading the products/);
const failed = renderToStaticMarkup(
  <ProductTypeField state={{ status: 'error', message: '503 Service Unavailable' }} value={null} onChange={() => undefined} />,
);
assert.match(failed, /could not be loaded: 503 Service Unavailable/);
assert.match(failed, /role="alert"/);
console.log('loading and failure are shown, never an empty list that looks like a choice');

assert.deepEqual(packageCreateBody('A vendor', 'countertop'), {
  vendor: 'A vendor',
  product_type: 'countertop',
});
assert.deepEqual(packageCreateBody('', 'cabinet'), { vendor: null, product_type: 'cabinet' });
console.log('the create request carries the chosen product');
