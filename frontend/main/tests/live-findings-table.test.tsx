/**
 * The findings table, rendered from a **live API payload**.
 *
 * `findings-table.test.tsx` builds its own `Finding` objects, which tests the table's ordering and
 * highlighting and cannot catch the one thing this does: that what `GET .../findings` actually
 * sends still carries the fields the screen reads. The PR that added the table said it had not been
 * run against a live API; this is that half.
 *
 * **What it does not cover.** It maps the payload here rather than importing `src/api/findings.ts`,
 * because that module pulls in `client.ts` and its Vite-only `import.meta.env`, and this build's
 * `include` list is deliberately narrow — pure renderable pieces, no network layer. So a change to
 * `toFinding` that reads a *new* field will not fail here. A change on the **server** that stops
 * sending one will, and that is the direction a fixture can never see.
 */
import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import type { Finding, Outcome, ReviewerAction, Severity } from '../src/data/types.js';
import { FindingsTable } from '../src/components/output/FindingsTable.js';
import { LIVE_FINDING_ITEMS } from './liveFindings.js';

// The five fields `src/api/findings.ts:toFinding` reads off a listed finding. Named here so a
// server that stops sending one fails on the field rather than on a blank row.
for (const item of LIVE_FINDING_ITEMS) {
  for (const field of ['id', 'rule_id', 'outcome', 'severity'] as const) {
    assert.ok(
      (item as Record<string, unknown>)[field],
      `the server sent a finding with no ${field}`,
    );
  }
}

const findings: Finding[] = LIVE_FINDING_ITEMS.map((item) => ({
  id: item.id,
  check_id: item.rule_id,
  name: item.rule_id,
  outcome: item.outcome as Outcome,
  severity: item.severity as Severity,
  reviewer_action: null as ReviewerAction | null,
  reviewed_by: null,
}));

assert.ok(findings.length >= 5, `only ${findings.length} findings came back`);

const html = renderToStaticMarkup(<FindingsTable findings={findings} />);

// Every rule that was checked is on screen. A table that silently dropped a row would still look
// like a table.
for (const finding of findings) {
  assert.ok(html.includes(finding.check_id), `${finding.check_id} is missing from the table`);
}

// The ordering the PR exists for: what asks something of the reviewer comes first. The seeded
// package has one FAIL among NOT_FOUNDs, so the FAIL must be above them.
const failing = findings.find((finding) => finding.outcome === 'FAIL');
assert.ok(failing, 'the seeded package produced no FAIL, so ordering is not being tested');
const notFound = findings.find((finding) => finding.outcome === 'NOT_FOUND');
assert.ok(notFound, 'the seeded package produced no NOT_FOUND');
assert.ok(
  html.indexOf(failing.check_id) < html.indexOf(notFound.check_id),
  'a NOT_FOUND row is above the FAIL row',
);

// The markdown round trip this replaced leaked bare pipes into the rendered answer mid-reveal.
assert.ok(!html.includes('|'), 'a pipe character reached the rendered table');
assert.ok(html.includes('<table'), 'no table in the output');

console.log(`live findings table: ${findings.length} rows from a real API payload`);
