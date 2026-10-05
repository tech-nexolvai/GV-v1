import assert from 'node:assert/strict';

import { candidateCropWarning } from '../src/components/measure/candidateCropWarning.js';

assert.equal(
  candidateCropWarning(true),
  "This picture shows GV's own coloured marks; check the vendor's drawing itself.",
);
assert.equal(candidateCropWarning(null), 'This picture has not been checked for GV marks.');
assert.equal(candidateCropWarning(false), null);

console.log('candidate crop warning states test passed');
