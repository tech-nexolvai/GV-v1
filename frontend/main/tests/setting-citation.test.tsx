import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import type { components } from '../src/api/schema';
import { SettingCitation } from '../src/components/measure/SettingCitation.js';
import {
  citingPointer,
  settingEntry,
  uploadLabel,
  type CitableSetting,
  type SettingPointer,
} from '../src/components/measure/settingPointers.js';

// **Blind entry (#866): the proposed value is never shown before the person types.**

// The schema first, at compile time. The pointer the server sends has exactly these four fields; a
// field added to `SettingPointerOut` (a value, its text, the runs' ids) stops this file compiling, so
// `npm run test:components` fails before anything renders it.
const pointerFields: Record<keyof components['schemas']['SettingPointerOut'], true> = {
  proposal_id: true,
  page_index: true,
  document_kind: true,
  has_crop: true,
};
assert.deepEqual(Object.keys(pointerFields).sort(), [
  'document_kind',
  'has_crop',
  'page_index',
  'proposal_id',
]);

const pointer: SettingPointer = {
  proposal_id: 'p-overhang',
  page_index: 2,
  document_kind: 'shop',
  has_crop: true,
};

// A pointer that smuggles the number in, as a server that leaked it would send it. The box reads
// only the four fields it is typed with, so none of this may reach the page.
const leaky = {
  ...pointer,
  value: '1 7/16 in',
  numerator: '23',
  denominator: '16',
  raw_text: 'OVERHANG 1 7/16" TYP.',
} as unknown as SettingPointer;

const render = (value: string, shown: SettingPointer = leaky) =>
  renderToStaticMarkup(
    <SettingCitation
      name="countertop_overhang"
      pointer={shown}
      value={value}
      crop={<img alt="the passage" src="blob:crop" />}
      onChange={() => undefined}
      onDecline={() => undefined}
    />,
  );

const before = render('');

// Where to look: the page, the file, and the crop.
assert.match(before, /Found in the architect&#x27;s drawing, page 3: type the value you see/);
assert.match(before, /Page 3 of the vendor&#x27;s file/);
assert.match(before, /src="blob:crop"/);

// Never the number: not as a value, a fraction, its parts or the passage's words. The box is empty,
// and its placeholder holds no digit a person could take for a suggestion.
for (const trace of ['7/16', '23/16', '1.4375', 'OVERHANG', 'TYP']) {
  assert.doesNotMatch(before, new RegExp(trace.replace('.', '\\.')), trace);
}
assert.match(before, /<input[^>]*value=""/);
const placeholder = /placeholder="([^"]*)"/.exec(before)?.[1] ?? '';
assert.doesNotMatch(placeholder, /[0-9]/);
assert.match(before, /The number the app read there is not shown/);

// What the person types is theirs, and is shown back to them.
assert.match(render('1 1/2"'), /value="1 1\/2&quot;"/);

// No crop was cut: the page is named instead, and the caller's picture is not used.
const noCrop = render('', { ...pointer, has_crop: false });
assert.match(noCrop, /No picture of this passage was cut\. Open page 3 of the vendor&#x27;s file/);
assert.doesNotMatch(noCrop, /blob:crop/);

assert.equal(uploadLabel('architectural'), "architect's file");
assert.equal(uploadLabel('product_spec'), 'product spec file');

// **Submitting it sends the citation**, and nothing about the source: the server writes those from
// the passage. Declined, the setting goes exactly as before #866.
const overhang: CitableSetting = {
  name: 'countertop_overhang',
  scope: 'project',
  sources: [{ value: 'G.C / Client', guidance: 'From the architect.' }],
  found: pointer,
};
assert.deepEqual(
  settingEntry(overhang, ' 1 7/16" ', {
    declined: {},
    source: 'Company standard',
    reference: 'A-501',
  }),
  { name: 'countertop_overhang', value: '1 7/16"', scope: 'project', citation: 'p-overhang' },
);
assert.deepEqual(
  settingEntry(overhang, '1 1/2"', {
    declined: { countertop_overhang: true },
    reference: ' Architect A-501 ',
  }),
  {
    name: 'countertop_overhang',
    value: '1 1/2"',
    scope: 'project',
    source: 'G.C / Client',
    reference: 'Architect A-501',
  },
);
// Nothing found: the free path, unchanged.
assert.equal(citingPointer({ ...overhang, found: null }, {}), null);
assert.deepEqual(settingEntry({ ...overhang, found: null }, '1"', { declined: {} }), {
  name: 'countertop_overhang',
  value: '1"',
  scope: 'project',
  source: 'G.C / Client',
});

console.log('setting-citation: ok');
