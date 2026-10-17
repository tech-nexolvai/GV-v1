import assert from 'node:assert/strict';
import { formatRoute, parseRoute } from '../src/app/route.js';

assert.deepEqual(parseRoute('#/review/package-1'), { page: 'review', packageId: 'package-1' });
assert.deepEqual(parseRoute('#/documents'), { page: 'documents', packageId: null });
assert.deepEqual(parseRoute('#/%'), { page: 'review', packageId: null });
assert.equal(formatRoute({ page: 'review', packageId: 'package-1' }), '#/review/package-1');

console.log('review route survives reload and malformed hashes');

// #1578: the job tracker, and a chat link that opens one finding.
assert.deepEqual(parseRoute('#/tracker'), { page: 'tracker', packageId: null });
assert.equal(formatRoute({ page: 'tracker', packageId: null }), '#/tracker');
const focused = { page: 'chat' as const, packageId: 'set-1', chatId: 'chat-1', finding: { messageId: 'answer-1', findingId: 'F3' } };
assert.equal(formatRoute(focused), '#/chat/set-1/chat-1?message=answer-1&finding=F3');
assert.deepEqual(parseRoute(formatRoute(focused)), focused);
assert.deepEqual(parseRoute('#/chat/set-1/chat-1?finding=F3'), { page: 'chat', packageId: 'set-1', chatId: 'chat-1' });

console.log('tracker route and focused chat links survive reload');
