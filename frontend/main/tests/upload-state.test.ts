import assert from 'node:assert/strict';
import { PartialUploadError } from '../src/api/uploadState.js';
const original = new Error('Storage unavailable');
const partial = new PartialUploadError('saved-package', original);
assert.equal(partial.packageId, 'saved-package');
assert.equal(partial.cause, original);
assert.equal(partial.message, 'Storage unavailable');
assert.doesNotMatch(partial.message, /Nothing has been recorded/);
console.log('partial upload record preservation tests passed');
