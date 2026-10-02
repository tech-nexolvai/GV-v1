import assert from 'node:assert/strict';
import { parseRoute, formatRoute } from '../src/app/route.js';
assert.deepEqual(parseRoute('#/review/%'), { page: 'review', packageId: null });
assert.deepEqual(parseRoute('#/documents'), { page: 'documents', packageId: null });
assert.deepEqual(parseRoute(formatRoute({ page: 'review', packageId: 'saved-package' })), { page: 'review', packageId: 'saved-package' });
console.log('route preservation and malformed URL tests passed');
