import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import type { CountertopResult } from '../src/api/client.js';
import { CountertopStrip } from '../src/components/results/CountertopStrip.js';

// Static render (no DOM) of the countertop picture (#1043). Synthetic values only.
const x = (numerator: string, denominator: string, display: string) => ({ numerator, denominator, display });
const row: CountertopResult = {
  finding_id: 'f', row_id: 'r', page_number: 1, label: 'Synthetic countertop', row_location: null,
  outcome: 'FAIL', needs_decision: false, printed_overall: x('72', '1', '72"'),
  pieces: [
    { index: 0, value: x('30', '1', '30"'), kind: 'cabinet', source: 'sealed' },
    { index: 1, value: x('105', '8', '13 1/8"'), kind: 'filler', source: 'typed' },
    { index: 2, value: x('18', '1', '18"'), kind: 'appliance_space', source: 'sealed' },
  ],
  field_cut_per_end: x('1', '1', '1"'), field_cut_count: 2, expected_total: x('509', '8', '63 5/8"'),
  delta: x('67', '8', '8 3/8"'), hold: null, reviewer_decision: null,
  wall_layout: { config: 'back_left_right', label: 'back wall and both ends', source: 'reviewer' },
  agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [true, true, null, true] },
};

const html = renderToStaticMarkup(<CountertopStrip row={row} size="full" />);
assert.match(html, /role="img"/);
assert.match(html, /aria-label="Printed 72&quot;, needed 63 5\/8&quot;, over by 8 3\/8&quot;/, 'the picture states its facts in words');
assert.match(html, /data-bracket="printed" data-label="72&quot;"/);
assert.match(html, /data-bracket="needed" data-label="63 5\/8&quot;"/);
assert.match(html, /data-cap="left" data-label="\+1&quot;"/);
assert.match(html, /data-cap="right" data-label="\+1&quot;"/);
assert.match(html, /data-wall="back"/);
assert.match(html, /data-piece="1" data-kind="filler" data-source="typed" data-label="13 1\/8&quot;"/, 'exact fraction text, never a float');
assert.match(html, /data-kind="appliance"/);
assert.match(html, /\+8 3\/8&quot;/, 'the overrun carries a plus sign');
assert.match(html, /data-outcome-icon="FAIL"/, 'the difference colour comes with its glyph');
assert.doesNotMatch(html, /\d\.\d{3,}&quot;/, 'no float is ever written as a label');

const islandHtml = renderToStaticMarkup(<CountertopStrip row={{ ...row, wall_layout: { config: 'island', label: 'island; no wall ends', source: 'reviewer' } }} />);
assert.doesNotMatch(islandHtml, /data-cap=/, 'no field-cut caps without walls');
assert.doesNotMatch(islandHtml, /data-wall=/);

const held = renderToStaticMarkup(<CountertopStrip row={{ ...row, delta: null, expected_total: null, hold: { code: 'row-held', reason: 'Synthetic hold' } }} />);
assert.match(held, /data-held="true"/);
assert.doesNotMatch(held, /data-slot="strip-difference"/, 'a held row shows no difference');

assert.match(renderToStaticMarkup(<CountertopStrip row={{ ...row, pieces: [] }} />), /No picture: pieces not read/);
console.log('countertop strip: exact labels, brackets, caps only at walls, held overlay and refusal render statically');
