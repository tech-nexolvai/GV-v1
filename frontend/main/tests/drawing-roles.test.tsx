import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import { DrawingRolesList } from '../src/components/measure/DrawingRolesList.js';
import { roleLabel, stillToConfirm, type DrawingView } from '../src/components/measure/drawingRoleChoices.js';

// Two drawings on one sheet, as `GET …/views` lists them: neither confirmed, both suggested by the
// heading the sheet prints above them (#710).
const unconfirmed: DrawingView[] = [
  {
    view_id: 'v-arch',
    page_index: 2,
    tag: 'panel-0',
    role: null,
    suggested_role: 'arch',
    suggested_from: 'ID SET ELEVATION ',
    reason: 'the heading above it',
  },
  {
    view_id: 'v-shop',
    page_index: 2,
    tag: 'panel-1',
    role: null,
    suggested_role: 'shop',
    suggested_from: "VENDOR'S SHOP DRAWING ELEVATION",
    reason: 'the heading above it',
  },
];

const html = renderToStaticMarkup(
  <DrawingRolesList views={unconfirmed} saving={null} onChoose={() => undefined} />,
);

// The question is asked, and how much is left is stated.
assert.match(html, /Which drawing is which\?/);
assert.match(html, /2 still to confirm\./);
assert.equal(stillToConfirm(unconfirmed), 2);

// **The suggestion is shown, never applied.** Nothing is pressed until a reviewer presses it.
assert.match(html, /the label suggests: Architect&#x27;s drawing/);
assert.match(html, /labelled “ID SET ELEVATION”/);
assert.doesNotMatch(html, /aria-pressed="true"/);

// Each drawing offers both answers.
assert.equal((html.match(/Architect&#x27;s drawing<\/button>/g) ?? []).length, 2);
assert.equal((html.match(/Vendor&#x27;s drawing<\/button>/g) ?? []).length, 2);

// Once a person has answered, that answer is the pressed one and the suggestion is not repeated.
const confirmed = renderToStaticMarkup(
  <DrawingRolesList
    views={[{ ...unconfirmed[0], role: 'arch' }, unconfirmed[1]]}
    saving="v-shop"
    onChoose={() => undefined}
  />,
);
assert.match(confirmed, /1 still to confirm\./);
assert.equal((confirmed.match(/aria-pressed="true"/g) ?? []).length, 1);
assert.equal((confirmed.match(/the label suggests/g) ?? []).length, 1);
// The drawing being saved cannot be answered twice.
assert.equal((confirmed.match(/disabled=""/g) ?? []).length, 2);

// A two-PDF package's one drawing per page takes the upload's side: shown, and not asked about.
const fromUpload = renderToStaticMarkup(
  <DrawingRolesList
    views={[{ ...unconfirmed[1], upload_side: 'shop' }]}
    saving={null}
    onChoose={() => undefined}
  />,
);
assert.match(fromUpload, /from the upload: Vendor&#x27;s drawing/);
assert.match(fromUpload, /All confirmed\./);
assert.doesNotMatch(fromUpload, /the label suggests/);

assert.equal(roleLabel('arch'), "Architect's drawing");
assert.equal(roleLabel(null), null);

console.log('drawing-roles: ok');
