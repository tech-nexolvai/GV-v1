import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import type { Finding } from '../src/data/types.js';
import { decisionPayload } from '../src/components/output/reviewerResults.js';

// Load the actual components through Vite, including CSS and import.meta.env, not copies.
const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
try {
  // The Results tab is the countertop table since #1039; these safety properties moved with it.
  const { CountertopTable } = await server.ssrLoadModule('/src/components/results/countertop-table.tsx');
  const { WallGlyph } = await server.ssrLoadModule('/src/components/results/wall-glyph.tsx');
  const { toFinding, withChain } = await server.ssrLoadModule('/src/api/findings.ts');
  const finding: Finding = {
    id: 'synthetic-finding', check_id: 'CHECK-1', name: 'Synthetic check', severity: 'FLAG',
    outcome: 'NOT_FOUND', reviewer_action: 'confirm', reviewer_note: null,
    scope_row_candidate_id: 'synthetic-row',
  };
  const synthetic = {
    finding_id: 'synthetic-finding', row_id: 'synthetic-row', page_number: 3, label: 'Synthetic countertop',
    row_location: null, outcome: 'NOT_FOUND', needs_decision: true, printed_overall: null, pieces: [],
    field_cut_per_end: null, field_cut_count: null, expected_total: null, delta: null, hold: null,
    reviewer_decision: { action: 'confirm', actor: 'synthetic reviewer', note: null, time: '2026-10-09T10:00:00Z' },
    wall_layout: { config: null, label: null, source: 'not established' },
    agreement: { both_readers_agreed_on_row: null, code_clue_used: false, values_agreed: [] },
  };
  const actions = { onShowDrawing: () => {}, onOpenCard: () => {}, onDecide: () => {}, canDecide: (row: { needs_decision: boolean }) => row.needs_decision };
  function results(row: object) {
    return renderToStaticMarkup(createElement(CountertopTable, { rows: [{ ...synthetic, ...row }], actions }));
  }
  await test('FAIL dismissal needs a note; confirmation stays one click', () => {
    for (const note of [undefined, '', '  ']) {
      assert.throws(() => decisionPayload('one', 'FAIL', 'dismiss', note), /note/i);
    }
    assert.deepEqual(decisionPayload('one', 'FAIL', 'confirm'), { finding_id: 'one', action: 'confirm' });
    assert.equal(decisionPayload('one', 'FAIL', 'dismiss', 'Not applicable').note, 'Not applicable');
  });
  await test('an evidence-only confirmation cannot hide the still-required review buttons', () => {
    const html = results({});
    assert.match(html, />Decide<\/button>/);
    assert.match(html, /Pending/);
    assert.doesNotMatch(html, /Checked: OK/, 'a confirmation that did not finish the review never reads as a decision');
  });
  await test('an expired exception keeps review controls available', () => {
    const html = results({ outcome: 'FAIL', reviewer_decision: { ...synthetic.reviewer_decision, action: 'except' } });
    assert.match(html, />Decide<\/button>/);
    assert.doesNotMatch(html, /Exception/);
  });
  await test('a correction explains that checks must run again', () => {
    assert.match(results({ outcome: 'FAIL', reviewer_decision: { ...synthetic.reviewer_decision, action: 'correct' } }), /run (the )?checks again/i);
  });
  await test('a finished decision is shown as the reviewer\'s, never as the check\'s result', () => {
    const html = results({ needs_decision: false });
    assert.match(html, /Checked: OK/);
    assert.match(html, /Not found|Waiting on a value/, 'the recorded outcome badge stays');
    assert.doesNotMatch(html, />Decide<\/button>/);
  });
  await test('the chat card also restores unresolved review controls', () => {
    // Chat uses the same tested card, mounted when its table disclosure is opened.
    const source = readFileSync('src/components/chat/ChatThread.tsx', 'utf8');
    assert.match(source, /needsDecision=\{blockingFindingIds\?\.includes\(finding.id\)\}/);
    assert.match(readFileSync('src/pages/ReviewPage.tsx', 'utf8'), /blockingFindingIds=\{readiness\?\.blocking_finding_ids\}/);
  });
  await test('wall sources are reviewer words, not storage codes', () => {
    for (const source of ['drawing clues', 'both readers', 'reviewer', 'between panels', 'not established']) {
      const html = renderToStaticMarkup(createElement(WallGlyph, { layout: { config: 'back_only', label: 'back wall only', source } }));
      assert.doesNotMatch(html, /vendor-drawing-clues|wall-source:|walls-sealed:/);
      if (source === 'drawing clues') assert.match(html, /drawing/);
      if (source === 'both readers') assert.match(html, /AIs/);
      if (source === 'between panels') assert.match(html, /side panels/i);
      if (source === 'not established') assert.match(html, /countertop card/i);
    }
  });
  await test('plain missing-input reasons survive both list and evidence loading', () => {
    const raw = "required value 'shop_cabinets' is missing";
    const plain = "This check needs the vendor's cabinet widths.";
    const listed = toFinding({ ...finding, rule_id: 'CHECK-1', reviewer_action: null,
      reason: raw, reviewer_reason: plain });
    assert.equal(listed.reason, plain);
    const enriched = withChain(listed, { operands: [], reviewer_reason: plain,
      trace: { kind: 'abstention', cause: 'missing_input', reason: raw } });
    assert.equal(enriched.reason, plain);
  });
} finally {
  await server.close();
}
