import assert from 'node:assert/strict';
import { formatRoute, parseRoute } from '../src/app/route.js';

// The old review screens' address (#/review/<set>, and /advanced) was retired on 2026-10-28: a saved or
// shared link opens that drawing set's chat.
assert.deepEqual(parseRoute('#/review/package-1'), { page: 'chat', packageId: 'package-1' });
assert.deepEqual(parseRoute('#/review/package-1/advanced'), { page: 'chat', packageId: 'package-1' });
assert.deepEqual(parseRoute('#/review'), { page: 'review', packageId: null });
assert.deepEqual(parseRoute('#/documents'), { page: 'documents', packageId: null });
assert.deepEqual(parseRoute('#/%'), { page: 'review', packageId: null });
assert.equal(formatRoute({ page: 'review', packageId: 'package-1' }), '#/chat/package-1');

console.log('an old review link opens the chat, and malformed hashes the start page');

// #1578: the job tracker, and a chat link that opens one finding.
assert.deepEqual(parseRoute('#/tracker'), { page: 'tracker', packageId: null });
assert.equal(formatRoute({ page: 'tracker', packageId: null }), '#/tracker');
const focused = { page: 'chat' as const, packageId: 'set-1', chatId: 'chat-1', finding: { messageId: 'answer-1', findingId: 'F3' } };
assert.equal(formatRoute(focused), '#/chat/set-1/chat-1?message=answer-1&finding=F3');
assert.deepEqual(parseRoute(formatRoute(focused)), focused);
assert.deepEqual(parseRoute('#/chat/set-1/chat-1?finding=F3'), { page: 'chat', packageId: 'set-1', chatId: 'chat-1' });

console.log('tracker route and focused chat links survive reload');
