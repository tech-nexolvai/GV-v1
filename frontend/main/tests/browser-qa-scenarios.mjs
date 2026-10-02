/** In-memory scenarios for browser QA only. No imports from backend, storage or model code. */
import { createHash } from 'node:crypto';
import { project, populated, empty, packages, findings, revision, samplePdf } from './browser-qa-fixtures.mjs';

export const uploadPackage = '00000000-0000-4000-8000-000000000103';
const uploadRevision = '00000000-0000-4000-8000-000000000203';
const timestamp = '2026-10-03T10:00:00Z';
const response = (body, status = 200) => ({ body, status });
const refuse = (message, status = 409) => response({ error: 'synthetic_qa_only', message, request_id: 'SYNTHETIC_UI_QA' }, status);

export function createScenarioState(name = null) {
  return {
    name: ['partial-upload', 'approval'].includes(name) ? name : null,
    packageCreates: 0, createdPackage: null, documents: [], sessions: [], actions: [], approved: false,
    storageAttempts: 0, storageFailures: 0, extractionRequests: 0, events: [],
  };
}

export function scenarioPackages(state) {
  return [
    ...packages.map((pkg) => state.approved && pkg.id === populated ? { ...pkg, state: 'APPROVED' } : pkg),
    ...(state.createdPackage ? [state.createdPackage] : []),
  ];
}

export function scenarioFindings(state, packageId = populated) {
  if (packageId === empty || packageId === uploadPackage) return [];
  return findings.map((finding) => ({
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
    findingCount: findings.length, approved: state.approved, events: state.events,
  };
}

/** Return a local response or null so read-only baseline fixture routes can handle the request. */
function respondToScenario(state, method, path, body = {}) {
  if (!state.name) return null;
  const prefix = `/api/v1/projects/${project}`;
  if (path.startsWith('/api/') && !path.startsWith(`${prefix}/`)) {
    // The semantic vocabulary is global, read-only and served by the baseline harness.
    return method === 'GET' && path === '/api/v1/semantic-types' ? null : refuse('Unknown synthetic project.', 404);
  }
  if (method === 'GET') {
    if (path === `${prefix}/packages`) return response({ items: scenarioPackages(state), next_cursor: null, limit: 50, ordering: 'synthetic-test-order' });
    if (path === `${prefix}/review-sessions`) return response({ items: state.sessions });
    const pkg = scenarioPackages(state).find((item) => path === `${prefix}/packages/${item.id}`);
    if (pkg) return response(pkg);
    const findingPackage = scenarioPackages(state).find((item) => path === `${prefix}/packages/${item.id}/findings`);
    if (findingPackage) return response({ items: scenarioFindings(state, findingPackage.id), next_cursor: null, limit: 50, ordering: 'synthetic-test-order' });
    if (/\/(?:report(?:\.pdf)?|redline\.pdf)$/.test(path) && !state.approved) return refuse('Synthetic sign-off is required before downloads.');
    return null;
  }

  if (state.name === 'partial-upload') {
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
      if (document.kind === 'shop') { state.storageFailures += 1; return refuse('Synthetic storage outage after the architect PDF was saved.', 503); }
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
      return refuse('The synthetic vendor PDF is missing. This scenario never runs extraction.');
    }
  }

  if (state.name === 'approval') {
    if (method === 'POST' && path === `${prefix}/packages/${populated}/review-sessions`) {
      if (body.package_revision_id !== revision) return refuse('Wrong synthetic revision.', 422);
      if (state.sessions[0]) return response(state.sessions[0], 201);
      const session = { id: '00000000-0000-4000-8000-000000000501', package_revision_id: revision, reviewer: 'Synthetic QA reviewer', created_at: timestamp, completed_at: null };
      state.sessions.push(session);
      return response(session, 201);
    }
    const session = state.sessions.find((item) => path.startsWith(`${prefix}/review-sessions/${item.id}/`));
    if (session && method === 'POST' && path.endsWith('/actions')) {
      if (state.approved) return refuse('The synthetic review is already signed off.');
      if (!findings.some((finding) => finding.id === body.finding_id) || !['confirm', 'dismiss'].includes(body.action)) return refuse('Only confirm/dismiss of existing synthetic findings is implemented.', 422);
      const action = { finding_id: body.finding_id, action: body.action, actor: 'Synthetic QA reviewer', note: body.note ?? '', created_at: timestamp };
      state.actions.push(action);
      return response(action, 201);
    }
    if (session && method === 'POST' && path.endsWith('/approve')) {
      const unresolved = scenarioFindings(state).filter((finding) => finding.outcome === 'REVIEW_REQUIRED' && !finding.reviewer_action);
      if (unresolved.length) return refuse('Synthetic sign-off refused: a REVIEW_REQUIRED finding still needs a recorded action.');
      state.approved = true;
      session.completed_at = timestamp;
      return response({ approval_id: '00000000-0000-4000-8000-000000000601', package_revision_id: revision, approved_by: 'Synthetic QA reviewer', findings_approved: findings.length, state: 'APPROVED' }, 201);
    }
  }
  if (method === 'POST' && /\/chat(?:\/stream)?$/.test(path)) return null;
  return refuse('This write is not part of the explicitly selected synthetic scenario. No backend request was made.');
}

export function fixturePdf(role) { return samplePdf(role === 'shop'); }

export function handleScenario(state, method, path, body = {}) {
  const result = respondToScenario(state, method, path, body);
  if (state.name && method !== 'GET' && result) {
    state.events.push({ sequence: state.events.length + 1, method, path, status: result.status,
      result: result.status >= 400 ? result.body.message : 'Synthetic request accepted in memory only' });
  }
  return result;
}
