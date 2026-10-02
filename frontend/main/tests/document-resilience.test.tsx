import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import type { FindingCounts, PackagePage, ReviewSessionPage } from '../src/api/client';
import { loadDocumentRows, documentListReducer, initialDocumentList } from '../src/pages/documentRows.js';
import { DocumentResults } from '../src/pages/DocumentResults.js';

const documents = [
  { id: 'synthetic-a', current_revision_id: 'rev-a', state: 'AWAITING_REVIEW', vendor: 'Synthetic A' },
  { id: 'synthetic-b', current_revision_id: 'rev-b', state: 'CREATED', vendor: 'Synthetic B' },
] as PackagePage['items'];
const page = { items: documents, next_cursor: null, limit: 50, ordering: 'created_at' } as PackagePage;
const zero: FindingCounts = { total: 0, passed: 0, failed: 0, review_required: 0, not_found: 0, no_applicable_rule: 0, critical_failed: 0 };
const counts: FindingCounts = { ...zero, total: 5, passed: 1, failed: 1, review_required: 1, not_found: 1, no_applicable_rule: 1 };
const sessions: ReviewSessionPage = { items: [{ id: 'synthetic-session', package_revision_id: 'rev-a', reviewer: 'Synthetic reviewer', created_at: '2026-10-03T10:00:00Z', completed_at: null }] };
const original = JSON.stringify(page);
const partial = await loadDocumentRows({
  packages: async () => page,
  counts: async (id) => { if (id === 'synthetic-a') throw new Error('summary 503'); return zero; },
  sessions: async () => { throw new Error('sessions 503'); },
});
assert.equal(partial.rows.length, 2, 'one summary failure cannot hide either document');
assert.equal(partial.rows[0].document, documents[0], 'original backend document object is retained');
assert.equal(partial.rows[0].counts, null, 'unavailable is never fabricated zero');
assert.equal(partial.rows[0].countsError, 'summary 503');
assert.equal(partial.rows[1].counts, zero, 'successful zero remains distinct');
assert.equal(partial.reviewerError, 'sessions 503');
assert.equal(JSON.stringify(page), original, 'no backend record is mutated');

const recovered = await loadDocumentRows({ packages: async () => page, counts: async () => counts, sessions: async () => sessions });
assert.equal(recovered.rows[0].reviewer, 'Synthetic reviewer');
assert.equal(recovered.rows[1].reviewer, null, 'never carry a reviewer across revisions');
assert.equal(recovered.reviewerError, null);
assert.equal(recovered.rows[0].counts, counts);
assert.equal(recovered.rows[0].countsError, null);
assert.equal(recovered.hasMore, false);
const paged = await loadDocumentRows({ packages: async () => ({ ...page, next_cursor: 'opaque-next' }), counts: async () => counts, sessions: async () => sessions });
assert.equal(paged.hasMore, true, 'do not claim the first page is the whole project');
await assert.rejects(loadDocumentRows({ packages: async () => { throw new Error('packages 503'); }, counts: async () => counts, sessions: async () => sessions }), /packages 503/);

let state = documentListReducer(initialDocumentList, { type: 'loaded', data: partial });
state = documentListReducer(state, { type: 'loading' });
assert.equal(state.data, partial, 'retry keeps all recorded rows visible');
assert.equal(state.loading, true);
state = documentListReducer(state, { type: 'failed', error: 'refresh 503' });
assert.equal(state.data, partial);
assert.equal(state.error, 'refresh 503');
assert.equal(state.loading, false);
state = documentListReducer(state, { type: 'loaded', data: recovered });
assert.equal(state.data, recovered);
assert.equal(state.error, null);

const missing = renderToStaticMarkup(<DocumentResults counts={null} error="summary 503" />);
assert.match(missing, /Results unavailable/);
assert.doesNotMatch(missing, /No findings|Looks right/);
const empty = renderToStaticMarkup(<DocumentResults counts={zero} error={null} />);
assert.match(empty, /No findings recorded/);
const populated = renderToStaticMarkup(<DocumentResults counts={counts} error={null} />);
for (const label of ['Looks right', 'Needs correction', 'Needs your decision', 'Waiting on a value', 'Not applicable']) assert.ok(populated.includes(label));
assert.equal((populated.match(/data-outcome-icon=/g) ?? []).length, 5);
assert.equal(JSON.stringify(page), original);
console.log('documents: partial failure isolation, unchanged records, stale-preserving retry, revision-scoped reviewers, pagination disclosure, five accessible outcomes passed');
