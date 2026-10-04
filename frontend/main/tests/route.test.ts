import assert from 'node:assert/strict';
import { formatRoute, parseRoute } from '../src/app/route.js';

assert.deepEqual(parseRoute('#/review/package-1'), { page: 'review', packageId: 'package-1' });
assert.deepEqual(parseRoute('#/documents'), { page: 'documents', packageId: null });
assert.deepEqual(parseRoute('#/%'), { page: 'review', packageId: null });
assert.equal(formatRoute({ page: 'review', packageId: 'package-1' }), '#/review/package-1');

console.log('review route survives reload and malformed hashes');
