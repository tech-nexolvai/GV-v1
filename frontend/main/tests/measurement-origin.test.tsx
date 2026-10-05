import assert from 'node:assert/strict';
import { measurementValueOrigin } from '../src/pages/measurementValueOrigin.js';

const base = { hasValue: true, humanEdited: false, proposed: false, placementUnverified: false, qualifications: ['exact_vector_tag'] };
assert.equal(measurementValueOrigin(base), 'tagged');
assert.equal(measurementValueOrigin({ ...base, humanEdited: true }), 'typed');
assert.equal(measurementValueOrigin({ ...base, hasValue: false, humanEdited: true }), 'empty');
assert.equal(measurementValueOrigin({ ...base, proposed: true, placementUnverified: true }), 'unplaced');
console.log('measurement origin follows the edited field, not stale reading provenance');
