import assert from 'node:assert/strict';
import { readDocumentPosition, saveDocumentPosition } from '../src/pages/documentPosition.js';
import { restoreDocumentList, documentListReducer, documentNavigation } from '../src/pages/documentRows.js';
import type { DocumentRows } from '../src/pages/documentRows.js';

const values = new Map<string, string>();
const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
const cursors = ['opaque+/next=2', 'opaque-next-3'];
assert.deepEqual(readDocumentPosition(storage, 'project-a'), { cursors: [], notice: null });
assert.equal(saveDocumentPosition(storage, 'project-a', cursors), null);
assert.deepEqual(readDocumentPosition(storage, 'project-a').cursors, cursors, 'remount/reload restores opaque cursors unchanged');
assert.deepEqual(readDocumentPosition(storage, 'project-b').cursors, [], 'projects do not share positions');
assert.deepEqual([...values.keys()], ['gv:documents-position:project-a']);
assert.deepEqual(JSON.parse([...values.values()][0]), { version: 1, cursors }, 'persist only navigation metadata, no backend records');

let state = restoreDocumentList(readDocumentPosition(storage, 'project-a').cursors);
assert.equal(state.requestedTrail.at(-1), 'opaque-next-3');
assert.equal(documentNavigation(state).page, 3);
assert.equal(state.data, null, 'saved navigation is not cached findings or document truth');
state = documentListReducer(state, { type: 'failed', error: 'expired cursor' });
assert.equal(state.data, null);
assert.equal(state.requestedTrail.at(-1), 'opaque-next-3', 'retry still targets saved page');
state = documentListReducer(state, { type: 'first' });
assert.deepEqual(state.requestedTrail, [undefined]);
assert.equal(state.error, null);
const fresh: DocumentRows = { rows: [], reviewerError: null, hasMore: false, nextCursor: null };
state = documentListReducer(state, { type: 'loaded', data: fresh });
assert.equal(documentNavigation(state).page, 1);
saveDocumentPosition(storage, 'project-a', []);
assert.deepEqual(readDocumentPosition(storage, 'project-a').cursors, []);

for (const raw of ['{bad', 'null', '[]', '{"version":2,"cursors":[]}', '{"version":1,"cursors":[1]}', '{"version":1,"cursors":[""]}', '{"version":1,"cursors":["same","same"]}']) {
  values.set('gv:documents-position:project-a', raw);
  const invalid = readDocumentPosition(storage, 'project-a');
  assert.deepEqual(invalid.cursors, []);
  assert.ok(invalid.notice, 'invalid metadata is explained rather than crashing');
}
const blocked = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('quota'); } };
assert.ok(readDocumentPosition(blocked, 'project-a').notice);
assert.ok(saveDocumentPosition(blocked, 'project-a', cursors));
assert.ok(readDocumentPosition(null, 'project-a').notice);
assert.ok(saveDocumentPosition(null, 'project-a', cursors));
console.log('document position: project-scoped cursor-only persistence, restore, malformed/blocked storage and expired-cursor recovery passed');
