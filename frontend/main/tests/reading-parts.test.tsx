import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import { ReadingPartsList } from '../src/components/measure/ReadingPartsList.js';
import { GV_MARKS_WARNING } from '../src/components/measure/drawingPartChoices.js';
import {
  isTheSuggestion,
  linkLabel,
  partsStillToLink,
  readingChoiceLabel,
  startingReading,
  type LinkDrawing,
  type LinkPart,
  type LinkReading,
  type ReadingLinks,
} from '../src/components/measure/readingPartChoices.js';

// One vendor's drawing as `GET …/reading-parts` lists it (#913): a cabinet (part 1) whose own
// dimension line carries an 18 in reading, and a filler (part 2) that no confirmed reading spans.
// Nothing is linked yet. Every value is invented.
const said = 'Its dimension line runs across the page from 0.2500 to 0.5000, within the stated tolerance.';
const eighteen: LinkReading = {
  reading_id: 'r-18',
  value: '18 in',
  semantic_type: 'cabinet_width',
  placed_by: 'line',
  left: '0.25',
  right: '0.5',
  unplaced: null,
  linked_to: null,
  linked_to_number: null,
};
const two: LinkReading = {
  ...eighteen,
  reading_id: 'r-2',
  value: '2 in',
  semantic_type: 'filler_width',
  placed_by: 'region',
  left: '0.51',
  right: '0.515',
};

const cabinet: LinkPart = {
  item_id: 'cabinet',
  number: 1,
  proposal_id: 'p-cabinet',
  has_picture: true,
  // Its picture shows GV's own coloured marks (#921).
  picture_gv_marks: 'shown',
  kind: 'cabinet',
  code: null,
  left: '0.25',
  right: '0.5',
  suggestion: { reading_id: 'r-18', said, spanning: ['r-18'], edge_tolerance: '0.004' },
  links: [],
};
const filler: LinkPart = {
  item_id: 'filler',
  number: 2,
  proposal_id: 'p-filler',
  has_picture: false,
  picture_gv_marks: null,
  kind: 'filler',
  code: null,
  left: '0.5',
  right: '0.53',
  suggestion: {
    reading_id: null,
    said: 'No confirmed reading on this drawing has a dimension line whose ends lie within 0.004.',
    spanning: [],
    edge_tolerance: '0.004',
  },
  links: [],
};

const drawing: LinkDrawing = {
  view_id: 'v-shop',
  page_index: 1,
  tag: 'panel-3',
  can_confirm: true,
  why_not: null,
  parts: [cabinet, filler],
  readings: [eighteen, two],
};

const links: ReadingLinks = { can_suggest: true, why_not: null, drawings: [drawing] };

const pictured: string[] = [];
const html = renderToStaticMarkup(
  <ReadingPartsList
    links={links}
    saving={null}
    renderPicture={(_drawing, shown) => {
      pictured.push(shown.item_id);
      return <img className="test-picture" data-part={shown.proposal_id ?? ''} alt="" />;
    }}
    onConfirm={() => undefined}
    onWithdraw={() => undefined}
  />,
);

// **Each confirmed part shows its picture (#897)**, the one cut for the suggestion it was confirmed
// from; a part with none says so rather than showing another.
assert.deepEqual(pictured, ['cabinet']);
assert.match(html, /data-part="p-cabinet"/);
assert.equal((html.match(/No picture of this part is stored yet\./g) ?? []).length, 1);

// **A picture that shows GV's own coloured marks says so under it (#921)**, as in "Parts of each
// drawing"; the part with no picture says nothing about marks.
const warning = GV_MARKS_WARNING.replaceAll("'", '&#x27;');
assert.equal(html.split(warning).length - 1, 1);
assert.ok(html.indexOf('data-part="p-cabinet"') < html.indexOf(warning));
assert.ok(html.indexOf(warning) < html.indexOf('Part 1 (cabinet)'));
for (const marks of ['not_shown', 'not_checked'] as const) {
  const quiet = renderToStaticMarkup(
    <ReadingPartsList
      links={{ ...links, drawings: [{ ...drawing, parts: [{ ...cabinet, picture_gv_marks: marks }, filler] }] }}
      saving={null}
      renderPicture={(_drawing, shown) => <img data-part={shown.proposal_id ?? ''} alt="" />}
      onConfirm={() => undefined}
      onWithdraw={() => undefined}
    />,
  );
  assert.match(quiet, /data-part="p-cabinet"/);
  assert.doesNotMatch(quiet, /coloured marks/, marks);
}

// The question is asked, and how much is left is stated.
assert.match(html, /Which reading is each part&#x27;s width/);
assert.match(html, /Page 2: the vendor&#x27;s drawing/);
assert.match(html, /2 still without a reading\./);
assert.equal(partsStillToLink(links), 2);

// The suggestion is shown beside its part with its value, what it was confirmed as, and why; the
// part nothing spans says so.
assert.match(html, /Part 1 \(cabinet\)/);
assert.match(html, /Suggested width: 18 in, confirmed as cabinet width\./);
assert.match(html, /Its dimension line runs across the page/);
assert.match(html, /No reading is suggested\./);
assert.match(html, /No confirmed reading on this drawing has a dimension line/);
assert.equal((html.match(/No reading is linked to this part yet\./g) ?? []).length, 2);

// **The suggestion is picked for the person to keep or change, never sent without their click.**
assert.equal(startingReading(cabinet), 'r-18');
assert.equal(startingReading(filler), '');
assert.equal((html.match(/<option value="r-18" selected="">/g) ?? []).length, 1);
assert.match(html, />Confirm the suggested reading<\/button>/);
assert.match(html, />Confirm the picked reading<\/button>/);

// **There is no "confirm all".** Two buttons per part, both about that part.
assert.doesNotMatch(html, /confirm all|accept all|confirm every/i);
assert.equal((html.match(/<button/g) ?? []).length, 4);
assert.equal((html.match(/role="group"/g) ?? []).length, 2);
// Nothing to take back yet, and the filler with nothing picked cannot be confirmed.
assert.equal((html.match(/disabled=""[^>]*>Take the link back/g) ?? []).length, 2);
assert.match(html, /disabled=""[^>]*>Confirm the picked reading/);

// Picking another reading makes it a correction.
assert.equal(isTheSuggestion(cabinet, 'r-18'), true);
assert.equal(isTheSuggestion(cabinet, 'r-2'), false);
assert.equal(isTheSuggestion(filler, ''), false);

// A link a person confirmed starts the pick from it, is said in a sentence naming who and which
// reading, and a reading linked to another part says that picking it moves it.
const linkedCabinet: LinkPart = {
  ...cabinet,
  links: [
    {
      reading_id: 'r-2',
      decided_by: 'reviewer@example.com',
      decided_at: '2026-10-04T12:00:00Z',
      signal: 'Picked by a person.',
      read: true,
      why_not_read: null,
    },
  ],
};
const linkedDrawing: LinkDrawing = {
  ...drawing,
  parts: [linkedCabinet, filler],
  readings: [eighteen, { ...two, linked_to: 'cabinet', linked_to_number: 1 }],
};
assert.equal(startingReading(linkedCabinet), 'r-2');
assert.equal(
  linkLabel(linkedDrawing, linkedCabinet),
  'Linked by reviewer@example.com: 2 in, confirmed as filler width.',
);
assert.equal(
  readingChoiceLabel(linkedDrawing, filler, linkedDrawing.readings[1]),
  '2 in, confirmed as filler width (now the width of part 1 (cabinet); picking it moves it here)',
);
assert.equal(
  readingChoiceLabel(linkedDrawing, linkedCabinet, linkedDrawing.readings[1]),
  '2 in, confirmed as filler width',
);
assert.equal(partsStillToLink({ ...links, drawings: [linkedDrawing] }), 1);

// A link no longer read says why, and starts the pick from the suggestion instead.
const notRead: LinkPart = {
  ...linkedCabinet,
  links: [{ ...linkedCabinet.links[0], read: false, why_not_read: 'The reading was corrected in review.' }],
};
assert.equal(startingReading(notRead), 'r-18');
const notReadHtml = renderToStaticMarkup(
  <ReadingPartsList
    links={{ ...links, drawings: [{ ...linkedDrawing, parts: [notRead, filler] }] }}
    saving={null}
    renderPicture={() => <img alt="" />}
    onConfirm={() => undefined}
    onWithdraw={() => undefined}
  />,
);
assert.match(notReadHtml, /role="status">The reading was corrected in review\./);

// On a drawing no longer the vendor's, a link made there before can be taken back, but nothing can
// be picked or confirmed, and the page says why; with no tolerance stated, nothing is suggested and
// the page says why.
const closed = renderToStaticMarkup(
  <ReadingPartsList
    links={{
      can_suggest: false,
      why_not: 'No reading can be suggested yet: GV_RUN_EDGE_TOLERANCE has not been set.',
      drawings: [
        {
          ...drawing,
          can_confirm: false,
          why_not: 'This drawing is no longer confirmed as the vendor’s.',
          parts: [{ ...linkedCabinet, suggestion: null }],
        },
      ],
    }}
    saving={null}
    renderPicture={() => <img alt="" />}
    onConfirm={() => undefined}
    onWithdraw={() => undefined}
  />,
);
assert.match(closed, /GV_RUN_EDGE_TOLERANCE has not been set/);
assert.match(closed, /no longer confirmed as the vendor/);
assert.doesNotMatch(closed, /Suggested width/);
assert.match(closed, /<select disabled="">/);
assert.match(closed, /disabled=""[^>]*>Confirm the picked reading/);
assert.doesNotMatch(closed, /disabled=""[^>]*>Take the link back/);
assert.match(closed, /Nothing left to link\./);

console.log('reading-parts: ok');
