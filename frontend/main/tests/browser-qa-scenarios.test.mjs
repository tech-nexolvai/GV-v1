import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { project, populated, revision, findings, chains } from './browser-qa-fixtures.mjs';
import { createScenarioState, handleScenario, scenarioSnapshot, scenarioFindings, fixturePdf, uploadPackage } from './browser-qa-scenarios.mjs';

const prefix = `/api/v1/projects/${project}`;
{
  const state = createScenarioState('partial-upload');
  const created = handleScenario(state, 'POST', `${prefix}/packages`, { vendor: 'Upload regression' });
  assert.equal(created.status, 201);
  const packageId = created.body.id;
  assert.equal(packageId, uploadPackage);
  for (const kind of ['architectural', 'shop']) {
    const bytes = fixturePdf(kind), sha256 = createHash('sha256').update(bytes).digest('hex');
    const ticket = handleScenario(state, 'POST', `${prefix}/packages/${packageId}/documents`, { kind, sha256 });
    assert.equal(ticket.status, 201);
    const stored = handleScenario(state, 'PUT', ticket.body.upload_url, bytes);
    if (kind === 'shop') { assert.equal(stored.status, 503); continue; }
    assert.equal(stored.status, 200);
    const confirmed = handleScenario(state, 'POST', `${prefix}/documents/${ticket.body.document_id}/confirm`, { sha256, page_count: 1 });
    assert.equal(confirmed.status, 200);
  }
  const snapshot = scenarioSnapshot(state);
  assert.equal(snapshot.packageCreates, 1, 'a partial upload retains exactly the created package');
  assert.equal(snapshot.createdPackage.id, packageId);
  assert.equal(snapshot.documents.length, 2, 'both document registrations remain available after failed storage');
  assert.equal(snapshot.confirmedDocuments, 1, 'the architect PDF remains confirmed');
  assert.equal(snapshot.documents.find((document) => document.kind === 'shop').uploaded, false);
  assert.equal(snapshot.storageAttempts, 2);
  assert.equal(snapshot.storageFailures, 1);
  assert.equal(snapshot.extractionRequests, 0, 'a partial pair never starts extraction');
  assert.equal(handleScenario(state, 'GET', `${prefix}/packages/${packageId}`).body.id, packageId, 'Open saved review can resolve the original package');
  assert.deepEqual(handleScenario(state, 'GET', `${prefix}/packages/${packageId}/findings`).body.items, []);
  assert.equal(handleScenario(state, 'POST', `${prefix}/packages`, { vendor: 'Replacement' }).status, 409);
  assert.equal(state.packageCreates, 1, 'refused retry cannot overwrite or duplicate the fixture package');
}
{
  const state = createScenarioState('approval');
  assert.equal(handleScenario(state, 'GET', `${prefix}/packages/${populated}/report.pdf`).status, 409, 'download is gated before approval');
  const opened = handleScenario(state, 'POST', `${prefix}/packages/${populated}/review-sessions`, { package_revision_id: revision });
  assert.equal(opened.status, 201);
  const sessionPrefix = `${prefix}/review-sessions/${opened.body.id}`;
  assert.equal(handleScenario(state, 'POST', `${sessionPrefix}/approve`).status, 409, 'unaddressed REVIEW_REQUIRED blocks sign-off');
  assert.equal(state.approved, false);
  assert.equal(state.sessions[0].completed_at, null, 'refusal does not complete a sitting');
  for (const finding of findings) {
    const acted = handleScenario(state, 'POST', `${sessionPrefix}/actions`, { finding_id: finding.id, action: 'confirm' });
    assert.equal(acted.status, 201);
  }
  assert.equal(scenarioSnapshot(state).reviewedCount, 4);
  assert.ok(scenarioFindings(state).every((finding) => finding.reviewer_action.action === 'confirm'), 'reload gets recorded actions');
  assert.deepEqual(scenarioFindings(state).map((finding) => finding.outcome), findings.map((finding) => finding.outcome), 'review actions do not change fixture verdicts');
  const approval = handleScenario(state, 'POST', `${sessionPrefix}/approve`);
  assert.equal(approval.status, 201);
  assert.equal(approval.body.findings_approved, 4);
  assert.equal(handleScenario(state, 'GET', `${prefix}/packages/${populated}`).body.state, 'APPROVED');
  assert.ok(state.sessions[0].completed_at);
  for (const artifact of ['report.pdf', 'redline.pdf', 'report']) assert.equal(handleScenario(state, 'GET', `${prefix}/packages/${populated}/${artifact}`), null, 'approved download passes to local fixture bytes');
  assert.equal(handleScenario(state, 'POST', `${sessionPrefix}/actions`, { finding_id: findings[0].id, action: 'dismiss' }).status, 409, 'completed fixture session refuses extra actions');
  assert.equal(handleScenario(state, 'DELETE', `${prefix}/packages/${populated}`).status, 409, 'unknown writes never reach any backend');
}
assert.equal(handleScenario(createScenarioState(), 'POST', `${prefix}/packages`, {}), null, 'no scenario delegates to baseline refusal');
{
  const state = createScenarioState('decision-save');
  const session = handleScenario(state, 'POST', `${prefix}/packages/${populated}/review-sessions`, { package_revision_id: revision }).body;
  const scoped = `${prefix}/review-sessions/${session.id}`;
  const chain = handleScenario(state, 'GET', `${prefix}/packages/${populated}/findings/${findings[0].id}/chain`).body;
  assert.equal(chain.operands.filter(op => op.evidence).length, 1);
  assert.equal(chains[findings[0].id].operands.filter(op => op.evidence).length, 2, 'baseline fixture never mutated');
  const payloads = [
    ['evidence', { finding_id: findings[0].id, observation_id: 'synthetic-shop-crop', action: 'correct', corrected_value: '25 1/2 in' }],
    ['exceptions', { finding_id: findings[2].id, scope: 'finding', scope_id: findings[2].id, reason: 'Synthetic QA exception', expires_at: '2030-01-01T12:00:00.000Z' }],
  ];
  for (const [endpoint, body] of payloads) {
    const before = state.actions.length;
    assert.equal(handleScenario(state, 'POST', `${scoped}/${endpoint}`, { ...body, finding_id: 'wrong' }).status, 422);
    assert.equal(handleScenario(state, 'POST', `${scoped}/${endpoint}`, body).status, 503);
    assert.equal(state.actions.length, before, 'rejection records no action');
    assert.equal(handleScenario(state, 'POST', `${scoped}/${endpoint}`, body).status, 201);
    assert.equal(state.actions.length, before + 1, 'explicit retry records exactly one action');
    assert.equal(handleScenario(state, 'POST', `${scoped}/${endpoint}`, body).status, 409);
    assert.equal(state.actions.length, before + 1);
  }
  assert.deepEqual(scenarioFindings(state).map(f => f.outcome), findings.map(f => f.outcome), 'saved corrections/exceptions leave recorded verdicts unchanged');
}
console.log('browser-qa-scenarios: partial-upload retention, counts, approval gate, exact verdict preservation and download gating passed');
