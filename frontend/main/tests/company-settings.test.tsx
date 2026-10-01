import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import { CompanySettingsList } from '../src/components/settings/CompanySettingsList.js';
import {
  changedValues,
  inUseSentence,
  settingLabel,
  type CompanySetting,
} from '../src/components/settings/companySettings.js';

// Three of the shapes `GET /company-settings` returns (#812): a rulebook default in use, a company
// standard in use, and a number nobody has given.
const settings: CompanySetting[] = [
  {
    name: 'filler_max',
    scope: 'global',
    rule_ids: ['CAB-FILLER-001'],
    rulebook_default: '2 in',
    rulebook_note: '2" is Raj\'s written rule — awaiting his confirmation (#674).',
    company_value: null,
    company_set_by: null,
    company_set_at: null,
    in_use: '2 in',
    in_use_from: 'rulebook',
  },
  {
    name: 'back_offset_minimum',
    scope: 'global',
    rule_ids: ['CT-BACK-OFFSET-MIN-001'],
    rulebook_default: '2 1/2 in',
    rulebook_note: null,
    company_value: '2 3/8 in',
    company_set_by: 'anant',
    company_set_at: '2026-10-02T09:30:00Z',
    in_use: '2 3/8 in',
    in_use_from: 'company',
  },
  {
    name: 'cabinet_depth',
    scope: 'project',
    rule_ids: ['CT-DEPTH-001'],
    rulebook_default: null,
    rulebook_note: null,
    company_value: null,
    company_set_by: null,
    company_set_at: null,
    in_use: null,
    in_use_from: null,
  },
];

const html = renderToStaticMarkup(
  <CompanySettingsList settings={settings} drafts={{}} saving={false} onDraft={() => undefined} />,
);

// One row each, with a readable name and the code beside it, and how many are still missing.
assert.match(html, /Cabinet depth <code>cabinet_depth<\/code>/);
assert.match(html, /1 not set yet\./);

// Where each number in use came from, said in words.
assert.match(html, /2 in — the rulebook&#x27;s default, until GV sets its own\./);
assert.match(html, /2 3\/8 in — GV&#x27;s standard, set by anant on /);
assert.match(html, /Not set — checks that need it say &quot;not found&quot; until it is\./);

// The doubt note shows only while the rulebook default is in use; a company value shows the default
// it replaced instead.
assert.match(html, /awaiting his confirmation \(#674\)/);
assert.match(html, /Rulebook default: 2 1\/2 in/);
assert.equal((html.match(/Rulebook default:/g) ?? []).length, 1);

// Which checks use each one.
assert.match(html, /Used by CT-DEPTH-001/);

// Only typed, non-blank values are sent; a blank box leaves a standard as it was.
assert.deepEqual(changedValues({ cabinet_depth: ' 24" ', filler_max: '', back_offset_minimum: '  ' }), [
  { name: 'cabinet_depth', value: '24"' },
]);

assert.equal(settingLabel('cabinet_side_thickness'), 'Cabinet side thickness');
assert.equal(inUseSentence(settings[2]), 'Not set — checks that need it say "not found" until it is.');

console.log('company-settings: ok');
