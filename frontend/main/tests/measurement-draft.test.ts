import assert from 'node:assert/strict';
import { measurementValueOrigin, prefillReadingValues } from '../src/pages/measurementDraft.js';

const originInput = {
  hasValue: true, humanEdited: false, proposed: false, placementUnverified: false,
  qualifications: ['reviewer_confirmed'],
};
assert.equal(measurementValueOrigin(originInput), 'confirmed');
assert.equal(measurementValueOrigin({ ...originInput, qualifications: ['exact_vector_tag'] }), 'tagged');
assert.equal(measurementValueOrigin({ ...originInput, qualifications: [] }), 'typed');
assert.equal(measurementValueOrigin({ ...originInput, proposed: true }), 'proposed');
assert.equal(measurementValueOrigin({ ...originInput, proposed: true, placementUnverified: true }), 'unplaced');
// Editing either a scalar or a run cannot inherit a historical observation's qualification.
for (const qualifications of [[], ['reviewer_confirmed'], ['exact_vector_tag']]) {
  for (const proposed of [false, true]) {
    for (const placementUnverified of [false, true]) {
      const edited = { ...originInput, humanEdited: true, qualifications, proposed, placementUnverified };
      assert.equal(measurementValueOrigin(edited), 'typed');
      assert.equal(measurementValueOrigin({ ...edited, hasValue: false }), 'empty');
    }
  }
}
assert.deepEqual(originInput.qualifications, ['reviewer_confirmed'], 'labels never change backend qualification');

// Later extraction results must not overwrite typed dimensions or restore deliberately cleared fields.
assert.deepEqual(prefillReadingValues(['25 1/4"'], true, ['25"'], ['26"'], false),
  { values: ['25 1/4"'], origin: 'unchanged' });
assert.deepEqual(prefillReadingValues([''], true, ['25"'], ['26"'], false),
  { values: [''], origin: 'unchanged' });
assert.deepEqual(prefillReadingValues(['', ''], true, ['15"', '36"'], ['18"'], true),
  { values: ['', ''], origin: 'unchanged' });
// Repeated widths are different run positions, never de-duplicated or reordered.
assert.deepEqual(prefillReadingValues([''], false, ['18"', '18"', '36"'], [], true),
  { values: ['18"', '18"', '36"'], origin: 'confirmed' });
assert.deepEqual(prefillReadingValues([''], false, ['25"'], ['26"'], false),
  { values: ['25"'], origin: 'confirmed' });
// Conflicting scalar confirmations cannot be silently resolved by a proposal.
assert.deepEqual(prefillReadingValues([''], false, ['25"', '26"'], ['25"'], false),
  { values: [''], origin: 'confirmed' });
assert.deepEqual(prefillReadingValues([''], false, [], ['25"'], false),
  { values: ['25"'], origin: 'proposed' });
assert.deepEqual(prefillReadingValues([''], false, [], ['25"', '26"'], false),
  { values: [''], origin: 'unchanged' });
assert.deepEqual(prefillReadingValues(['3"', '5"'], false, ['4"', '4"'], [], true),
  { values: ['3"', '5"'], origin: 'unchanged' });
console.log('measurement drafts: polling preserves edits, blanks, run order, and confirmation provenance');
