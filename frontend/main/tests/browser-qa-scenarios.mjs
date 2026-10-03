/** In-memory scenarios for browser QA only. No imports from backend, storage or model code. */
import { createHash } from 'node:crypto';
import { project, populated, empty, packages, findings, chains, needed, revision, samplePdf, candidates } from './browser-qa-fixtures.mjs';

export const uploadPackage = '00000000-0000-4000-8000-000000000103';
const uploadRevision = '00000000-0000-4000-8000-000000000203';
const timestamp = '2026-10-03T10:00:00Z';
const response = (body, status = 200) => ({ body, status });
const refuse = (message, status = 409) => response({ error: 'synthetic_qa_only', message, request_id: 'SYNTHETIC_UI_QA' }, status);
// Fixed fixture answers, NOT an implementation of the engine or an exact-value parser.
export const flowMeasurements = [
  { rule_id: 'CT-DEPTH-001', name: 'vendor_depth', value: '25 1/4 in' },
  { rule_id: 'CT-DEPTH-001', name: 'approved_depth', value: '25 1/2 in' },
];
const flowFindings = findings.map((finding, i) => ({ ...finding,
  id: `00000000-0000-4000-8000-00000000040${i + 1}`,
  package_revision_id: uploadRevision, check_run_id: 'synthetic-flow-run',
}));

export function createScenarioState(name = null) {
  return {
    name: ['partial-upload', 'approval', 'review-flow', 'decision-save', 'action-save', 'reading-confirmation'].includes(name) ? name : null,
    confirmationAttempts: 0, confirmedReadings: [],
    dismissAttempts: 0,
    decisionAttempts: { correction: 0, exception: 0 },
    packageCreates: 0, createdPackage: null, documents: [], sessions: [], actions: [], approved: false,
    storageAttempts: 0, storageFailures: 0, extractionRequests: 0, events: [],
    extracted: false, storedMeasurements: null, checksRequests: 0, resultsReady: false,
  };
}

export function scenarioPackages(state) {
  return [
    ...packages.map((pkg) => state.name === 'approval' && state.approved && pkg.id === populated ? { ...pkg, state: 'APPROVED' } : pkg),
    ...(state.createdPackage ? [state.createdPackage] : []),
  ];
}

export function scenarioFindings(state, packageId = populated) {
  if (packageId === empty || (packageId === uploadPackage && !state.resultsReady)) return [];
  return (packageId === uploadPackage ? flowFindings : findings).map((finding) => ({
    ...finding,
    reviewer_action: state.actions.findLast((action) => action.finding_id === finding.id) ?? null,
  }));
}

export function scenarioSnapshot(state) {
  return {
    scenario: state.name, packageCreates: state.packageCreates, packageIds: scenarioPackages(state).map((pkg) => pkg.id),
    createdPackage: state.createdPackage,
    documents: state.documents.map(({ bytes: _bytes, ...document }) => document),
    confirmedDocuments: state.documents.filter((document) => document.confirmed).length,
    storageAttempts: state.storageAttempts, storageFailures: state.storageFailures, extractionRequests: state.extractionRequests,
    sessions: state.sessions, actions: state.actions, reviewedCount: new Set(state.actions.map((action) => action.finding_id)).size,
    findingCount: state.name === 'review-flow' && !state.resultsReady ? 0 : findings.length,
    approved: state.approved, events: state.events,
    decisionAttempts: state.decisionAttempts,
    dismissAttempts: state.dismissAttempts,
    confirmationAttempts: state.confirmationAttempts, confirmedReadings: state.confirmedReadings,
    extracted: state.extracted, storedMeasurements: state.storedMeasurements,
    checksRequests: state.checksRequests, resultsReady: state.resultsReady,
  };
}

/** Return a local response or null so read-only baseline fixture routes can handle the request. */
function respondToScenario(state, method, path, body = {}) {
  if (!state.name) return null;
  const prefix = `/api/v1/projects/${project}`;
  const flow = state.name === 'review-flow';
  const targetPackage = flow ? uploadPackage : populated;
  const targetRevision = flow ? uploadRevision : revision;
  if (path.startsWith('/api/') && !path.startsWith(`${prefix}/`)) {
    // The semantic vocabulary is global, read-only and served by the baseline harness.
    return method === 'GET' && path === '/api/v1/semantic-types' ? null : refuse('Unknown synthetic project.', 404);
  }
  if (method === 'GET') {
    if (state.name === 'reading-confirmation' && path === `${prefix}/packages/${populated}/required-inputs`) {
      return response({ ...needed, confirmed_readings: [...needed.confirmed_readings, ...state.confirmedReadings] });
    }
    if (state.name === 'reading-confirmation' && path === `${prefix}/packages/${populated}/candidates`) {
      const remaining = state.confirmedReadings.length ? [] : candidates;
      return response({ candidates: remaining, total: remaining.length });
    }
    if (path === `${prefix}/packages`) return response({ items: scenarioPackages(state), next_cursor: null, limit: 50, ordering: 'synthetic-test-order' });
    if (path === `${prefix}/review-sessions`) return response({ items: state.sessions });
    const pkg = scenarioPackages(state).find((item) => path === `${prefix}/packages/${item.id}`);
    if (pkg) return response(pkg);
    const findingPackage = scenarioPackages(state).find((item) => path === `${prefix}/packages/${item.id}/findings`);
    if (findingPackage) return response({ items: scenarioFindings(state, findingPackage.id), next_cursor: null, limit: 50, ordering: 'synthetic-test-order' });
    if (state.name === 'decision-save' && path === `${prefix}/packages/${populated}/findings/${findings[0].id}/chain`) {
      // This explicit fixture has one correctable drawing operand; the original two-crop fixture
      // remains unchanged for all other scenarios. Approved numeric facts remain in the trace.
      const chain = chains[findings[0].id];
      return response({ ...chain, operands: chain.operands.map(op => op.name === 'approved_depth' ? { ...op, evidence: null } : op) });
    }
    if (flow && path === `${prefix}/packages/${uploadPackage}/required-inputs`) return response({ ...needed, confirmed_readings: [], revision_state: state.createdPackage?.state ?? 'CREATED' });
    if (flow && path.endsWith('/chain')) {
      const i = flowFindings.findIndex((finding) => path === `${prefix}/packages/${uploadPackage}/findings/${finding.id}/chain`);
      if (i >= 0) return state.resultsReady ? response({ ...chains[findings[i].id], finding_id: flowFindings[i].id }) : refuse('Fixture checks have not run.');
    }
    if (/\/(?:report(?:\.pdf)?|redline\.pdf)$/.test(path)
      && (!state.approved || !path.startsWith(`${prefix}/packages/${targetPackage}/`))) return refuse('Synthetic sign-off is required for this package before downloads.');
    return null;
  }

  if (state.name === 'reading-confirmation' && method === 'POST'
    && path === `${prefix}/packages/${populated}/candidates/synthetic-shop-crop/confirm`) {
    if (Object.keys(body).length !== 1 || !['countertop_depth', 'filler_width'].includes(body.semantic_type)) return refuse('Fixture expects only a reviewer-selected semantic_type.', 422);
    if (state.confirmedReadings.length) return refuse('Fixture reading was already confirmed.');
    state.confirmationAttempts++;
    if (state.confirmationAttempts === 1) return refuse('Synthetic confirmation service temporarily unavailable. Try use again.', 503);
    state.confirmedReadings.push({ key: `SHOP:${body.semantic_type}`, source: 'SHOP', semantic_type: body.semantic_type, value: candidates[0].value, qualification: 'reviewer_confirmed' });
    return response({ canonical_observation_id: 'synthetic-confirmed-reading', semantic_type: body.semantic_type, status: 'HUMAN_CONFIRMED' }, 201);
  }

  if (state.name === 'partial-upload' || flow) {
    if (method === 'POST' && path === `${prefix}/packages`) {
      if (state.createdPackage) return refuse('The synthetic package already exists. Open the saved review instead of replacing it.');
      state.createdPackage = { id: uploadPackage, project_id: project, current_revision_id: uploadRevision, current_revision_number: 1,
        state: 'UPLOADING', vendor: `SYNTHETIC UI QA · ${body.vendor || 'Partial upload'}`, created_at: timestamp };
      state.packageCreates += 1;
      return response(state.createdPackage, 201);
    }
    if (method === 'POST' && path === `${prefix}/packages/${uploadPackage}/documents`) {
      if (!state.createdPackage) return refuse('Create the synthetic package first.');
      if (!['architectural', 'shop'].includes(body.kind) || !/^[0-9a-f]{64}$/.test(body.sha256 ?? '')) return refuse('A synthetic PDF role and SHA-256 are required.', 422);
      const document = { id: `synthetic-${body.kind}-upload`, kind: body.kind, sha256: body.sha256, confirmed: false, uploaded: false, bytes: null };
      if (state.documents.some((item) => item.id === document.id)) return refuse('That synthetic registration is already retained.');
      state.documents.push(document);
      return response({ document_id: document.id, storage_key: document.id, upload_url: `/_dev/upload/${document.id}`, method: 'PUT', expires_at: '2099-01-01T00:00:00Z', required_headers: { 'Content-Type': 'application/pdf' } }, 201);
    }
    if (method === 'PUT' && path.startsWith('/_dev/upload/')) {
      const document = state.documents.find((item) => path === `/_dev/upload/${item.id}`);
      if (!document) return refuse('Unknown synthetic storage registration.', 404);
      state.storageAttempts += 1;
      if (!flow && document.kind === 'shop') { state.storageFailures += 1; return refuse('Synthetic storage outage after the architect PDF was saved.', 503); }
      if (!Buffer.isBuffer(body) || !body.subarray(0, 5).equals(Buffer.from('%PDF-'))) return refuse('Expected a synthetic PDF payload.', 422);
      document.bytes = body;
      document.uploaded = true;
      return response({ synthetic_uploaded: true });
    }
    if (method === 'POST' && path.startsWith(`${prefix}/documents/`) && path.endsWith('/confirm')) {
      const document = state.documents.find((item) => path === `${prefix}/documents/${item.id}/confirm`);
      if (!document?.bytes) return refuse('The synthetic PDF has not been uploaded.');
      const digest = createHash('sha256').update(document.bytes).digest('hex');
      if (digest !== body.sha256 || digest !== document.sha256 || body.page_count !== 1) return refuse('Synthetic upload confirmation does not match its PDF.', 422);
      document.confirmed = true;
      return response({ document_id: document.id, state: 'CONFIRMED' });
    }
    if (method === 'POST' && path === `${prefix}/packages/${uploadPackage}/extract`) {
      state.extractionRequests += 1;
      if (flow && state.documents.filter((document) => document.confirmed).length === 2) {
        state.extracted = true;
        state.createdPackage.state = 'AWAITING_REVIEW';
        return response({ accepted_id: 'synthetic-extraction', package_revision_id: uploadRevision }, 202);
      }
      return refuse(flow ? 'Fixture extraction requires both confirmed PDFs.' : 'The synthetic vendor PDF is missing. This scenario never runs extraction.');
    }
    if (flow && method === 'POST' && path === `${prefix}/packages/${uploadPackage}/measurements`) {
      if (!state.extracted || state.approved) return refuse('Fixture values require a complete PDF pair and an open review.');
      if (body.parameters?.length || body.classifications?.length || body.measurements?.length !== 2
        || !flowMeasurements.every((expected) => body.measurements.some((actual) => actual.rule_id === expected.rule_id && actual.name === expected.name && actual.value === expected.value && !actual.values))) {
        return refuse('This fixture only accepts the two documented synthetic depths, with units. No value parser or engine runs here.', 422);
      }
      state.storedMeasurements = structuredClone(body.measurements);
      return response({ parameter_set_version: null, measurement_set_version: 1, parameters: [], lists: [], measurements: [
        { name: 'vendor_depth', numerator: '101', denominator: '4', unit: 'in', as_typed: '25 1/4 in' },
        { name: 'approved_depth', numerator: '51', denominator: '2', unit: 'in', as_typed: '25 1/2 in' },
      ] }, 201);
    }
    if (flow && method === 'POST' && path === `${prefix}/packages/${uploadPackage}/checks`) {
      if (!state.storedMeasurements || state.approved) return refuse('Save fixture values before checks; signed-off fixtures are read-only.');
      state.checksRequests += 1;
      state.resultsReady = true; // Preauthored outcomes only; no real extraction or verdict calculation.
      return response({ accepted_id: 'synthetic-checks', package_revision_id: uploadRevision }, 202);
    }
  }

  if (state.name === 'approval' || flow || state.name === 'decision-save' || state.name === 'action-save') {
    if (method === 'POST' && path === `${prefix}/packages/${targetPackage}/review-sessions`) {
      if (flow && !state.extracted) return refuse('Upload the fixture pair before opening a sitting.');
      if (body.package_revision_id !== targetRevision) return refuse('Wrong synthetic revision.', 422);
      if (state.sessions[0]) return response(state.sessions[0], 201);
      const session = { id: '00000000-0000-4000-8000-000000000501', package_revision_id: targetRevision, reviewer: 'Synthetic QA reviewer', created_at: timestamp, completed_at: null };
      state.sessions.push(session);
      return response(session, 201);
    }
    const session = state.sessions.find((item) => path.startsWith(`${prefix}/review-sessions/${item.id}/`));
    if (state.name === 'decision-save' && session && method === 'POST' && /\/(evidence|exceptions)$/.test(path)) {
      const correction = path.endsWith('/evidence');
      const kind = correction ? 'correction' : 'exception';
      if (body.finding_id !== findings[correction ? 0 : 2].id) return refuse('Wrong synthetic finding.', 422);
      if (correction ? (body.observation_id !== 'synthetic-shop-crop' || body.action !== 'correct' || body.corrected_value !== '25 1/2 in')
        : (body.scope !== 'finding' || body.scope_id !== body.finding_id || body.reason !== 'Synthetic QA exception' || body.expires_at !== '2030-01-01T12:00:00.000Z')) return refuse('Use only the documented synthetic decision payload.', 422);
      state.decisionAttempts[kind] += 1;
      if (state.decisionAttempts[kind] === 1) return refuse(`Synthetic ${kind} save rejected once. Your input can be retried.`, 503);
      if (state.actions.some(item => item.finding_id === body.finding_id)) return refuse('Synthetic decision already recorded.');
      const action = { id: `synthetic-${kind}-action`, finding_id: body.finding_id, action: correction ? 'correct' : 'except', actor: 'Synthetic QA reviewer', note: '', created_at: timestamp };
      state.actions.push(action);
      return response(correction ? { action, original_observation_id: 'synthetic-shop-crop', resulting_observation_id: 'synthetic-corrected-reading', original_value: '101/4 in', resulting_value: '51/2 in' }
        : { id: 'synthetic-exception', review_action_id: action.id, scope: body.scope, scope_id: body.scope_id, reason: body.reason, expires_at: body.expires_at, approved_by: action.actor, created_at: timestamp }, 201);
    }
    if (session && method === 'POST' && path.endsWith('/actions')) {
      if (state.approved) return refuse('The synthetic review is already signed off.');
      if (!scenarioFindings(state, targetPackage).some((finding) => finding.id === body.finding_id) || !['confirm', 'dismiss'].includes(body.action)) return refuse('Only confirm/dismiss of existing synthetic findings is implemented.', 422);
      if (state.name === 'action-save' && body.action === 'dismiss') {
        state.dismissAttempts += 1;
        if (state.dismissAttempts === 1) return refuse('Synthetic dismissal rejected once. No dismissal was recorded.', 503);
      }
      const action = { finding_id: body.finding_id, action: body.action, actor: 'Synthetic QA reviewer', note: body.note ?? '', created_at: timestamp };
      state.actions.push(action);
      return response(action, 201);
    }
    if (session && method === 'POST' && path.endsWith('/approve')) {
      if (flow && !state.resultsReady) return refuse('Run fixture checks before sign-off.');
      const unresolved = scenarioFindings(state, targetPackage).filter((finding) => finding.outcome === 'REVIEW_REQUIRED' && !finding.reviewer_action);
      if (unresolved.length) return refuse('Synthetic sign-off refused: a REVIEW_REQUIRED finding still needs a recorded action.');
      state.approved = true;
      if (flow) state.createdPackage.state = 'APPROVED';
      session.completed_at = timestamp;
      return response({ approval_id: '00000000-0000-4000-8000-000000000601', package_revision_id: targetRevision, approved_by: 'Synthetic QA reviewer', findings_approved: findings.length, state: 'APPROVED' }, 201);
    }
  }
  if (method === 'POST' && /\/chat(?:\/stream)?$/.test(path)) return null;
  return refuse('This write is not part of the explicitly selected synthetic scenario. No backend request was made.');
}

export function fixturePdf(role) { return samplePdf(role === 'shop'); }

/** Explicit browser starting point when file-picker automation is unavailable. Upload contracts
 * are exercised by the real client in review-flow.test.mjs, not claimed as browser interactions. */
export function prepareUploadedFixture(state) {
  if (state.name !== 'review-flow' || state.createdPackage) throw new Error('Expected a fresh review-flow fixture.');
  const prefix = `/api/v1/projects/${project}`;
  handleScenario(state, 'POST', `${prefix}/packages`, { vendor: 'Connected flow fixture' });
  for (const kind of ['architectural', 'shop']) {
    const bytes = fixturePdf(kind), sha256 = createHash('sha256').update(bytes).digest('hex');
    const ticket = handleScenario(state, 'POST', `${prefix}/packages/${uploadPackage}/documents`, { kind, sha256 });
    handleScenario(state, 'PUT', ticket.body.upload_url, bytes);
    handleScenario(state, 'POST', `${prefix}/documents/${ticket.body.document_id}/confirm`, { sha256, page_count: 1 });
  }
  handleScenario(state, 'POST', `${prefix}/packages/${uploadPackage}/extract`);
  handleScenario(state, 'POST', `${prefix}/packages/${uploadPackage}/review-sessions`, { package_revision_id: uploadRevision });
}

export function handleScenario(state, method, path, body = {}) {
  const result = respondToScenario(state, method, path, body);
  if (state.name && method !== 'GET' && result) {
    state.events.push({ sequence: state.events.length + 1, method, path, status: result.status,
      result: result.status >= 400 ? result.body.message : 'Synthetic request accepted in memory only' });
  }
  return result;
}
