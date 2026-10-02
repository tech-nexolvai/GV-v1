import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { MeasurementGuidance, StoredProposalGuidance } from '../src/pages/MeasurementGuidance.js';
import { MeasurementSectionNav } from '../src/pages/MeasurementSectionNav.js';
import { jumpToMeasurementSection, measurementSections } from '../src/pages/measurementNavigation.js';

const header = renderToStaticMarkup(<MeasurementGuidance rulesPublished={7} />);
assert.match(header, /Review measurements/);
assert.match(header, /Check suggested values against their drawing crops/);
assert.match(header, /Values without units are refused/);
assert.match(header, /<details class="measure-guidance"><summary>How these values are used/);
assert.doesNotMatch(header, /<details[^>]* open/);
assert.match(header, /The 7 published rules/);
assert.match(header, /saves first and queues the checks only if saving succeeds/);
assert.match(header, /Missing or uncertain inputs/);

for (const count of [1, 3]) {
  for (const unverifiedCount of [0, count]) {
    const html = renderToStaticMarkup(<StoredProposalGuidance count={count} unverifiedCount={unverifiedCount} />);
    assert.match(html, new RegExp(`${count} field${count === 1 ? '' : 's'} below ${count === 1 ? 'has' : 'have'} an AI suggestion`));
    assert.match(html, /A suggestion is not a confirmed measurement/);
    assert.doesNotMatch(html, /every one passed|scanned images|no dimension line-work/,
      'do not infer a cause or universal placement verification from a boolean');
    if (unverifiedCount > 0) {
      assert.match(html, new RegExp(`Placement is unverified for ${unverifiedCount} field`));
      assert.match(html, /Inspect the crop before keeping the suggestion/);
      assert.doesNotMatch(html, /<details/, 'placement warning must remain visible');
    } else {
      assert.doesNotMatch(html, /Placement is unverified/);
    }
  }
}

const panel = readFileSync('src/pages/MeasurementPanel.tsx', 'utf8');
assert.match(panel, /aria-label="Measurement fields with a value, not reading accuracy"/);
assert.match(panel, /Form coverage, not accuracy/);
assert.match(panel, /<StoredProposalGuidance count=\{storedProposalCount\} unverifiedCount=\{unverifiedFields.size\}/);
assert.match(panel, /if \(!\(await saveVisibleValues\(\)\)\) return;/, 'saving still gates checks');
assert.match(panel, /disabled=\{busy \|\| candidates.length === 0\}/, 'no AI request without readings');
assert.match(panel, /disabled=\{isConfirming \|\| !candidate.crop_key \|\| availableTypes.length === 0\}/,
  'confirmation still requires a crop and an available meaning');
assert.doesNotMatch(panel, /Nothing was read here that could fill it/, 'an empty field does not imply no raw readings');
const sources = [{ key: 'SHOP', label: 'shop drawing' }, { key: 'ARCH', label: 'architectural drawing' }];
const before = JSON.stringify(sources);
const sections = measurementSections(sources, false, false);
assert.deepEqual(sections.map((section) => section.key), ['overview', 'source:SHOP', 'source:ARCH', 'settings', 'distribution', 'actions']);
assert.equal(JSON.stringify(sources), before);
assert.deepEqual(measurementSections([], true, true).map((section) => section.key), ['overview', 'settings', 'distribution', 'layout', 'stored', 'actions']);
const navigation = renderToStaticMarkup(<MeasurementSectionNav sections={sections} onJump={() => { throw new Error('render must not navigate'); }} />);
assert.match(navigation, /aria-label="Measurement sections"/);
assert.match(navigation, /Jump to/);
assert.match(navigation, /type="button"/);
assert.doesNotMatch(navigation, /href=|type="submit"/);
const unknownSource = renderToStaticMarkup(<MeasurementSectionNav sections={measurementSections([{ key: 'new/source', label: '<new source>' }], false, false)} onJump={() => undefined} />);
assert.match(unknownSource, /&lt;new source&gt;/, 'unknown source labels remain visible and escaped');
const effects: unknown[] = [];
const target = { dataset: { measureSection: 'settings' }, getBoundingClientRect: () => ({ top: 500 }), focus: (options: unknown) => effects.push(options) };
const container = {
  scrollTop: 200,
  querySelectorAll: () => [target],
  querySelector: () => ({ offsetHeight: 60 }),
  getBoundingClientRect: () => ({ top: 100 }),
  scrollTo: (options: unknown) => effects.push(options),
} as unknown as HTMLElement;
jumpToMeasurementSection(null, 'settings');
jumpToMeasurementSection(container, 'unknown');
assert.deepEqual(effects, [], 'missing destinations are a safe no-op');
jumpToMeasurementSection(container, 'settings');
assert.deepEqual(effects, [{ preventScroll: true }, { top: 540, behavior: 'instant' }], 'only focus and local scroll, no animated movement');
effects.length = 0;
const paddedContainer = { ...container, ownerDocument: { defaultView: { getComputedStyle: () => ({ paddingTop: '20px', marginBottom: '16px' }) } } } as unknown as HTMLElement;
jumpToMeasurementSection(paddedContainer, 'settings');
assert.deepEqual(effects, [{ preventScroll: true }, { top: 504, behavior: 'instant' }], 'heading clears sticky nav including padding and gap');
assert.match(panel, /ref=\{formContainer\}/);
for (const key of ['overview', 'settings', 'layout', 'stored', 'actions']) {
  assert.ok(panel.includes(`data-measure-section="${key}"`));
}
const css = readFileSync('src/pages/MeasurementPanel.css', 'utf8');
assert.match(css, /\.sheet-group__head\s*\{[^}]*position: static/s, 'section headings must not compete with sticky navigation');
console.log('measurement guidance: concise instructions, honest proposal/placement states, collapsed detail, accessible coverage and existing safety gates passed');
