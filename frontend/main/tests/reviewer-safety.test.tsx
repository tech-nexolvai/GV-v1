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
  const { ResultsPanel } = await server.ssrLoadModule('/src/components/output/ResultsPanel.tsx');
  const { toFinding, withChain } = await server.ssrLoadModule('/src/api/findings.ts');
  const finding: Finding = {
    id: 'synthetic-finding', check_id: 'CHECK-1', name: 'Synthetic check', severity: 'FLAG',
    outcome: 'NOT_FOUND', reviewer_action: 'confirm', reviewer_note: null,
    scope_row_candidate_id: 'synthetic-row',
  };
  const callbacks = {
    onViewEvidence: () => {}, onAction: async () => ({ saved: true }),
    onCorrect: async () => ({ saved: true }), onExcept: async () => ({ saved: true }),
  };
  function results(item: Finding, blocked: boolean, rows: object[] = []) {
    return renderToStaticMarkup(createElement(ResultsPanel, {
      ...callbacks, findings: [item], rows, selected: null, busy: false,
      onRefresh: () => {}, onShowDrawing: () => {}, onOpenRow: () => {},
      readiness: { can_approve: !blocked, blocking_findings: blocked ? 1 : 0,
        blocking_finding_ids: blocked ? [item.id] : [] },
    }));
  }
  await test('FAIL dismissal needs a note; confirmation stays one click', () => {
    for (const note of [undefined, '', '  ']) {
      assert.throws(() => decisionPayload('one', 'FAIL', 'dismiss', note), /note/i);
    }
    assert.deepEqual(decisionPayload('one', 'FAIL', 'confirm'), { finding_id: 'one', action: 'confirm' });
    assert.equal(decisionPayload('one', 'FAIL', 'dismiss', 'Not applicable').note, 'Not applicable');
  });
  await test('an evidence-only confirmation cannot hide the still-required review buttons', () => {
    const html = results(finding, true);
    assert.match(html, />Not checkable<\/button>/);
    assert.match(html, />Checked: OK<\/button>/);
    assert.doesNotMatch(html, /Reviewer decision: Checked: OK/);
  });
  await test('an expired exception keeps review controls available', () => {
    assert.match(results({ ...finding, outcome: 'FAIL', reviewer_action: 'except' }, true), />Dismiss<\/button>/);
  });
  await test('a correction explains that checks must run again', () => {
    assert.match(results({ ...finding, outcome: 'FAIL', reviewer_action: 'correct' }, true), /run (the )?checks again/i);
  });
  await test('the chat card also restores unresolved review controls', () => {
    // Chat uses the same tested card, mounted when its table disclosure is opened.
    const source = readFileSync('src/components/chat/ChatThread.tsx', 'utf8');
    assert.match(source, /needsDecision=\{blockingFindingIds\?\.includes\(finding.id\)\}/);
    assert.match(readFileSync('src/pages/ReviewPage.tsx', 'utf8'), /blockingFindingIds=\{readiness\?\.blocking_finding_ids\}/);
  });
  await test('wall sources are reviewer words, not storage codes', () => {
    for (const source of ['vendor-drawing-clues', 'drawing-and-readers', 'readers', 'reviewer', 'between-panels']) {
      const html = results(finding, true, [{ row_id: 'synthetic-row', values: [],
        wall_config: null, wall_proposal: 'back_only', wall_source: source }]);
      assert.doesNotMatch(html, new RegExp(` · ${source}<`));
      if (source === 'vendor-drawing-clues') assert.match(html, /drawing clues/i);
      if (source === 'drawing-and-readers' || source === 'readers') assert.match(html, /needs (your )?confirmation/i);
      if (source === 'between-panels') assert.match(html, /side panels/i);
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
