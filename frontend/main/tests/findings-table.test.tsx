import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';

import { FindingsTable } from '../src/components/output/FindingsTable.js';
import { closedRows, sortFindings, toggleRow } from '../src/components/output/findingsOrder.js';
import type { Finding } from '../src/data/types.js';

function finding(id: string, outcome: Finding['outcome'], name: string, shop?: string, arch?: string): Finding {
  const operands: NonNullable<Finding['recorded_operands']> = [];
  if (arch) operands.push({ name: 'approved', value: arch, source: 'ARCH', status: 'APPROVED', hasEvidence: true, documentRole: 'ARCH' });
  if (shop) operands.push({ name: 'vendor', value: shop, source: 'SHOP', status: 'APPROVED', hasEvidence: true, documentRole: 'SHOP' });
  return {
    id,
    check_id: `RULE-${id}`,
    name,
    outcome,
    severity: 'FLAG',
    reviewer_action: null,
    recorded_operands: operands,
    shop_evidence: shop
      ? { canonical_observation_id: `obs-${id}`, page: 13, polygon: [[0, 0]], semantic_type: 'countertop_depth' }
      : null,
    arch_evidence: null,
  } as Finding;
}

const findings: Finding[] = [
  finding('pass-1', 'PASS', 'Countertop width', '83 in', '83 in'),
  finding('missing-1', 'NOT_FOUND', 'Filler width'),
  finding('fail-1', 'FAIL', 'Countertop depth', '25 1/2 in', '25 in'),
  finding('review-1', 'REVIEW_REQUIRED', 'Cabinet run', '107 3/4 in', '108 in'),
  finding('fail-2', 'FAIL', 'Sink cutout', '440 mm', '445 mm'),
];

// --- ordering -------------------------------------------------------------------------------

assert.deepEqual(
  sortFindings(findings).map((item) => item.id),
  ['fail-1', 'fail-2', 'review-1', 'missing-1', 'pass-1'],
  'fail first, then review, not found, pass; ties keep the engine order',
);
assert.equal(findings[0].id, 'pass-1', "sorting does not reorder the caller's array");

// --- default view: the table, nothing else ----------------------------------------------------

const narratives = { 'fail-1': 'The vendor drew the countertop half an inch deeper than approved.' };
const html = renderToStaticMarkup(
  <FindingsTable findings={findings} narratives={narratives} onViewEvidence={() => undefined} />,
);

assert.match(html, /<table class="ftable">/);
assert.doesNotMatch(html, /\|/, 'no markdown pipe ever reaches the screen');
assert.equal(html.match(/class="ftable__row"/g)?.length, 5, 'one row per finding');
assert.doesNotMatch(html, /half an inch deeper/, 'narratives are closed by default');
assert.doesNotMatch(html, /ftable__detail/);
assert.ok(
  html.indexOf('Countertop depth') < html.indexOf('Countertop width'),
  'the failing check is above the passing one',
);

// The vendor's value is the highlighted one in a FAIL row, and only there.
assert.equal(html.match(/data-mismatch="true"/g)?.length, 2, 'both FAIL rows highlight the shop value');
assert.match(html, /data-mismatch="true">25 1\/2 in</);

// A missing value is a dash with an accessible name, not repeated words.
assert.match(html, /aria-label="Not recorded">—</);
assert.doesNotMatch(html, />Not recorded</);

// Evidence button only where there is evidence.
assert.equal(html.match(/View evidence for/g)?.length, 4, 'the finding with no evidence has no evidence button');

// --- an opened row ----------------------------------------------------------------------------

const opened = renderToStaticMarkup(
  <FindingsTable
    findings={findings}
    narratives={narratives}
    initiallyOpen={['fail-1', 'pass-1']}
    renderDetail={(item) => <div className="card-stub">actions for {item.id}</div>}
  />,
);
assert.match(opened, /half an inch deeper/, 'an opened row shows its narrative');
assert.match(opened, /actions for fail-1/, 'and the reviewer actions');
assert.match(opened, /aria-expanded="true"/);
assert.equal(opened.match(/class="ftable__detail"/g)?.length, 2);

// --- CodeRabbit #4114736983: one outcome wording, shared with the markdown table -----------------

assert.match(html, />Review required</, 'the chip uses the same label as the markdown table');
assert.match(html, />Not found</);
assert.doesNotMatch(html, />Review</, 'no shortened second wording');

// --- CodeRabbit #4114736988: a failed load is not "not recorded" ---------------------------------

{
  const unloaded = { ...finding('u-1', 'PASS', 'Countertop width'), recorded_operands: undefined } as Finding;
  const markup = renderToStaticMarkup(<FindingsTable findings={[unloaded]} />);
  assert.match(markup, /aria-label="Not loaded">—</, 'values the chain request did not return say so');
  assert.doesNotMatch(markup, /aria-label="Not recorded"/);
}

// --- CodeRabbit #4114736993: closing a row keeps its detail (and any draft) mounted ------------

{
  let state = closedRows();
  assert.equal(state.mounted.has('fail-1'), false, 'a row never opened mounts nothing');
  state = toggleRow(state, 'fail-1');
  assert.ok(state.open.has('fail-1') && state.mounted.has('fail-1'), 'opening mounts the detail');
  state = toggleRow(state, 'fail-1');
  assert.equal(state.open.has('fail-1'), false, 'closing closes it');
  assert.ok(state.mounted.has('fail-1'), 'but keeps it mounted, so a half-typed correction survives');
  state = toggleRow(state, 'fail-1');
  assert.ok(state.open.has('fail-1'), 'reopening shows the same mounted detail');
}

// --- empty ------------------------------------------------------------------------------------

assert.equal(renderToStaticMarkup(<FindingsTable findings={[]} />), '', 'no findings, no empty frame');

const outDir = new URL('../../elevation-preview/', import.meta.url);
mkdirSync(outDir, { recursive: true });
writeFileSync(new URL('findings-table.html', outDir), opened);

console.log('findings table: all assertions passed');
