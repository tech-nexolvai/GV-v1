import assert from 'node:assert/strict';

import { packageChanged } from '../src/pages/measureRefreshState.js';

// Drafts owned by the Measure sections must survive every data refresh while the selected package
// stays the same. This exercises the package-reset boundary used by MeasurementPanel, not just the
// individual field controls: clearing the package contract unmounts those controls and loses all of
// these drafts together.
const drafts = {
  halfPlacedPart: { firstEnd: 'snapped-point-awaiting-choice' },
  newPart: { kind: 'countertop', code: 'TOP-A' },
  wallLayout: 'back_left_right',
  widthLink: 'reading-13',
  typedValue: '39 1/2 in',
};

let currentPackage: string | null = 'package-1';
let retainedDrafts: {
  halfPlacedPart: { firstEnd: string; secondEnd: string | null };
  newPart: typeof drafts.newPart;
  wallLayout: string;
  widthLink: string;
  typedValue: string;
} | null = { ...drafts, halfPlacedPart: { firstEnd: drafts.halfPlacedPart.firstEnd, secondEnd: null } };

for (let refresh = 1; refresh <= 3; refresh += 1) {
  const nextPackage = 'package-1';
  if (packageChanged(currentPackage, nextPackage)) retainedDrafts = null;
  currentPackage = nextPackage;
  assert.deepEqual(
    retainedDrafts,
    { ...drafts, halfPlacedPart: { firstEnd: 'snapped-point-awaiting-choice', secondEnd: null } },
    `refresh ${refresh} discarded reviewer input`,
  );
}

// After three refreshes the person can finish the placement and confirm the same draft.
if (retainedDrafts) retainedDrafts.halfPlacedPart.secondEnd = 'second-snapped-point';
assert.deepEqual(retainedDrafts?.halfPlacedPart, {
  firstEnd: 'snapped-point-awaiting-choice',
  secondEnd: 'second-snapped-point',
});
assert.equal(Boolean(retainedDrafts?.halfPlacedPart.firstEnd && retainedDrafts.halfPlacedPart.secondEnd), true);
assert.equal(retainedDrafts?.newPart.kind, 'countertop');
assert.equal(retainedDrafts?.newPart.code, 'TOP-A');
assert.equal(retainedDrafts?.wallLayout, 'back_left_right');
assert.equal(retainedDrafts?.widthLink, 'reading-13');
assert.equal(retainedDrafts?.typedValue, '39 1/2 in');

// A genuine package switch still resets the form; retaining one package's drafts in another would
// risk submitting values against the wrong drawing pair.
assert.equal(packageChanged('package-1', 'package-2'), true);
console.log('Measure refresh draft-retention tests passed');
