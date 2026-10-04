import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import { DrawingPartsList } from '../src/components/measure/DrawingPartsList.js';
import {
  GV_MARKS_WARNING,
  codeToSend,
  decisionLabel,
  endChoices,
  showsGvMarks,
  startingCode,
  stillToDecide,
  type PartDrawing,
  type SuggestedPart,
} from '../src/components/measure/drawingPartChoices.js';

// One vendor's drawing as `GET …/parts` lists it (#882): two cabinets drawn end to end, the first
// under an invented code, and a countertop over both. The drawing is confirmed as the vendor's.
const part = (overrides: Partial<SuggestedPart>): SuggestedPart => ({
  proposal_id: 'p-1',
  view_id: 'v-shop',
  page_index: 2,
  position: 1,
  suggested_kind: 'cabinet',
  suggested_code: null,
  reason: 'One of 2 dimensions drawn end to end along the lowest row.',
  added_by_a_person: false,
  left_end: { x: '0.25', y: '0.8' },
  right_end: { x: '0.5', y: '0.8' },
  has_picture: false,
  picture_gv_marks: null,
  has_crop: false,
  decision: null,
  ...overrides,
});

// The first cabinet and the countertop have their own pictures (#897); the first cabinet's code also
// has the crop of the reading it came from. The worker has not cut the second cabinet's yet. The
// countertop's picture shows GV's own coloured marks, and the first cabinet's was checked and does
// not (#921).
const parts: SuggestedPart[] = [
  part({
    proposal_id: 'p-1',
    position: 1,
    suggested_code: 'XQ24',
    has_picture: true,
    picture_gv_marks: 'not_shown',
    has_crop: true,
  }),
  part({
    proposal_id: 'p-top',
    position: 2,
    has_picture: true,
    picture_gv_marks: 'shown',
    suggested_kind: 'countertop',
    left_end: { x: '0.25', y: '0.6' },
    right_end: { x: '0.75', y: '0.6' },
  }),
  part({
    proposal_id: 'p-2',
    position: 3,
    left_end: { x: '0.5', y: '0.8' },
    right_end: { x: '0.75', y: '0.8' },
  }),
];

const vendors: PartDrawing = {
  view_id: 'v-shop',
  page_index: 2,
  tag: 'panel-1',
  role: 'shop',
  can_confirm: true,
  why_not: null,
  parts,
};

const pictures: string[] = [];
const crops: string[] = [];
const html = renderToStaticMarkup(
  <DrawingPartsList
    drawings={[vendors]}
    saving={null}
    renderPicture={(_drawing, shown) => {
      pictures.push(shown.proposal_id);
      return <img className="test-picture" data-picture={shown.proposal_id} alt="" />;
    }}
    renderCrop={(_drawing, shown) => {
      crops.push(shown.proposal_id);
      return <img className="test-crop" data-part={shown.proposal_id} alt="" />;
    }}
    onConfirm={() => undefined}
    onWithdraw={() => undefined}
    onAdd={() => undefined}
  />,
);

// The question is asked, about a drawing named as a person would name it, and how much is left is
// stated.
assert.match(html, /Parts of each drawing/);
assert.match(html, /Page 3: the vendor&#x27;s drawing/);
assert.match(html, /3 still to decide\./);
assert.equal(stillToDecide([vendors]), 3);

// **Each suggestion is shown with its own picture (#897).** Coded or not, a part with a picture shows
// it; the one the worker has not cut yet says there is none and where to look, rather than showing
// some other region. The coded part also shows where its code is printed.
assert.deepEqual(pictures, ['p-1', 'p-top']);
assert.match(html, /data-picture="p-1"/);
assert.match(html, /data-picture="p-top"/);
assert.equal((html.match(/No picture of this part is stored yet\./g) ?? []).length, 1);
assert.match(html, /Find it on page 3: it is number 3 from the left in this drawing\./);
assert.deepEqual(crops, ['p-1']);
assert.match(html, /data-part="p-1"/);
assert.equal((html.match(/Where its code is printed/g) ?? []).length, 1);
assert.ok(html.indexOf('data-picture="p-1"') < html.indexOf('data-part="p-1"'));

// **A picture that shows GV's own coloured marks says so, plainly, under that picture (#921)**, and
// nowhere else: not under a picture checked and found without them, nor where there is no picture.
const warning = GV_MARKS_WARNING.replaceAll("'", '&#x27;');
const rowOf = (markup: string, proposalId: string): string => {
  const start = markup.indexOf(`data-picture="${proposalId}"`);
  const end = markup.indexOf('class="drawing-parts__item"', start);
  return markup.slice(start, end === -1 ? undefined : end);
};
assert.equal(GV_MARKS_WARNING, "This picture shows GV's own coloured marks; check the vendor's drawing itself.");
assert.equal(html.split(warning).length - 1, 1);
assert.ok(rowOf(html, 'p-top').includes(warning));
assert.ok(!rowOf(html, 'p-1').includes(warning));
assert.match(html, /<p class="drawing-parts__gv-marks" role="note">/);
assert.ok(html.indexOf('data-picture="p-top"') < html.indexOf(warning));
assert.ok(html.indexOf(warning) < html.indexOf('2. Suggested as a countertop'));

// A picture never checked, cut before the check existed, carries no warning: nothing is guessed. Nor
// does a part with no picture, whatever it says.
const unchecked = renderToStaticMarkup(
  <DrawingPartsList
    drawings={[
      {
        ...vendors,
        parts: [
          part({ proposal_id: 'p-old', has_picture: true, picture_gv_marks: 'not_checked' }),
          part({ proposal_id: 'p-none', position: 2, has_picture: false, picture_gv_marks: 'shown' }),
        ],
      },
    ]}
    saving={null}
    renderPicture={(_drawing, shown) => <img data-picture={shown.proposal_id} alt="" />}
    renderCrop={() => <img alt="" />}
    onConfirm={() => undefined}
    onWithdraw={() => undefined}
    onAdd={() => undefined}
  />,
);
assert.match(unchecked, /data-picture="p-old"/);
assert.doesNotMatch(unchecked, /coloured marks/);
assert.equal(showsGvMarks('shown'), true);
for (const marks of ['not_shown', 'not_checked', null, undefined] as const) {
  assert.equal(showsGvMarks(marks), false, String(marks));
}

// Each suggestion is listed once, left to right, with why it was suggested and the code read on it.
assert.equal((html.match(/class="drawing-parts__item"/g) ?? []).length, 3);
assert.ok(html.indexOf('1. Suggested as a cabinet') < html.indexOf('2. Suggested as a countertop'));
assert.ok(html.indexOf('2. Suggested as a countertop') < html.indexOf('3. Suggested as a cabinet'));
assert.match(html, /Code read on the drawing: “XQ24”/);
assert.match(html, /value="XQ24"/);

// **There is no "confirm all".** Every decision button belongs to one part: three kinds and "Not a
// part" on each, and the only other button adds one part.
assert.doesNotMatch(html, /confirm all|accept all|confirm every/i);
for (const label of ['Cabinet', 'Filler', 'Countertop', 'Not a part']) {
  assert.equal((html.match(new RegExp(`>${label}</button>`, 'g')) ?? []).length, 3, label);
}
assert.equal((html.match(/<button/g) ?? []).length, 3 * 4 + 1);
assert.equal((html.match(/role="group"/g) ?? []).length, 3);
// Adding waits until a person has picked both ends.
assert.match(html, /disabled=""[^>]*>Add this part<\/button>/);

// Nothing is pressed until a person presses it.
assert.doesNotMatch(html, /aria-pressed="true"/);

// A decision is shown as the pressed answer, with the code exactly as it was kept.
const decided = renderToStaticMarkup(
  <DrawingPartsList
    drawings={[
      {
        ...vendors,
        parts: [
          part({
            ...parts[0],
            decision: {
              decision: 'confirmed',
              kind: 'filler',
              code: ' xq-24/B ',
              decided_by: 'reviewer@example.com',
              decided_at: '2026-10-03T10:00:00Z',
            },
          }),
          parts[1],
          part({
            ...parts[2],
            decision: {
              decision: 'withdrawn',
              kind: null,
              code: null,
              decided_by: 'reviewer@example.com',
              decided_at: '2026-10-03T10:01:00Z',
            },
          }),
        ],
      },
    ]}
    saving="p-top"
    renderPicture={() => <img alt="" />}
    renderCrop={() => <img alt="" />}
    onConfirm={() => undefined}
    onWithdraw={() => undefined}
    onAdd={() => undefined}
  />,
);
assert.match(decided, /1 still to decide\./);
assert.equal((decided.match(/aria-pressed="true"/g) ?? []).length, 2);
assert.match(decided, /aria-pressed="true"[^>]*>Filler<\/button>/);
assert.match(decided, /aria-pressed="true"[^>]*>Not a part<\/button>/);
assert.match(decided, /Confirmed as a filler, code “ xq-24\/B ”, by reviewer@example\.com\./);
assert.match(decided, /value=" xq-24\/B "/);
// The part being saved cannot be decided twice: its four buttons and its code box are disabled,
// and nothing else is but the add button, which waits for two ends.
assert.equal((decided.match(/disabled=""/g) ?? []).length, 5 + 1);
assert.equal(
  (decided.match(/disabled=""[^>]*>(Cabinet|Filler|Countertop|Not a part)<\/button>/g) ?? []).length,
  4,
);

// On a drawing nobody has confirmed as the vendor's, the note says what to do first, no part can be
// confirmed or added, and saying one is not a part is still offered.
const unconfirmed = renderToStaticMarkup(
  <DrawingPartsList
    drawings={[
      {
        ...vendors,
        role: null,
        can_confirm: false,
        why_not: "Nobody has confirmed whose drawing this is yet. Confirm it is the vendor's drawing first.",
      },
    ]}
    saving={null}
    renderPicture={() => <img alt="" />}
    renderCrop={() => <img alt="" />}
    onConfirm={() => undefined}
    onWithdraw={() => undefined}
    onAdd={() => undefined}
  />,
);
assert.match(unconfirmed, /Page 3: a drawing nobody has confirmed yet/);
assert.match(unconfirmed, /Confirm it is the vendor&#x27;s drawing first\./);
assert.match(unconfirmed, /Nothing left to decide\./);
assert.doesNotMatch(unconfirmed, /Add this part/);
assert.equal((unconfirmed.match(/disabled=""[^>]*>(Cabinet|Filler|Countertop)<\/button>/g) ?? []).length, 9);
assert.doesNotMatch(unconfirmed, /disabled=""[^>]*>Not a part<\/button>/);

// The ends a person can add a part between: each listed part's two ends, in the list's order, with
// an end two parts share offered once. The countertop's ends are drawn higher than the cabinets', so
// they are ends of their own: points are compared as the exact text the server sent.
const ends = endChoices(vendors);
assert.deepEqual(
  ends.map((end) => end.label),
  [
    'left end of 1',
    'right end of 1, left end of 3',
    'left end of 2',
    'right end of 2',
    'right end of 3',
  ],
);
assert.deepEqual(ends[1].point, { x: '0.5', y: '0.8' });

// The code box starts from the code read, and is sent exactly as typed or as no code at all.
assert.equal(startingCode(parts[0]), 'XQ24');
assert.equal(startingCode(parts[1]), '');
assert.equal(codeToSend(' B24 '), ' B24 ');
assert.equal(codeToSend(''), null);
assert.equal(decisionLabel(parts[1]), 'Not decided yet.');

console.log('drawing-parts: ok');
