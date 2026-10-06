import assert from 'node:assert/strict';
import { describeUploadFailure, IncompletePackageUpload } from '../src/api/uploadState.js';

const saved = describeUploadFailure(new IncompletePackageUpload('package-1', new Error('upload refused')));
assert.deepEqual(saved, { detail: 'upload refused', savedPackageId: 'package-1' });

const unknown = describeUploadFailure(new Error('connection lost'));
assert.deepEqual(unknown, { detail: 'connection lost', savedPackageId: null });

console.log('upload failure preserves saved package identity');

// #963: one file in both slots is one combined set — uploaded once, as the shop drawing.
import { looksLikeTheSameFile, uploadPlan } from '../src/api/uploadState.js';
assert.deepEqual(uploadPlan('A', 'A', true), [['A', 'shop']]);
assert.deepEqual(uploadPlan('A', 'B', false), [['A', 'architectural'], ['B', 'shop']]);
assert.deepEqual(uploadPlan(null, 'B', false), [['B', 'shop']]);
assert.deepEqual(uploadPlan('A', null, true), [['A', 'architectural']]);
const file = { name: 'set.pdf', size: 10, lastModified: 1 };
assert.equal(looksLikeTheSameFile(file, { ...file }), true);
assert.equal(looksLikeTheSameFile(file, { ...file, size: 11 }), false);
assert.equal(looksLikeTheSameFile(file, null), false);
console.log('the same file in both slots is uploaded once');
