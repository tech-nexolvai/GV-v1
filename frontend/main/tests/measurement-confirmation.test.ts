import assert from 'node:assert/strict';
import {
  appendConfirmedRunValue, confirmCandidateOnce, confirmProposalFields, newConfirmationLedger,
} from '../src/pages/measurementConfirmation.js';

// Different candidate identities may have the same numeric width. Both run positions survive.
assert.deepEqual(appendConfirmedRunValue(['18"'], '18"', 'right', ['left']), ['18"', '18"']);
assert.deepEqual(appendConfirmedRunValue(['', '15"', ''], '36"', 'center'), ['15"', '36"']);
assert.deepEqual(appendConfirmedRunValue(['18"', '18"'], '18"', 'right', ['left', 'right']), ['18"', '18"'],
  'confirming a candidate already displayed by its proposal ID does not append it twice');

const ledger = newConfirmationLedger();
const calls: string[] = [];
let rejectSecond = true;
const confirm = async (id: string, semanticType: string) => {
  calls.push(id);
  if (id === 'right' && rejectSecond) throw new Error('409 crop verification failed');
  return { semantic_type: semanticType };
};
const fields = [{ key: 'SHOP:cabinet_width', semanticType: 'cabinet_width', candidateIds: ['left', 'right'] }];
const draft = { values: ['18"', '18"'], candidateIds: ['left', 'right'] };
let downstreamWrites = 0;
await assert.rejects(async () => {
  await confirmProposalFields(fields, ledger, confirm);
  downstreamWrites += 1; // Saving typed operands or running checks must never follow a failure.
}, /Drawing confirmation stopped.*409 crop verification failed.*Save and checks were stopped/);
assert.equal(downstreamWrites, 0);
assert.deepEqual(draft, { values: ['18"', '18"'], candidateIds: ['left', 'right'] });
assert.equal(ledger.confirmed.has('left'), true);
assert.equal(ledger.confirmed.has('right'), false, 'failed/unknown confirmations are never considered saved');
assert.equal(ledger.pending.size, 0);

rejectSecond = false;
assert.deepEqual([...await confirmProposalFields(fields, ledger, confirm)], ['SHOP:cabinet_width']);
assert.deepEqual(calls, ['left', 'right', 'right'], 'retry retains the successful first receipt and retries only the failure');
await confirmProposalFields(fields, ledger, confirm);
assert.deepEqual(calls, ['left', 'right', 'right'], 'subsequent Save/Run must not replay non-idempotent confirmation');
await assert.rejects(confirmCandidateOnce(ledger, 'left', 'filler_width', confirm), /different meaning/);

const concurrent = newConfirmationLedger();
let repeatedCalls = 0;
const oneRequest = async () => { repeatedCalls += 1; return { semantic_type: 'cabinet_width' }; };
await Promise.all([
  confirmCandidateOnce(concurrent, 'same-candidate', 'cabinet_width', oneRequest),
  confirmCandidateOnce(concurrent, 'same-candidate', 'cabinet_width', oneRequest),
]);
assert.equal(repeatedCalls, 1, 'concurrent clicks on the same ID share one request');

const unknown = newConfirmationLedger();
await assert.rejects(confirmCandidateOnce(unknown, 'lost-response', 'filler_width', async () => {
  throw new Error('network disconnected');
}), /network disconnected/);
assert.equal(unknown.confirmed.size, 0, 'lost response is not proof of confirmation');
await assert.rejects(confirmCandidateOnce(unknown, 'wrong-type', 'filler_width', async () => ({
  semantic_type: 'cabinet_width',
})), /did not match/);
assert.equal(unknown.confirmed.size, 0);

// Explicit reviewer-input fields have no proposal IDs, so they need no candidate confirmation.
assert.deepEqual([...await confirmProposalFields([], newConfirmationLedger(), async () => {
  throw new Error('must not call for typed values');
})], []);
console.log('measurement confirmations: repeated widths preserved; failures abort; successful receipts survive retry; typed input unchanged');
