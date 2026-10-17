import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import { ProductTypeField } from '../src/components/upload/ProductTypeField.js';
import {
  canUploadFor,
  defaultProductType,
  packageCreateBody,
  type ProductChoice,
} from '../src/api/uploadState.js';

// #994, #1547: the upload screen asks what the drawing set is for. The choices are the API's
// (`GET /products`, the product registry), never a list in the frontend: an active product with
// published checks can be chosen, an inactive one is greyed "coming later", an active one with no
// check is greyed too. The first that can be chosen is preselected; the create request carries it.
function product(id: string, label: string, active: boolean, published_checks: number): ProductChoice {
  return {
    id,
    label,
    plural: `${label}s`,
    description: `${label} description`,
    active,
    draft: !active,
    check_kinds: ['dimension_match', 'global_rules'],
    category_label: `${label}s`,
    checklist: [{ name: 'sizes', text: 'Sizes.' }],
    published_checks,
  };
}

const choices: ProductChoice[] = [
  product('countertop', 'Countertop', true, 7),
  product('cabinet', 'Cabinet', true, 2),
  product('lighting', 'Lighting', false, 0),
];

assert.equal(canUploadFor(choices[0]), true);
assert.equal(canUploadFor(choices[2]), false, 'inactive: coming later');
assert.equal(canUploadFor(product('cabinet', 'Cabinet', true, 0)), false, 'no published check');
assert.equal(defaultProductType(choices), 'countertop');
assert.equal(defaultProductType([choices[2], choices[1]]), 'cabinet', 'the first that can be chosen');
assert.equal(defaultProductType([choices[2]]), null, 'nothing can be chosen: nothing is chosen for you');
assert.equal(defaultProductType([]), null);
console.log('the first product that can be uploaded for is the default');

const ready = renderToStaticMarkup(
  <ProductTypeField
    state={{ status: 'ready', choices }}
    value={defaultProductType(choices)}
    onChange={() => undefined}
  />,
);
assert.match(ready, /What is this drawing set for\?/);
assert.match(ready, /<option value="countertop" title="Countertop description" selected="">Countertops<\/option>/);
assert.match(ready, /<option value="cabinet" title="Cabinet description">Cabinets<\/option>/);
assert.match(ready, /<option value="lighting" disabled="" title="Lighting description">Lightings \(coming later\)<\/option>/);
assert.equal((ready.match(/<option /g) ?? []).length, 3, 'only the API choices are offered');
assert.match(ready, /Only the checks for this product are run\./);
// #1125: the shadcn input classes name `disabled:` states, so the attribute itself is asserted.
assert.doesNotMatch(ready, /<select[^>]* disabled=""/);
assert.match(ready, /<label[^>]*for="([^"]+)"[^>]*>What is this drawing set for\?<\/label>/, 'the label names the select');
console.log('the dropdown renders every product, the inactive one greyed as coming later');

const unchecked = renderToStaticMarkup(
  <ProductTypeField
    state={{ status: 'ready', choices: [product('dummy_widget', 'Dummy widget', true, 0), ...choices] }}
    value="countertop"
    onChange={() => undefined}
  />,
);
assert.match(unchecked, /disabled="" title="Dummy widget description">Dummy widgets \(no checks published yet\)<\/option>/);
console.log('a product added to the registry appears with no frontend change');

const empty = renderToStaticMarkup(
  <ProductTypeField state={{ status: 'ready', choices: [choices[2]] }} value={null} onChange={() => undefined} />,
);
assert.match(empty, /No checks are published yet/);
assert.match(empty, /<select[^>]* disabled=""/);
console.log('with nothing that can be chosen the dropdown says so and cannot be used');

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
