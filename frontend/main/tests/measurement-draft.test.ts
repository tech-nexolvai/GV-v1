import assert from 'node:assert/strict';
import { prefillReadingValues } from '../src/pages/measurementDraft.js';

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
