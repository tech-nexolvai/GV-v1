import assert from 'node:assert/strict';

import { canUploadFor, packageCreateBody, type ProductChoice } from '../src/api/uploadState.js';

// #1765 (decided 2026-10-19): the start page no longer asks "What is this drawing set for?". A new
// set is checked for every active product (All, the chat chips' default); the reviewer narrows in
// the chat. The create request names no product, which the server stores as "every product".
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

assert.equal(canUploadFor(product('countertop', 'Countertop', true, 7)), true);
assert.equal(canUploadFor(product('lighting', 'Lighting', false, 0)), false, 'inactive: coming later');
assert.equal(canUploadFor(product('cabinet', 'Cabinet', true, 0)), false, 'no published check');
console.log('a product can be named only when it is active and checked');

assert.deepEqual(packageCreateBody('A vendor'), { vendor: 'A vendor' });
assert.deepEqual(packageCreateBody(''), { vendor: null });
assert.equal('product_type' in packageCreateBody('A vendor', null), false, 'no product named: All');
console.log('the start page names no product: the set is checked for every product (All)');

assert.deepEqual(packageCreateBody('A vendor', 'countertop'), { vendor: 'A vendor', product_type: 'countertop' });
console.log('a caller that names a product still sends it');
