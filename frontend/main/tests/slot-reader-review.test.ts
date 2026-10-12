import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  shouldOfferRowWallControl,
  slotReaderReviewPayload,
} from '../src/components/measure/slotReaderReview.js';
import type { SlotReaderRow } from '../src/api/client.js';

const row = {
  row_id: 'row-a',
  page_number: 2,
  label: 'Countertop row on page 2',
  piece_count: 1,
  held_reason: null,
  wall_confirmation_allowed: true,
  values: [],
  wall_proposal: 'back_left_right',
  wall_source: 'readers',
  wall_reason: null,
  wall_layout_choices: ['back_left_right'],
  decision_id: null,
  confirmed_by: null,
  decided_at: null,
  wall_config: null,
} as SlotReaderRow;

assert.deepEqual(
  slotReaderReviewPayload(row, undefined, { 'piece_widths:0': '21 in' }),
  { measurements: { 'piece_widths:0': '21 in' } },
  'typing a width must not silently confirm the preselected AI wall proposal',
);
assert.deepEqual(
  slotReaderReviewPayload(row, 'back_left_right', { 'piece_widths:0': '21 in' }),
  { wall_config: 'back_left_right', measurements: { 'piece_widths:0': '21 in' } },
  'the selected wall is sent only after the reviewer changes the control',
);
assert.equal(
  shouldOfferRowWallControl({ ...row, wall_source: 'drawing-and-readers' }),
  true,
  'mixed drawing/reader walls need a reviewer control',
);
assert.deepEqual(
  slotReaderReviewPayload(
    { ...row, wall_source: 'drawing-and-readers' },
    undefined,
    { 'piece_widths:0': '21 in' },
  ),
  { measurements: { 'piece_widths:0': '21 in' } },
  'typing a width must not confirm the reader half of a mixed-source wall layout',
);
// #1202: only code's wall ink at both ends with both AIs needs no control; an older run's
// code-alone layout is now a proposal like any other; walls that change nothing are not asked.
assert.equal(
  shouldOfferRowWallControl({ ...row, wall_source: 'code-ink-and-both-ais' }),
  true,
  'walls set by code ink and both AIs can always be corrected by the reviewer',
);
assert.equal(
  shouldOfferRowWallControl({ ...row, wall_source: 'vendor-drawing-clues' }),
  true,
  'an older run\'s code-alone walls now need the reviewer',
);
assert.equal(
  shouldOfferRowWallControl({ ...row, walls_needed: false }),
  false,
  'a width wrong for every wall case asks no wall question',
);
assert.match(readFileSync('src/components/measure/SlotReaderRows.tsx', 'utf8'), /WallChoiceResult/, 'each wall choice shows its recorded result');
const component = readFileSync('src/components/measure/SlotReaderRows.tsx', 'utf8');
assert.match(component, /slotReaderReviewPayload/, 'the component must send only explicit reviewer choices');
assert.match(component, /shouldOfferRowWallControl/, 'mixed-source rows must show the wall control');

const betweenPanels = {
  ...row,
  held_reason: 'the stone stops at fillers or panels',
  wall_source: 'between-panels',
  wall_proposal: 'back_only',
  wall_confirmation_allowed: true,
};
assert.equal(shouldOfferRowWallControl(betweenPanels), true);
assert.deepEqual(
  slotReaderReviewPayload(betweenPanels, 'back_only', { 'piece_widths:0': '21 in' }),
  { measurements: { 'piece_widths:0': '21 in' } },
  'the between-panels selection is sent only by the wall confirmation button, never a width save',
);
assert.deepEqual(
  slotReaderReviewPayload(betweenPanels, undefined, undefined), {},
  'rendering the preselected proposal sends nothing',
);
assert.match(
  component,
  /onClick=\{\(\) => void save\(row, \{ wall_config: selectedWall \}\)\}/,
  'the wall button, not Save this row, submits the explicit wall-only decision',
);

console.log('slot reader row review: no implicit AI wall confirmation and mixed-source control');
