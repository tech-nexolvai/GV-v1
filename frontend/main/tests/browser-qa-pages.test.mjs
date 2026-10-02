import assert from 'node:assert/strict';
import { pageFixture, pageRules, pageSettings, syntheticNextCursor } from './browser-qa-pages.mjs';

for (const mode of ['populated', 'empty', 'error']) {
  const rules = pageFixture('/api/v1/rules', mode);
  const settings = pageFixture('/api/v1/company-settings', mode);
  if (mode === 'error') {
    assert.equal(rules.status, 503);
    assert.equal(settings.status, 503);
    assert.equal(pageFixture('/api/v1/projects/synthetic/packages', mode).status, 503);
  } else {
    assert.equal(rules.status, 200);
    assert.equal(settings.status, 200);
    assert.equal(rules.body.length, mode === 'empty' ? 0 : 2);
    assert.equal(settings.body.settings.length, mode === 'empty' ? 0 : 2);
  }
}
assert.ok(pageRules.every(rule => !rule.production_ready && rule.rule_id.startsWith('SYNTHETIC-')));
assert.equal(pageSettings.settings[1].in_use, null, 'missing is null, never an invented zero');
assert.equal(pageSettings.settings[0].in_use, '20 1/2 in', 'fixture exact text stays unchanged');
assert.equal(pageFixture('/api/v1/projects/synthetic/packages', 'populated'), null, 'baseline package data is not replaced');
assert.equal(pageFixture('/api/v1/unimplemented-route', 'populated'), null, 'unknown endpoints remain unimplemented');
assert.equal(pageFixture('/api/v1/projects/synthetic/packages', 'partial'), null, 'partial failures leave the primary records untouched');
assert.equal(pageFixture('/api/v1/projects/synthetic/packages/00000000-0000-4000-8000-000000000101/findings/summary', 'partial').status, 503);
assert.equal(pageFixture('/api/v1/projects/synthetic/packages/00000000-0000-4000-8000-000000000102/findings/summary', 'partial'), null, 'other documents still load');
assert.equal(pageFixture('/api/v1/projects/synthetic/review-sessions', 'partial').status, 503);
console.log('page fixtures: distinct populated/empty/error states, exact text and synthetic isolation passed');
const first = pageFixture('/api/v1/projects/synthetic/packages', 'paginated');
assert.equal(first.body.next_cursor, syntheticNextCursor);
const second = pageFixture('/api/v1/projects/synthetic/packages', 'paginated', syntheticNextCursor);
assert.equal(second.body.next_cursor, null);
assert.notEqual(first.body.items[0].id, second.body.items[0].id);
assert.equal(pageFixture('/api/v1/projects/synthetic/packages', 'page-error', syntheticNextCursor).status, 503);
assert.equal(pageFixture('/api/v1/projects/synthetic/packages', 'paginated', 'made-up').status, 400);
